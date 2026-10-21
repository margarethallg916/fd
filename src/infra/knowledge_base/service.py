"""Knowledge base business logic: CRUD, upload, retry/reindex, delete and retrieval."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from src.infra.async_utils import run_blocking_io
from src.infra.knowledge_base import (
    chunking,
    embedding,
    fusion,
    parsing,
    sparse,
    vector_store,
)
from src.infra.knowledge_base import objects as kb_objects
from src.infra.knowledge_base import queue as kb_queue
from src.infra.knowledge_base.access import require_kb_access
from src.infra.knowledge_base.errors import (
    KBBusyError,
    KBDisabledError,
    KBDocumentNotFoundError,
    KBNotFoundError,
    KBTaskNotFoundError,
    KBValidationError,
)
from src.infra.knowledge_base.events import push_document_updated
from src.infra.knowledge_base.storage import (
    KnowledgeBaseStorage,
    expand_status_filter,
    get_kb_storage,
)
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.knowledge_base import (
    DEFAULT_KB_ICON,
    DOC_ERROR_INDEXING,
    DOC_ERROR_PARSING,
    DOC_INDEXED,
    DOC_PARSED,
    DOC_UPLOADED,
    KB_ROLE_OWNER,
    KB_STATUS_ACTIVE,
    KBChunk,
    KBChunkListResponse,
    KBConfig,
    KBContentLine,
    KBContentResponse,
    KBCreate,
    KBDeleteDocumentsResponse,
    KBDocument,
    KBDocumentListResponse,
    KBPreset,
    KBSearchHit,
    KBSearchRequest,
    KBSearchResponse,
    KBStats,
    KBTask,
    KBTaskEnqueueResponse,
    KBTaskListResponse,
    KBUpdate,
    KBUploadResponse,
    KnowledgeBase,
)
from src.kernel.schemas.user import TokenPayload

logger = get_logger(__name__)

NAME_MAX = 64
DESCRIPTION_MAX = 500
QUERY_MAX = 2000
DELETE_DOCS_MAX = 500
UPLOAD_OVERHEAD_SLACK = 1024 * 1024
SEARCH_MODES = ("hybrid", "vector", "keyword")
EMBEDDING_MISSING = "embedding_model_missing"
_ICON_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_cleanup_tasks: set[asyncio.Task[Any]] = set()
_cleaning: set[str] = set()


def ensure_enabled() -> None:
    if not getattr(settings, "KB_ENABLED", True):
        raise KBDisabledError()


async def _current_embedding_card() -> embedding.ModelCard | None:
    try:
        return await embedding.resolve_embedding_card()
    except Exception as exc:  # noqa: BLE001 - a broken model card must not break listings
        logger.warning("[KB] resolving embedding card failed: %s", exc)
        return None


async def _current_rerank_card() -> embedding.ModelCard | None:
    try:
        return await embedding.resolve_rerank_card()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] resolving rerank card failed: %s", exc)
        return None


# ── validation ───────────────────────────────────────────────


def _clean_name(value: str | None) -> str:
    name = (value or "").strip()
    if not 1 <= len(name) <= NAME_MAX:
        raise KBValidationError(f"知识库名称需为 1-{NAME_MAX} 个字符")
    return name


def _clean_description(value: str | None) -> str:
    text = (value or "").strip()
    if len(text) > DESCRIPTION_MAX:
        raise KBValidationError(f"描述不能超过 {DESCRIPTION_MAX} 个字符")
    return text


def _clean_icon(value: str | None) -> str:
    icon = (value or "").strip() or DEFAULT_KB_ICON
    if not _ICON_PATTERN.match(icon):
        raise KBValidationError("图标名称不合法")
    return icon


def _check_chunk_settings(
    preset: str | None, token_num: int | None, overlap: int | None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if preset is not None:
        if preset not in chunking.CHUNK_PRESETS:
            raise KBValidationError("不支持的分块预设")
        out["chunk_preset"] = preset
    if token_num is not None:
        if not chunking.MIN_CHUNK_TOKEN_NUM <= token_num <= chunking.MAX_CHUNK_TOKEN_NUM:
            raise KBValidationError(
                f"分块大小需在 {chunking.MIN_CHUNK_TOKEN_NUM}-{chunking.MAX_CHUNK_TOKEN_NUM} 之间"
            )
        out["chunk_token_num"] = token_num
    if overlap is not None:
        if not 0 <= overlap <= chunking.MAX_OVERLAP_PERCENT:
            raise KBValidationError(f"重叠比例需在 0-{chunking.MAX_OVERLAP_PERCENT} 之间")
        out["overlap_percent"] = overlap
    return out


def _clamp(value: int, low: int, high: int) -> int:
    return min(max(int(value), low), high)


# ── response assembly ────────────────────────────────────────


def _to_document(doc: dict[str, Any]) -> KBDocument:
    return KBDocument.model_validate(doc)


def _to_task(task: dict[str, Any]) -> KBTask:
    return KBTask.model_validate(task)


async def build_kb_responses(items: list[tuple[dict[str, Any], str]]) -> list[KnowledgeBase]:
    """Attach live stats, the active task and the index-outdated flag to KB rows."""
    if not items:
        return []
    storage = get_kb_storage()
    ids = [str(kb["_id"]) for kb, _ in items]
    stats = await storage.stats_for(ids)
    tasks = await storage.active_tasks_for(ids)
    card = await _current_embedding_card()

    responses: list[KnowledgeBase] = []
    for kb, role in items:
        emb = kb.get("embedding") or None
        outdated = bool(emb) and (card is None or card.model_name != emb.get("model_name"))
        active = tasks.get(str(kb["_id"]))
        responses.append(
            KnowledgeBase.model_validate(
                {
                    **kb,
                    "my_role": role,
                    "embedding": (
                        {"model_name": emb.get("model_name"), "dim": emb.get("dim")}
                        if emb
                        else None
                    ),
                    "index_outdated": outdated,
                    "stats": KBStats(**stats[str(kb["_id"])]),
                    "active_task": _to_task(active) if active else None,
                }
            )
        )
    return responses


# ── configuration ────────────────────────────────────────────


async def get_config() -> KBConfig:
    enabled = bool(getattr(settings, "KB_ENABLED", True))
    ready = False
    reason: str | None = None
    rerank_available = False
    if enabled:
        if await _current_embedding_card() is None:
            reason = EMBEDDING_MISSING
        else:
            ready = True
        rerank_available = await _current_rerank_card() is not None
    return KBConfig(
        enabled=enabled,
        ready=ready,
        not_ready_reason=reason,
        presets=[KBPreset(key=key, **values) for key, values in chunking.CHUNK_PRESETS.items()],
        allowed_extensions=sorted(parsing.SUPPORTED_EXTENSIONS),
        mineru_enabled=parsing.mineru_available(),
        max_file_mb=int(settings.KB_MAX_FILE_MB),
        rerank_available=rerank_available,
    )


# ── knowledge bases ──────────────────────────────────────────


async def create_kb(user: TokenPayload, body: KBCreate) -> KnowledgeBase:
    ensure_enabled()
    name = _clean_name(body.name)
    description = _clean_description(body.description)
    icon = _clean_icon(body.icon)
    preset = body.chunk_preset or chunking.DEFAULT_CHUNK_PRESET
    defaults = chunking.CHUNK_PRESETS.get(preset, {})
    chunk = _check_chunk_settings(
        preset,
        body.chunk_token_num
        if body.chunk_token_num is not None
        else defaults.get("chunk_token_num", chunking.DEFAULT_CHUNK_TOKEN_NUM),
        body.overlap_percent
        if body.overlap_percent is not None
        else defaults.get("overlap_percent", chunking.DEFAULT_OVERLAP_PERCENT),
    )

    now = utc_now()
    kb_id = uuid4().hex
    kb_doc = {
        "_id": kb_id,
        "owner_id": user.sub,
        "owner_username": user.username,
        "name": name,
        "description": description,
        "icon": icon,
        **chunk,
        "embedding": None,
        "visibility": "private",
        "graph_enabled": False,
        "status": KB_STATUS_ACTIVE,
        "created_at": now,
        "updated_at": now,
    }
    storage = get_kb_storage()
    await storage.insert_base(kb_doc)
    try:
        await storage.insert_member(
            {
                "_id": uuid4().hex,
                "kb_id": kb_id,
                "user_id": user.sub,
                "username": user.username,
                "role": KB_ROLE_OWNER,
                "chat_enabled": True,
                "granted_by": user.sub,
                "created_at": now,
            }
        )
    except Exception:
        await storage.delete_base(kb_id)
        raise
    return (await build_kb_responses([(kb_doc, KB_ROLE_OWNER)]))[0]


async def list_kbs(user: TokenPayload, scope: str = "mine") -> list[KnowledgeBase]:
    ensure_enabled()
    if scope != "mine":
        raise KBValidationError("不支持的 scope")
    storage = get_kb_storage()
    kb_ids = await storage.list_member_kb_ids(user.sub, [KB_ROLE_OWNER])
    bases = await storage.list_bases(kb_ids)
    return await build_kb_responses([(kb, KB_ROLE_OWNER) for kb in bases])


async def get_kb(user: TokenPayload, kb_id: str) -> KnowledgeBase:
    ensure_enabled()
    access = await require_kb_access(user.sub, kb_id)
    return (await build_kb_responses([(access.kb, access.role)]))[0]


async def update_kb(user: TokenPayload, kb_id: str, body: KBUpdate) -> KnowledgeBase:
    ensure_enabled()
    access = await require_kb_access(user.sub, kb_id, manage=True)
    updates: dict[str, Any] = {}
    if body.name is not None:
        updates["name"] = _clean_name(body.name)
    if body.description is not None:
        updates["description"] = _clean_description(body.description)
    if body.icon is not None:
        updates["icon"] = _clean_icon(body.icon)
    updates.update(
        _check_chunk_settings(body.chunk_preset, body.chunk_token_num, body.overlap_percent)
    )
    if updates:
        updated = await get_kb_storage().update_base(kb_id, updates)
        if updated is None:
            raise KBNotFoundError()
    else:
        updated = access.kb
    return (await build_kb_responses([(updated, access.role)]))[0]


async def delete_kb(user: TokenPayload, kb_id: str) -> None:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id, manage=True)
    storage = get_kb_storage()
    if not await storage.mark_base_deleting(kb_id):
        raise KBNotFoundError()
    await kb_queue.cancel_kb_tasks(kb_id)
    task = asyncio.create_task(cleanup_kb(kb_id), name=f"kb-cleanup:{kb_id}")
    _cleanup_tasks.add(task)
    task.add_done_callback(_cleanup_tasks.discard)


async def cleanup_kb(kb_id: str) -> bool:
    """Remove everything a ``deleting`` KB owns. Returns False when it must be retried later."""
    if kb_id in _cleaning:
        return False
    _cleaning.add(kb_id)
    storage = get_kb_storage()
    try:
        kb = await storage.get_base(kb_id)
        await kb_queue.cancel_kb_tasks(kb_id)
        if await storage.has_running_task(kb_id):
            return False  # let the in-flight document finish; the reconciler comes back

        for _ in range(10_000):
            docs = await storage.list_documents_for_cleanup(kb_id, 200)
            if not docs:
                break
            ids = [d["_id"] for d in docs]
            await kb_objects.delete_objects(
                [key for d in docs for key in (d.get("object_key"), d.get("markdown_key"))]
            )
            await storage.delete_chunks_by_docs(ids)
            await storage.delete_documents(kb_id, ids)

        collection = ((kb or {}).get("embedding") or {}).get("collection")
        if collection:
            await vector_store.get_vector_store().delete_by_kb(collection, kb_id)
        await storage.delete_chunks_by_kb(kb_id)
        await kb_objects.delete_prefix(f"kb/{kb_id}/")
        await storage.delete_tasks_by_kb(kb_id)
        await storage.delete_members_by_kb(kb_id)
        await storage.delete_base(kb_id)
        logger.info("[KB] knowledge base %s removed", kb_id)
        return True
    except Exception:
        logger.exception("[KB] cleanup of knowledge base %s failed; will retry", kb_id)
        return False
    finally:
        _cleaning.discard(kb_id)


async def drain_cleanup_tasks() -> None:
    tasks = list(_cleanup_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── documents ────────────────────────────────────────────────


async def list_documents(
    user: TokenPayload,
    kb_id: str,
    *,
    status: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> KBDocumentListResponse:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    page = max(int(page), 1)
    page_size = _clamp(page_size, 1, 200)
    items, total = await get_kb_storage().list_documents(
        kb_id,
        statuses=expand_status_filter(status),
        query=q,
        page=page,
        page_size=page_size,
    )
    return KBDocumentListResponse(
        items=[_to_document(d) for d in items], total=total, page=page, page_size=page_size
    )


async def _require_document(
    storage: KnowledgeBaseStorage, kb_id: str, doc_id: str
) -> dict[str, Any]:
    doc = await storage.get_document(kb_id, doc_id)
    if doc is None:
        raise KBDocumentNotFoundError()
    return doc


async def get_document(user: TokenPayload, kb_id: str, doc_id: str) -> KBDocument:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    return _to_document(await _require_document(get_kb_storage(), kb_id, doc_id))


def _safe_filename(raw: str | None) -> str:
    name = str(raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:255]


async def upload_document(
    user: TokenPayload,
    kb_id: str,
    file: Any,
    *,
    content_length: int | None = None,
) -> tuple[KBUploadResponse, bool]:
    """Stream one upload into object storage and queue it. Returns ``(response, created)``."""
    ensure_enabled()
    await require_kb_access(user.sub, kb_id, write=True)

    filename = _safe_filename(getattr(file, "filename", None))
    if not filename or not parsing.is_supported_filename(filename):
        raise KBValidationError("不支持的文件类型")

    max_mb = int(settings.KB_MAX_FILE_MB)
    max_bytes = max_mb * 1024 * 1024
    if content_length is not None and content_length > max_bytes + UPLOAD_OVERHEAD_SLACK:
        raise KBValidationError(f"文件大小超过 {max_mb}MB 限制")

    spooled = await kb_objects.spool_upload(file, max_size_bytes=max_bytes, max_size_mb=max_mb)
    try:
        doc, created = await _store_upload(user, kb_id, file, filename, spooled)
    finally:
        spooled.close()
    if not created:
        return KBUploadResponse(document=_to_document(doc), duplicate=True), False

    # The KB may have been deleted while the bytes were streaming.
    storage = get_kb_storage()
    fresh = await storage.get_base(kb_id)
    if fresh is None or fresh.get("status") != KB_STATUS_ACTIVE:
        await storage.delete_documents(kb_id, [doc["_id"]])
        await kb_objects.delete_objects([doc["object_key"]])
        raise KBNotFoundError()

    try:
        await kb_queue.enqueue_documents(kb_id, [doc["_id"]], user.sub)
    except Exception:
        # The document stays ``uploaded``; the reconciler enqueues stragglers.
        logger.exception("[KB] enqueue after upload failed for document %s", doc["_id"])
    await push_document_updated(kb_id, doc["_id"], DOC_UPLOADED)
    return KBUploadResponse(document=_to_document(doc), duplicate=False), True


async def _store_upload(
    user: TokenPayload,
    kb_id: str,
    file: Any,
    filename: str,
    spooled: kb_objects.SpooledUpload,
) -> tuple[dict[str, Any], bool]:
    """Dedupe on sha256, write the object and insert the row. ``created`` is False for dupes."""
    storage = get_kb_storage()
    if spooled.size == 0:
        raise KBValidationError("文件内容为空")
    existing = await storage.get_document_by_sha(kb_id, spooled.sha256_hex)
    if existing is not None:
        return existing, False

    ext = parsing.normalize_extension(filename)
    doc_id = uuid4().hex
    mime = getattr(file, "content_type", None) or mimetypes.guess_type(filename)[0] or ""
    key = kb_objects.original_key(kb_id, doc_id, ext)
    await kb_objects.put_original(key, spooled, mime or None)

    now = utc_now()
    doc = {
        "_id": doc_id,
        "kb_id": kb_id,
        "uploader_id": user.sub,
        "uploader_username": user.username,
        "filename": filename,
        "ext": ext,
        "mime": mime,
        "size": spooled.size,
        "sha256": spooled.sha256_hex,
        "source": "upload",
        "source_ref": None,
        "object_key": key,
        "markdown_key": None,
        "status": DOC_UPLOADED,
        "error": None,
        "parse_method": None,
        "chunk_count": 0,
        "token_count": 0,
        "line_count": 0,
        "processing_task_id": None,
        "created_at": now,
        "updated_at": now,
        "indexed_at": None,
    }
    try:
        await storage.insert_document(doc)
    except DuplicateKeyError:
        await kb_objects.delete_objects([key])
        existing = await storage.get_document_by_sha(kb_id, spooled.sha256_hex)
        if existing is None:
            raise
        return existing, False
    except Exception:
        await kb_objects.delete_objects([key])
        raise
    return doc, True


async def retry_documents(
    user: TokenPayload, kb_id: str, doc_ids: list[str]
) -> KBTaskEnqueueResponse:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id, write=True)
    storage = get_kb_storage()
    failed = [DOC_ERROR_PARSING, DOC_ERROR_INDEXING]
    if doc_ids:
        docs = [
            d for d in await storage.list_documents_by_ids(kb_id, doc_ids) if d["status"] in failed
        ]
    else:
        docs = await storage.list_documents_by_status(kb_id, failed)

    moved: list[str] = []
    for doc in docs:
        target = DOC_UPLOADED if doc["status"] == DOC_ERROR_PARSING else DOC_PARSED
        updated = await storage.transition_document(
            doc["_id"],
            [doc["status"]],
            {"status": target, "error": None, "processing_task_id": None},
        )
        if updated is not None:
            moved.append(doc["_id"])
            await push_document_updated(kb_id, doc["_id"], target)
    return await _enqueue(kb_id, moved, user.sub)


async def _enqueue(kb_id: str, doc_ids: list[str], user_id: str) -> KBTaskEnqueueResponse:
    if not doc_ids:
        return KBTaskEnqueueResponse(task_id=None, count=0)
    task = await kb_queue.enqueue_documents(kb_id, doc_ids, user_id)
    return KBTaskEnqueueResponse(task_id=task["_id"], count=len(doc_ids))


async def reindex_kb(user: TokenPayload, kb_id: str) -> KBTaskEnqueueResponse:
    """Rebuild every indexed document with the current model and chunk settings."""
    ensure_enabled()
    access = await require_kb_access(user.sub, kb_id, manage=True)
    storage = get_kb_storage()
    if await storage.has_running_task(kb_id):
        raise KBBusyError()

    collection = (access.kb.get("embedding") or {}).get("collection")
    if collection:
        try:
            await vector_store.get_vector_store().delete_by_kb(collection, kb_id)
        except Exception:
            logger.exception("[KB] clearing vectors of %s before reindex failed", kb_id)
            raise
    await storage.clear_embedding_signature(kb_id)

    moved: list[str] = []
    seen: set[str] = set()
    rebuildable = [DOC_INDEXED, DOC_ERROR_INDEXING]
    while True:
        batch = [
            d
            for d in await storage.list_documents_by_status(kb_id, rebuildable, limit=500)
            if d["_id"] not in seen
        ]
        if not batch:
            break
        for doc in batch:
            seen.add(doc["_id"])
            updated = await storage.transition_document(
                doc["_id"],
                rebuildable,
                {"status": DOC_PARSED, "error": None, "processing_task_id": None},
            )
            if updated is not None:
                moved.append(doc["_id"])
                await push_document_updated(kb_id, doc["_id"], DOC_PARSED)
    return await _enqueue(kb_id, moved, user.sub)


async def delete_documents(
    user: TokenPayload, kb_id: str, doc_ids: list[str]
) -> KBDeleteDocumentsResponse:
    ensure_enabled()
    access = await require_kb_access(user.sub, kb_id, write=True)
    ids = list(dict.fromkeys(str(i) for i in doc_ids))
    if not 1 <= len(ids) <= DELETE_DOCS_MAX:
        raise KBValidationError(f"请选择 1-{DELETE_DOCS_MAX} 个文档")
    storage = get_kb_storage()
    # Row first: an in-flight pipeline then loses its CAS and cleans up after itself.
    docs = await storage.delete_documents(kb_id, ids)
    removed = [d["_id"] for d in docs]
    await storage.delete_chunks_by_docs(removed)
    collection = (access.kb.get("embedding") or {}).get("collection")
    if collection:
        store = vector_store.get_vector_store()
        for doc_id in removed:
            try:
                await store.delete_by_doc(collection, doc_id)
            except Exception as exc:  # noqa: BLE001 - search drops hits of missing documents
                logger.warning("[KB] deleting vectors of %s failed: %s", doc_id, exc)
    await kb_objects.delete_objects(
        [key for d in docs for key in (d.get("object_key"), d.get("markdown_key"))]
    )
    for doc_id in removed:
        await push_document_updated(kb_id, doc_id, "deleted")
    return KBDeleteDocumentsResponse(deleted=len(removed))


async def get_content(
    user: TokenPayload, kb_id: str, doc_id: str, *, offset: int = 1, limit: int = 400
) -> KBContentResponse:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    doc = await _require_document(get_kb_storage(), kb_id, doc_id)
    key = doc.get("markdown_key")
    if not key:
        raise KBValidationError("文档尚未解析完成")
    offset = max(int(offset), 1)
    limit = _clamp(limit, 1, 2000)
    markdown = await kb_objects.read_markdown(key)
    lines = markdown.splitlines()
    window = lines[offset - 1 : offset - 1 + limit]
    return KBContentResponse(
        doc_id=doc_id,
        filename=doc["filename"],
        total_lines=len(lines),
        offset=offset,
        lines=[KBContentLine(n=offset + i, text=text) for i, text in enumerate(window)],
        has_more=offset - 1 + limit < len(lines),
    )


async def list_chunks(
    user: TokenPayload, kb_id: str, doc_id: str, *, page: int = 1, page_size: int = 20
) -> KBChunkListResponse:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    storage = get_kb_storage()
    await _require_document(storage, kb_id, doc_id)
    page = max(int(page), 1)
    page_size = _clamp(page_size, 1, 100)
    items, total = await storage.list_chunks(doc_id, page=page, page_size=page_size)
    return KBChunkListResponse(
        items=[KBChunk.model_validate(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


async def open_download(
    user: TokenPayload, kb_id: str, doc_id: str
) -> tuple[dict[str, Any], AsyncIterator[bytes]]:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    doc = await _require_document(get_kb_storage(), kb_id, doc_id)
    key = doc.get("object_key")
    if not key or not await kb_objects.object_exists(key):
        raise KBDocumentNotFoundError("文件不存在")
    return doc, kb_objects.stream_object(key)


# ── tasks ────────────────────────────────────────────────────


async def list_tasks(user: TokenPayload, kb_id: str, *, active: bool = False) -> KBTaskListResponse:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id)
    tasks = await get_kb_storage().list_tasks(kb_id, active_only=active)
    return KBTaskListResponse(items=[_to_task(t) for t in tasks])


async def cancel_task(user: TokenPayload, kb_id: str, task_id: str) -> KBTask:
    ensure_enabled()
    await require_kb_access(user.sub, kb_id, write=True)
    task = await kb_queue.cancel_task(kb_id, task_id)
    if task is None:
        raise KBTaskNotFoundError()
    return _to_task(task)


# ── retrieval ────────────────────────────────────────────────


@dataclass
class _Group:
    collection: str
    kb_ids: list[str]
    model_name: str
    dim: int
    fallback: str | None = None  # None | "index_outdated" | "embedding_failed"


def _validate_search(req: KBSearchRequest) -> tuple[str, int, str, float, float]:
    query = (req.query or "").strip()
    if not query:
        raise KBValidationError("请输入检索内容")
    if len(query) > QUERY_MAX:
        raise KBValidationError(f"检索内容不能超过 {QUERY_MAX} 个字符")
    if not 1 <= req.top_k <= 50:
        raise KBValidationError("top_k 需在 1-50 之间")
    if req.mode not in SEARCH_MODES:
        raise KBValidationError("不支持的检索模式")
    if not 0.0 <= req.score_threshold <= 1.0:
        raise KBValidationError("相似度阈值需在 0-1 之间")
    if not 0.0 <= req.vector_weight <= 1.0:
        raise KBValidationError("向量权重需在 0-1 之间")
    return query, req.top_k, req.mode, req.score_threshold, req.vector_weight


async def search_bases(
    bases: list[dict[str, Any]], req: KBSearchRequest
) -> tuple[list[KBSearchHit], list[str]]:
    """Hybrid retrieval over already-authorised KB rows (design §7.4)."""
    query, top_k, mode, threshold, weight = _validate_search(req)
    indexed = [kb for kb in bases if kb.get("embedding")]
    if not indexed:
        return [], []

    store = vector_store.get_vector_store()
    warnings: list[str] = []
    groups: dict[str, _Group] = {}
    for kb in indexed:
        emb = kb["embedding"]
        group = groups.setdefault(
            emb["collection"],
            _Group(
                collection=emb["collection"],
                kb_ids=[],
                model_name=str(emb.get("model_name") or ""),
                dim=int(emb.get("dim") or 0),
            ),
        )
        group.kb_ids.append(str(kb["_id"]))

    rerank_card = None
    if req.use_rerank is not False:
        rerank_card = await _current_rerank_card()
        if req.use_rerank is True and rerank_card is None:
            warnings.append("rerank_unavailable")
    recall = max(50, top_k) if rerank_card is not None else top_k * 2

    query_vec: list[float] | None = None
    if mode != "keyword":
        card = await _current_embedding_card()
        matching = card is not None and any(
            g.model_name == card.model_name for g in groups.values()
        )
        if matching and card is not None:
            try:
                query_vec = (await embedding.embed_texts([query], card))[0]
            except embedding.EmbeddingError as exc:
                logger.warning("[KB] query embedding failed, using keyword search: %s", exc)
                warnings.append("embedding_failed")
        for group in groups.values():
            if card is None or card.model_name != group.model_name:
                group.fallback = "index_outdated"
            elif query_vec is None:
                group.fallback = group.fallback or "embedding_failed"
            elif len(query_vec) != group.dim:
                group.fallback = "index_outdated"
        if any(g.fallback == "index_outdated" for g in groups.values()):
            warnings.append("index_outdated")

    sparse_query = None
    if mode != "vector" or any(g.fallback for g in groups.values()):
        sparse_query = await run_blocking_io(sparse.encode_query, query)

    async def _one(group: _Group) -> list[vector_store.VectorHit]:
        effective = "keyword" if group.fallback else mode
        dense_hits: list[vector_store.VectorHit] = []
        sparse_hits: list[vector_store.VectorHit] = []
        jobs = []
        if effective in ("hybrid", "vector") and query_vec is not None:
            jobs.append(store.search_dense(group.collection, group.kb_ids, query_vec, recall))
        if effective in ("hybrid", "keyword") and sparse_query is not None:
            jobs.append(store.search_sparse(group.collection, group.kb_ids, sparse_query, recall))
        results = await asyncio.gather(*jobs)
        iterator = iter(results)
        if effective in ("hybrid", "vector"):
            dense_hits = list(next(iterator))
        if effective in ("hybrid", "keyword"):
            sparse_hits = list(next(iterator))
        if effective == "vector":
            hits = dense_hits
        elif effective == "keyword":
            # vector_weight=0 turns the fusion into min-max normalised keyword scores in [0, 1].
            hits = fusion.weighted_fusion([], sparse_hits, vector_weight=0.0)
        else:
            hits = fusion.weighted_fusion(dense_hits, sparse_hits, vector_weight=weight)
        if effective != "keyword" and threshold > 0:
            hits = fusion.apply_threshold(hits, threshold)
        return list(hits)

    merged: list[vector_store.VectorHit] = []
    for hits in await asyncio.gather(*(_one(g) for g in groups.values())):
        merged.extend(hits)
    merged.sort(key=lambda h: (-h.score, h.chunk_id))
    candidates = merged[:recall]

    fused_scores = {h.chunk_id: h.score for h in candidates}
    reranked = False
    if rerank_card is not None and candidates:
        scores = await embedding.rerank(query, [h.text for h in candidates], rerank_card)
        if scores is not None and len(scores) == len(candidates):
            candidates = list(fusion.apply_rerank_scores(candidates, scores))
            reranked = True
        else:
            warnings.append("rerank_failed")

    briefs = await get_kb_storage().document_briefs(list({h.doc_id for h in candidates}))
    names = {str(kb["_id"]): str(kb.get("name") or "") for kb in indexed}
    results: list[KBSearchHit] = []
    for hit in candidates:
        brief = briefs.get(hit.doc_id)
        if brief is None or brief.get("kb_id") != hit.kb_id:
            continue  # document deleted while its vectors were still around
        results.append(
            KBSearchHit(
                chunk_id=hit.chunk_id,
                kb_id=hit.kb_id,
                kb_name=names.get(hit.kb_id, ""),
                doc_id=hit.doc_id,
                filename=str(brief.get("filename") or ""),
                index=hit.index,
                text=hit.text,
                start_line=hit.start_line,
                end_line=hit.end_line,
                score=fused_scores.get(hit.chunk_id, hit.score) if reranked else hit.score,
                rerank_score=hit.score if reranked else None,
            )
        )
        if len(results) >= top_k:
            break
    return results, warnings


async def search(user: TokenPayload, kb_id: str, req: KBSearchRequest) -> KBSearchResponse:
    ensure_enabled()
    access = await require_kb_access(user.sub, kb_id)
    started = time.perf_counter()
    results, warnings = await search_bases([access.kb], req)
    return KBSearchResponse(
        results=results,
        warnings=warnings,
        took_ms=int((time.perf_counter() - started) * 1000),
    )
