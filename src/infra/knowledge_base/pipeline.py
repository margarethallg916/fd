"""Single-document processing: parse -> chunk -> embed -> index (design §3).

Every state change is a CAS (``storage.transition_document``); losing it means the document
was deleted or handled elsewhere, in which case this run backs off and cleans up its own output.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.knowledge_base import chunking, embedding, parsing, sparse
from src.infra.knowledge_base import objects as kb_objects
from src.infra.knowledge_base.events import push_document_updated
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.knowledge_base.vector_store import ChunkPoint, get_vector_store
from src.infra.logging import get_logger
from src.infra.utils.datetime import utc_now
from src.kernel.schemas.knowledge_base import (
    DOC_ERROR_INDEXING,
    DOC_ERROR_PARSING,
    DOC_INDEXED,
    DOC_INDEXING,
    DOC_PARSED,
    DOC_PARSING,
    DOC_UPLOADED,
    KB_STATUS_ACTIVE,
)

logger = get_logger(__name__)

OUTCOME_INDEXED = "indexed"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"

PARSE_FAILED_MESSAGE = "文档解析失败，请稍后重试"
INDEX_FAILED_MESSAGE = "文档索引失败，请稍后重试"
MODEL_CHANGED_MESSAGE = "向量模型已变更，请先重建索引"
NO_EMBEDDING_MODEL_MESSAGE = "尚未配置向量模型，请联系管理员"
NO_CONTENT_MESSAGE = "文档没有可索引的内容"
BAD_EMBEDDING_MESSAGE = "向量模型返回结果异常"

# Separators str.splitlines() honours beyond \n; flattening them keeps stored line numbers
# identical for every consumer (chunker, content viewer, citations).
_EXOTIC_LINE_BREAKS = re.compile("[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")


class _IndexFailure(Exception):
    """User-presentable indexing failure (message is shown in the UI)."""


def normalize_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _EXOTIC_LINE_BREAKS.sub("\n", text)


async def process_document(doc_id: str, task_id: str) -> str:
    """Drive one document as far as it can go; returns an ``OUTCOME_*`` constant."""
    storage = get_kb_storage()
    doc = await storage.get_document_by_id(doc_id)
    if doc is None:
        return OUTCOME_SKIPPED

    if doc["status"] == DOC_UPLOADED:
        claimed = await _transition(
            doc, [DOC_UPLOADED], {"status": DOC_PARSING, "processing_task_id": task_id}
        )
        if claimed is None:
            return OUTCOME_SKIPPED
        outcome, parsed_doc = await _parse_stage(claimed)
        if outcome != OUTCOME_INDEXED or parsed_doc is None:
            return outcome
        doc = parsed_doc

    if doc["status"] == DOC_PARSED:
        claimed = await _transition(
            doc, [DOC_PARSED], {"status": DOC_INDEXING, "processing_task_id": task_id}
        )
        if claimed is None:
            return OUTCOME_SKIPPED
        return await _index_stage(claimed)

    return OUTCOME_SKIPPED


async def _transition(
    doc: dict[str, Any], from_statuses: list[str], updates: dict[str, Any]
) -> dict[str, Any] | None:
    updated = await get_kb_storage().transition_document(doc["_id"], from_statuses, updates)
    if updated is not None:
        await push_document_updated(updated["kb_id"], updated["_id"], updated["status"])
    return updated


async def _fail(doc: dict[str, Any], from_status: str, to_status: str, stage: str, message: str):
    await _transition(
        doc,
        [from_status],
        {
            "status": to_status,
            "error": {"stage": stage, "message": message, "at": utc_now()},
        },
    )
    return OUTCOME_FAILED


async def _parse_stage(doc: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """Returns ``(OUTCOME_INDEXED, doc)`` when parsing succeeded (``indexed`` = "continue")."""
    key = kb_objects.markdown_key(doc["kb_id"], doc["_id"])
    try:
        data = await kb_objects.read_object(doc["object_key"])
        parsed = await parsing.parse_document(doc["filename"], data, doc.get("mime"))
        markdown = await run_blocking_io(normalize_markdown, parsed.markdown)
        await kb_objects.put_markdown(key, markdown)
    except asyncio.CancelledError:
        raise
    except parsing.ParseError as exc:
        return await _fail(doc, DOC_PARSING, DOC_ERROR_PARSING, "parse", str(exc)), None
    except Exception:
        logger.exception("[KB] parse failed for document %s", doc["_id"])
        return await _fail(doc, DOC_PARSING, DOC_ERROR_PARSING, "parse", PARSE_FAILED_MESSAGE), None

    updated = await _transition(
        doc,
        [DOC_PARSING],
        {
            "status": DOC_PARSED,
            "markdown_key": key,
            "parse_method": parsed.method,
            "line_count": len(markdown.splitlines()),
            "error": None,
        },
    )
    if updated is None:
        await kb_objects.delete_objects([key])
        return OUTCOME_SKIPPED, None
    return OUTCOME_INDEXED, updated


def _encode_sparse(texts: list[str]) -> list[sparse.SparseVec]:
    return [sparse.encode_document(text) for text in texts]


async def _resolve_collection(kb: dict[str, Any], card: embedding.ModelCard, dim: int) -> str:
    """Lock (or verify) the KB's embedding signature and return its collection name."""
    storage = get_kb_storage()
    store = get_vector_store()
    kb_id = str(kb["_id"])

    def _check(signature: dict[str, Any]) -> None:
        if signature.get("model_name") != card.model_name or int(signature.get("dim", 0)) != dim:
            raise _IndexFailure(MODEL_CHANGED_MESSAGE)

    signature = kb.get("embedding")
    if signature:
        _check(signature)
        return await store.ensure_collection(dim, card.model_name)

    collection = await store.ensure_collection(dim, card.model_name)
    won = await storage.lock_embedding_signature(
        kb_id,
        {
            "model_id": card.model_id,
            "model_name": card.model_name,
            "dim": dim,
            "collection": collection,
        },
    )
    if won:
        return collection
    # Another document of this KB locked the signature first; it must match ours.
    fresh = await storage.get_base(kb_id)
    fresh_signature = (fresh or {}).get("embedding")
    if not fresh_signature:
        raise _IndexFailure(MODEL_CHANGED_MESSAGE)
    _check(fresh_signature)
    return str(fresh_signature["collection"])


async def _index_stage(doc: dict[str, Any]) -> str:
    storage = get_kb_storage()
    store = get_vector_store()
    doc_id, kb_id = doc["_id"], doc["kb_id"]

    kb = await storage.get_base(kb_id)
    if kb is None or kb.get("status") != KB_STATUS_ACTIVE:
        return OUTCOME_SKIPPED

    collection = ""
    try:
        markdown = await kb_objects.read_markdown(doc["markdown_key"])
        chunks = await run_blocking_io(
            lambda: chunking.chunk_markdown(
                markdown,
                filename=doc["filename"],
                preset=kb.get("chunk_preset"),
                chunk_token_num=kb.get("chunk_token_num"),
                overlap_percent=kb.get("overlap_percent"),
            )
        )
        if not chunks:
            raise _IndexFailure(NO_CONTENT_MESSAGE)

        card = await embedding.resolve_embedding_card()
        if card is None:
            raise embedding.EmbeddingError(NO_EMBEDDING_MODEL_MESSAGE)
        texts = [chunk.text for chunk in chunks]
        vectors = await embedding.embed_texts(texts, card)
        if len(vectors) != len(texts) or not vectors[0]:
            raise embedding.EmbeddingError(BAD_EMBEDDING_MESSAGE)

        collection = await _resolve_collection(kb, card, len(vectors[0]))
        sparse_vectors = await run_blocking_io(_encode_sparse, texts)

        await storage.replace_chunks(
            kb_id,
            doc_id,
            [
                {
                    "_id": f"{doc_id}:{chunk.index}",
                    "kb_id": kb_id,
                    "doc_id": doc_id,
                    "index": chunk.index,
                    "text": chunk.text,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "token_count": chunk.token_count,
                }
                for chunk in chunks
            ],
        )
        points = [
            ChunkPoint(
                chunk_id=f"{doc_id}:{chunk.index}",
                kb_id=kb_id,
                doc_id=doc_id,
                index=chunk.index,
                text=chunk.text,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                dense=vector,
                sparse=sparse_vec,
            )
            for chunk, vector, sparse_vec in zip(chunks, vectors, sparse_vectors, strict=True)
        ]
        await store.delete_by_doc(collection, doc_id)
        await store.upsert_chunks(collection, points)
    except asyncio.CancelledError:
        raise
    except (embedding.EmbeddingError, _IndexFailure) as exc:
        return await _fail(doc, DOC_INDEXING, DOC_ERROR_INDEXING, "index", str(exc))
    except Exception:
        logger.exception("[KB] index failed for document %s", doc_id)
        return await _fail(doc, DOC_INDEXING, DOC_ERROR_INDEXING, "index", INDEX_FAILED_MESSAGE)

    updated = await _transition(
        doc,
        [DOC_INDEXING],
        {
            "status": DOC_INDEXED,
            "chunk_count": len(chunks),
            "token_count": sum(chunk.token_count for chunk in chunks),
            "indexed_at": utc_now(),
            "error": None,
        },
    )
    if updated is None:
        # Deleted (or reset) while we were writing vectors: do not leave orphans behind.
        await _discard_index_output(storage, store, collection, doc_id)
        return OUTCOME_SKIPPED
    return OUTCOME_INDEXED


async def _discard_index_output(storage: Any, store: Any, collection: str, doc_id: str) -> None:
    try:
        await store.delete_by_doc(collection, doc_id)
        await storage.delete_chunks_by_docs([doc_id])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] cleanup after lost CAS for %s failed: %s", doc_id, exc)
