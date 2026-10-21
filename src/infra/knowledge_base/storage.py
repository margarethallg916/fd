"""MongoDB persistence for the personal knowledge base (single source of truth).

Mongo has no transactions here, so every state change is a conditional update whose
filter names the expected current state; callers treat ``None``/``False`` as "lost the race".
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import uuid4

from pymongo import ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from src.infra.logging import get_logger
from src.infra.storage.mongodb import get_mongo_client
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.knowledge_base import (
    DOC_ERROR_INDEXING,
    DOC_ERROR_PARSING,
    DOC_FAILED_STATUSES,
    DOC_INDEXED,
    DOC_INDEXING,
    DOC_PARSED,
    DOC_PARSING,
    DOC_PROCESSING_STATUSES,
    DOC_UPLOADED,
    KB_STATUS_ACTIVE,
    KB_STATUS_DELETING,
    TASK_ACTIVE_STATUSES,
    TASK_CANCELLED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_TERMINAL_STATUSES,
    TASK_TYPE_INGEST,
)

logger = get_logger(__name__)

BASES_COLLECTION = "kb_bases"
MEMBERS_COLLECTION = "kb_members"
DOCUMENTS_COLLECTION = "kb_documents"
CHUNKS_COLLECTION = "kb_chunks"
TASKS_COLLECTION = "kb_tasks"

DOC_LIST_PAGE_SIZE_MAX = 200
CHUNK_INSERT_BATCH = 500
STATUS_GROUPS: dict[str, tuple[str, ...]] = {
    "processing": DOC_PROCESSING_STATUSES,
    "failed": DOC_FAILED_STATUSES,
}


def expand_status_filter(raw: str | None) -> list[str] | None:
    """Turn ``status`` query text (comma separated, with group aliases) into a status list."""
    if not raw:
        return None
    statuses: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        statuses.extend(STATUS_GROUPS.get(part, (part,)))
    return statuses or None


def _error_doc(stage: str, message: str, at: datetime) -> dict[str, Any]:
    return {"stage": stage, "message": message, "at": at}


class KnowledgeBaseStorage:
    def __init__(self) -> None:
        self._db: Any = None

    @property
    def db(self) -> Any:
        if self._db is None:
            self._db = get_mongo_client()[settings.MONGODB_DB]
        return self._db

    @property
    def bases(self) -> Any:
        return self.db[BASES_COLLECTION]

    @property
    def members(self) -> Any:
        return self.db[MEMBERS_COLLECTION]

    @property
    def documents(self) -> Any:
        return self.db[DOCUMENTS_COLLECTION]

    @property
    def chunks(self) -> Any:
        return self.db[CHUNKS_COLLECTION]

    @property
    def tasks(self) -> Any:
        return self.db[TASKS_COLLECTION]

    async def ensure_indexes(self) -> None:
        await self.bases.create_index([("owner_id", ASCENDING), ("updated_at", DESCENDING)])
        await self.bases.create_index([("status", ASCENDING)])
        await self.members.create_index([("kb_id", ASCENDING), ("user_id", ASCENDING)], unique=True)
        await self.members.create_index([("user_id", ASCENDING), ("role", ASCENDING)])
        await self.documents.create_index(
            [("kb_id", ASCENDING), ("sha256", ASCENDING)], unique=True
        )
        await self.documents.create_index([("kb_id", ASCENDING), ("created_at", DESCENDING)])
        await self.documents.create_index([("kb_id", ASCENDING), ("status", ASCENDING)])
        await self.documents.create_index([("kb_id", ASCENDING), ("filename", ASCENDING)])
        await self.documents.create_index([("status", ASCENDING), ("updated_at", ASCENDING)])
        await self.chunks.create_index([("doc_id", ASCENDING), ("index", ASCENDING)])
        await self.chunks.create_index([("kb_id", ASCENDING)])
        # At most one queued ingest task per KB: uploads merge into it via upsert.
        await self.tasks.create_index(
            [("kb_id", ASCENDING), ("type", ASCENDING)],
            unique=True,
            partialFilterExpression={"status": TASK_QUEUED},
            name="kb_type_queued_unique",
        )
        await self.tasks.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.tasks.create_index([("kb_id", ASCENDING), ("created_at", DESCENDING)])
        await self.tasks.create_index([("created_by", ASCENDING), ("status", ASCENDING)])
        logger.info("[KB] storage indexes ensured")

    # ── knowledge bases ─────────────────────────────────────

    async def insert_base(self, doc: dict[str, Any]) -> None:
        await self.bases.insert_one(doc)

    async def get_base(self, kb_id: str) -> Optional[dict[str, Any]]:
        return await self.bases.find_one({"_id": kb_id})

    async def list_bases(self, kb_ids: list[str]) -> list[dict[str, Any]]:
        if not kb_ids:
            return []
        cursor = self.bases.find({"_id": {"$in": kb_ids}, "status": KB_STATUS_ACTIVE})
        return await cursor.sort("updated_at", DESCENDING).to_list(length=len(kb_ids))

    async def update_base(self, kb_id: str, updates: dict[str, Any]) -> Optional[dict[str, Any]]:
        return await self.bases.find_one_and_update(
            {"_id": kb_id, "status": KB_STATUS_ACTIVE},
            {"$set": {**updates, "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )

    async def lock_embedding_signature(self, kb_id: str, signature: dict[str, Any]) -> bool:
        """Set the KB's embedding signature once; False if one is already present."""
        result = await self.bases.update_one(
            {"_id": kb_id, "embedding": None}, {"$set": {"embedding": signature}}
        )
        return result.modified_count > 0

    async def clear_embedding_signature(self, kb_id: str) -> None:
        await self.bases.update_one({"_id": kb_id}, {"$set": {"embedding": None}})

    async def mark_base_deleting(self, kb_id: str) -> bool:
        result = await self.bases.update_one(
            {"_id": kb_id, "status": KB_STATUS_ACTIVE},
            {"$set": {"status": KB_STATUS_DELETING, "updated_at": utc_now()}},
        )
        return result.modified_count > 0

    async def list_deleting_bases(self, limit: int = 20) -> list[dict[str, Any]]:
        cursor = self.bases.find({"status": KB_STATUS_DELETING})
        return await cursor.limit(limit).to_list(length=limit)

    async def delete_base(self, kb_id: str) -> None:
        await self.bases.delete_one({"_id": kb_id})

    # ── members ─────────────────────────────────────────────

    async def insert_member(self, doc: dict[str, Any]) -> None:
        await self.members.insert_one(doc)

    async def get_member(self, kb_id: str, user_id: str) -> Optional[dict[str, Any]]:
        return await self.members.find_one({"kb_id": kb_id, "user_id": user_id})

    async def list_member_user_ids(self, kb_id: str) -> list[str]:
        docs = await self.members.find({"kb_id": kb_id}, {"user_id": 1}).to_list(length=1000)
        return [str(d["user_id"]) for d in docs]

    async def list_member_kb_ids(self, user_id: str, roles: list[str]) -> list[str]:
        cursor = self.members.find({"user_id": user_id, "role": {"$in": roles}}, {"kb_id": 1})
        docs = await cursor.to_list(length=5000)
        return [str(d["kb_id"]) for d in docs]

    async def delete_members_by_kb(self, kb_id: str) -> None:
        await self.members.delete_many({"kb_id": kb_id})

    # ── documents ───────────────────────────────────────────

    async def insert_document(self, doc: dict[str, Any]) -> None:
        """Raises ``DuplicateKeyError`` when ``(kb_id, sha256)`` already exists."""
        await self.documents.insert_one(doc)

    async def get_document(self, kb_id: str, doc_id: str) -> Optional[dict[str, Any]]:
        return await self.documents.find_one({"_id": doc_id, "kb_id": kb_id})

    async def get_document_by_id(self, doc_id: str) -> Optional[dict[str, Any]]:
        return await self.documents.find_one({"_id": doc_id})

    async def get_document_by_sha(self, kb_id: str, sha256: str) -> Optional[dict[str, Any]]:
        return await self.documents.find_one({"kb_id": kb_id, "sha256": sha256})

    async def list_documents(
        self,
        kb_id: str,
        *,
        statuses: list[str] | None = None,
        query: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        page_size = min(max(int(page_size), 1), DOC_LIST_PAGE_SIZE_MAX)
        page = max(int(page), 1)
        flt: dict[str, Any] = {"kb_id": kb_id}
        if statuses:
            flt["status"] = {"$in": statuses}
        if query:
            flt["filename"] = {"$regex": re.escape(query.strip()), "$options": "i"}
        total = await self.documents.count_documents(flt)
        cursor = self.documents.find(flt).sort("created_at", DESCENDING)
        items = await cursor.skip((page - 1) * page_size).limit(page_size).to_list(length=page_size)
        return items, total

    async def list_documents_by_ids(self, kb_id: str, doc_ids: list[str]) -> list[dict[str, Any]]:
        if not doc_ids:
            return []
        cursor = self.documents.find({"kb_id": kb_id, "_id": {"$in": doc_ids}})
        return await cursor.to_list(length=len(doc_ids))

    async def list_documents_by_status(
        self, kb_id: str, statuses: list[str], limit: int = 1000
    ) -> list[dict[str, Any]]:
        cursor = self.documents.find({"kb_id": kb_id, "status": {"$in": statuses}})
        return await cursor.sort("created_at", ASCENDING).limit(limit).to_list(length=limit)

    async def list_documents_for_cleanup(
        self, kb_id: str, limit: int = 200
    ) -> list[dict[str, Any]]:
        return await self.documents.find({"kb_id": kb_id}).limit(limit).to_list(length=limit)

    async def document_briefs(self, doc_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not doc_ids:
            return {}
        cursor = self.documents.find({"_id": {"$in": doc_ids}}, {"kb_id": 1, "filename": 1})
        docs = await cursor.to_list(length=len(doc_ids))
        return {str(d["_id"]): d for d in docs}

    async def transition_document(
        self,
        doc_id: str,
        from_statuses: list[str],
        updates: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        """CAS a document to a new state; ``None`` means it was deleted or handled elsewhere."""
        return await self.documents.find_one_and_update(
            {"_id": doc_id, "status": {"$in": from_statuses}},
            {"$set": {**updates, "updated_at": utc_now()}},
            return_document=ReturnDocument.AFTER,
        )

    async def delete_documents(self, kb_id: str, doc_ids: list[str]) -> list[dict[str, Any]]:
        docs = await self.list_documents_by_ids(kb_id, doc_ids)
        if docs:
            await self.documents.delete_many(
                {"kb_id": kb_id, "_id": {"$in": [d["_id"] for d in docs]}}
            )
        return docs

    async def delete_documents_by_kb(self, kb_id: str) -> None:
        await self.documents.delete_many({"kb_id": kb_id})

    async def settle_documents(
        self, doc_ids: list[str], message: str, *, now: datetime | None = None
    ) -> int:
        """Move unfinished documents to their failure states (cancel / lease exhaustion)."""
        if not doc_ids:
            return 0
        now = now or utc_now()
        parse = await self.documents.update_many(
            {"_id": {"$in": doc_ids}, "status": {"$in": [DOC_UPLOADED, DOC_PARSING]}},
            {
                "$set": {
                    "status": DOC_ERROR_PARSING,
                    "error": _error_doc("parse", message, now),
                    "updated_at": now,
                }
            },
        )
        index = await self.documents.update_many(
            {"_id": {"$in": doc_ids}, "status": {"$in": [DOC_PARSED, DOC_INDEXING]}},
            {
                "$set": {
                    "status": DOC_ERROR_INDEXING,
                    "error": _error_doc("index", message, now),
                    "updated_at": now,
                }
            },
        )
        return int(parse.modified_count) + int(index.modified_count)

    async def rollback_documents(self, doc_ids: list[str]) -> None:
        """Undo in-flight states of a task being requeued: parsing->uploaded, indexing->parsed."""
        if not doc_ids:
            return
        now = utc_now()
        await self.documents.update_many(
            {"_id": {"$in": doc_ids}, "status": DOC_PARSING},
            {"$set": {"status": DOC_UPLOADED, "updated_at": now}},
        )
        await self.documents.update_many(
            {"_id": {"$in": doc_ids}, "status": DOC_INDEXING},
            {"$set": {"status": DOC_PARSED, "updated_at": now}},
        )

    async def list_unqueued_stale_documents(
        self, before: datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        """``uploaded``/``parsed`` documents untouched since ``before`` and in no active task."""
        cursor = self.documents.find(
            {"status": {"$in": [DOC_UPLOADED, DOC_PARSED]}, "updated_at": {"$lt": before}},
            {"kb_id": 1, "status": 1},
        )
        docs = await cursor.sort("updated_at", ASCENDING).limit(limit).to_list(length=limit)
        if not docs:
            return []
        ids = [d["_id"] for d in docs]
        covered: set[str] = set()
        active = self.tasks.find(
            {"status": {"$in": list(TASK_ACTIVE_STATUSES)}, "doc_ids": {"$in": ids}},
            {"doc_ids": 1},
        )
        for task in await active.to_list(length=1000):
            covered.update(task.get("doc_ids") or [])
        return [d for d in docs if d["_id"] not in covered]

    async def stats_for(self, kb_ids: list[str]) -> dict[str, dict[str, int]]:
        """Per-KB counters from one aggregation over ``(kb_id, status)``."""
        result: dict[str, dict[str, int]] = {
            kb_id: {
                "doc_count": 0,
                "indexed_count": 0,
                "processing_count": 0,
                "failed_count": 0,
                "chunk_count": 0,
                "size_bytes": 0,
            }
            for kb_id in kb_ids
        }
        if not kb_ids:
            return result
        pipeline = [
            {"$match": {"kb_id": {"$in": kb_ids}}},
            {
                "$group": {
                    "_id": {"kb_id": "$kb_id", "status": "$status"},
                    "count": {"$sum": 1},
                    "size": {"$sum": "$size"},
                    "chunks": {"$sum": "$chunk_count"},
                }
            },
        ]
        async for row in self.documents.aggregate(pipeline):
            key = row["_id"]
            stats = result.get(key["kb_id"])
            if stats is None:
                continue
            count = int(row.get("count") or 0)
            status = key.get("status")
            stats["doc_count"] += count
            stats["size_bytes"] += int(row.get("size") or 0)
            stats["chunk_count"] += int(row.get("chunks") or 0)
            if status == DOC_INDEXED:
                stats["indexed_count"] += count
            elif status in DOC_FAILED_STATUSES:
                stats["failed_count"] += count
            else:
                stats["processing_count"] += count
        return result

    # ── chunks ──────────────────────────────────────────────

    async def replace_chunks(self, kb_id: str, doc_id: str, chunks: list[dict[str, Any]]) -> None:
        await self.chunks.delete_many({"doc_id": doc_id})
        for start in range(0, len(chunks), CHUNK_INSERT_BATCH):
            batch = chunks[start : start + CHUNK_INSERT_BATCH]
            await self.chunks.insert_many(batch)

    async def list_chunks(
        self, doc_id: str, *, page: int = 1, page_size: int = 20
    ) -> tuple[list[dict[str, Any]], int]:
        page = max(int(page), 1)
        page_size = min(max(int(page_size), 1), 100)
        total = await self.chunks.count_documents({"doc_id": doc_id})
        cursor = self.chunks.find({"doc_id": doc_id}).sort("index", ASCENDING)
        items = await cursor.skip((page - 1) * page_size).limit(page_size).to_list(length=page_size)
        return items, total

    async def delete_chunks_by_docs(self, doc_ids: list[str]) -> None:
        if doc_ids:
            await self.chunks.delete_many({"doc_id": {"$in": doc_ids}})

    async def delete_chunks_by_kb(self, kb_id: str) -> None:
        await self.chunks.delete_many({"kb_id": kb_id})

    # ── tasks ───────────────────────────────────────────────

    async def enqueue_task(self, kb_id: str, doc_ids: list[str], created_by: str) -> dict[str, Any]:
        """Merge ``doc_ids`` into the KB's queued ingest task, creating it when absent."""
        now = utc_now()
        update = {
            "$addToSet": {"doc_ids": {"$each": list(doc_ids)}},
            "$setOnInsert": {
                "_id": uuid4().hex,
                "created_by": created_by,
                "cancel_requested": False,
                "progress": {"total": 0, "done": 0, "failed": 0, "current_doc_id": None},
                "attempts": 0,
                "lease_owner": None,
                "lease_until": None,
                "heartbeat_at": None,
                "error": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None,
                "last_published_at": None,
            },
        }
        flt = {"kb_id": kb_id, "type": TASK_TYPE_INGEST, "status": TASK_QUEUED}
        for attempt in range(2):
            try:
                task = await self.tasks.find_one_and_update(
                    flt, update, upsert=True, return_document=ReturnDocument.AFTER
                )
                if task is not None:
                    return task
            except DuplicateKeyError:
                # A concurrent upsert created the queued task first; merge into it.
                if attempt:
                    raise
        raise RuntimeError("kb task upsert returned no document")

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        return await self.tasks.find_one({"_id": task_id})

    async def get_kb_task(self, kb_id: str, task_id: str) -> Optional[dict[str, Any]]:
        return await self.tasks.find_one({"_id": task_id, "kb_id": kb_id})

    async def list_tasks(
        self, kb_id: str, *, active_only: bool = False, limit: int = 20
    ) -> list[dict[str, Any]]:
        flt: dict[str, Any] = {"kb_id": kb_id}
        if active_only:
            flt["status"] = {"$in": list(TASK_ACTIVE_STATUSES)}
        cursor = self.tasks.find(flt).sort("created_at", DESCENDING)
        return await cursor.limit(limit).to_list(length=limit)

    async def active_tasks_for(self, kb_ids: list[str]) -> dict[str, dict[str, Any]]:
        """One active task per KB, preferring the running one."""
        if not kb_ids:
            return {}
        cursor = self.tasks.find(
            {"kb_id": {"$in": kb_ids}, "status": {"$in": list(TASK_ACTIVE_STATUSES)}}
        )
        picked: dict[str, dict[str, Any]] = {}
        for task in await cursor.to_list(length=5000):
            current = picked.get(task["kb_id"])
            if current is None or (
                task["status"] == TASK_RUNNING and current["status"] != TASK_RUNNING
            ):
                picked[task["kb_id"]] = task
        return picked

    async def has_running_task(self, kb_id: str) -> bool:
        return (
            await self.tasks.count_documents({"kb_id": kb_id, "status": TASK_RUNNING}, limit=1) > 0
        )

    async def has_running_for_user(self, created_by: str, now: datetime, exclude_id: str) -> bool:
        count = await self.tasks.count_documents(
            {
                "created_by": created_by,
                "status": TASK_RUNNING,
                "lease_until": {"$gt": now},
                "_id": {"$ne": exclude_id},
            },
            limit=1,
        )
        return count > 0

    async def claim_task(
        self, task_id: str, owner: str, now: datetime, lease_seconds: float
    ) -> Optional[dict[str, Any]]:
        task = await self.tasks.find_one_and_update(
            {"_id": task_id, "status": TASK_QUEUED},
            {
                "$set": {
                    "status": TASK_RUNNING,
                    "lease_owner": owner,
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "heartbeat_at": now,
                    "started_at": now,
                },
                "$inc": {"attempts": 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        if task is None:
            return None
        progress = {"total": len(task.get("doc_ids") or []), "done": 0, "failed": 0}
        await self.tasks.update_one(
            {"_id": task_id, "lease_owner": owner},
            {
                "$set": {
                    "progress.total": progress["total"],
                    "progress.done": 0,
                    "progress.failed": 0,
                    "progress.current_doc_id": None,
                }
            },
        )
        task["progress"] = {**progress, "current_doc_id": None}
        return task

    async def renew_lease(
        self, task_id: str, owner: str, now: datetime, lease_seconds: float
    ) -> Optional[dict[str, Any]]:
        """Extend the lease; ``None`` when the task is no longer ours."""
        return await self.tasks.find_one_and_update(
            {"_id": task_id, "status": TASK_RUNNING, "lease_owner": owner},
            {
                "$set": {
                    "lease_until": now + timedelta(seconds=lease_seconds),
                    "heartbeat_at": now,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def update_progress(
        self, task_id: str, owner: str, *, done: int, failed: int, current_doc_id: str | None
    ) -> bool:
        result = await self.tasks.update_one(
            {"_id": task_id, "status": TASK_RUNNING, "lease_owner": owner},
            {
                "$set": {
                    "progress.done": done,
                    "progress.failed": failed,
                    "progress.current_doc_id": current_doc_id,
                }
            },
        )
        return result.modified_count > 0

    async def finish_task(
        self, task_id: str, owner: str, status: str, error: str | None = None
    ) -> Optional[dict[str, Any]]:
        return await self.tasks.find_one_and_update(
            {"_id": task_id, "status": TASK_RUNNING, "lease_owner": owner},
            {
                "$set": {
                    "status": status,
                    "error": error,
                    "finished_at": utc_now(),
                    "lease_until": None,
                    "progress.current_doc_id": None,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def cancel_queued_task(self, task_id: str) -> Optional[dict[str, Any]]:
        return await self.tasks.find_one_and_update(
            {"_id": task_id, "status": TASK_QUEUED},
            {
                "$set": {
                    "status": TASK_CANCELLED,
                    "cancel_requested": True,
                    "finished_at": utc_now(),
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def request_cancel_running(self, task_id: str) -> Optional[dict[str, Any]]:
        return await self.tasks.find_one_and_update(
            {"_id": task_id, "status": TASK_RUNNING},
            {"$set": {"cancel_requested": True}},
            return_document=ReturnDocument.AFTER,
        )

    async def list_active_tasks_by_kb(self, kb_id: str) -> list[dict[str, Any]]:
        cursor = self.tasks.find({"kb_id": kb_id, "status": {"$in": list(TASK_ACTIVE_STATUSES)}})
        return await cursor.to_list(length=100)

    async def list_expired_running(self, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
        cursor = self.tasks.find({"status": TASK_RUNNING, "lease_until": {"$lt": now}})
        return await cursor.limit(limit).to_list(length=limit)

    async def requeue_expired(self, task: dict[str, Any], now: datetime) -> bool:
        """running(expired) -> queued, guarded on the attempt count we observed."""
        result = await self.tasks.update_one(
            {
                "_id": task["_id"],
                "status": TASK_RUNNING,
                "lease_until": {"$lt": now},
                "attempts": task.get("attempts", 0),
            },
            {
                "$set": {
                    "status": TASK_QUEUED,
                    "lease_owner": None,
                    "lease_until": None,
                    "last_published_at": None,
                }
            },
        )
        return result.modified_count > 0

    async def merge_into_queued(self, kb_id: str, doc_ids: list[str]) -> bool:
        result = await self.tasks.update_one(
            {"kb_id": kb_id, "type": TASK_TYPE_INGEST, "status": TASK_QUEUED},
            {"$addToSet": {"doc_ids": {"$each": doc_ids}}},
        )
        return result.modified_count > 0 or result.matched_count > 0

    async def close_expired(
        self, task: dict[str, Any], status: str, error: str, now: datetime
    ) -> bool:
        result = await self.tasks.update_one(
            {
                "_id": task["_id"],
                "status": TASK_RUNNING,
                "lease_until": {"$lt": now},
                "attempts": task.get("attempts", 0),
            },
            {
                "$set": {
                    "status": status,
                    "error": error,
                    "finished_at": now,
                    "lease_until": None,
                    "progress.current_doc_id": None,
                }
            },
        )
        return result.modified_count > 0

    async def list_stale_queued(self, before: datetime, limit: int = 50) -> list[dict[str, Any]]:
        cursor = self.tasks.find(
            {
                "status": TASK_QUEUED,
                "$or": [{"last_published_at": None}, {"last_published_at": {"$lt": before}}],
            }
        )
        return await cursor.sort("created_at", ASCENDING).limit(limit).to_list(length=limit)

    async def oldest_queued(self, limit: int = 10) -> list[dict[str, Any]]:
        cursor = self.tasks.find({"status": TASK_QUEUED}).sort("created_at", ASCENDING)
        return await cursor.limit(limit).to_list(length=limit)

    async def mark_published(self, task_id: str) -> None:
        await self.tasks.update_one(
            {"_id": task_id, "status": TASK_QUEUED}, {"$set": {"last_published_at": utc_now()}}
        )

    async def kb_ids_over_task_limit(self, keep: int) -> list[str]:
        pipeline = [
            {"$match": {"status": {"$in": list(TASK_TERMINAL_STATUSES)}}},
            {"$group": {"_id": "$kb_id", "n": {"$sum": 1}}},
            {"$match": {"n": {"$gt": keep}}},
        ]
        return [str(row["_id"]) async for row in self.tasks.aggregate(pipeline)]

    async def prune_terminal_tasks(self, kb_id: str, keep: int) -> int:
        cursor = self.tasks.find(
            {"kb_id": kb_id, "status": {"$in": list(TASK_TERMINAL_STATUSES)}}, {"_id": 1}
        ).sort("created_at", DESCENDING)
        stale = await cursor.skip(keep).to_list(length=1000)
        if not stale:
            return 0
        await self.tasks.delete_many({"_id": {"$in": [t["_id"] for t in stale]}})
        return len(stale)

    async def delete_tasks_by_kb(self, kb_id: str) -> None:
        await self.tasks.delete_many({"kb_id": kb_id})


_storage: Optional[KnowledgeBaseStorage] = None


def get_kb_storage() -> KnowledgeBaseStorage:
    global _storage
    if _storage is None:
        _storage = KnowledgeBaseStorage()
    return _storage
