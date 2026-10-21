"""Qdrant wrapper: server mode via QDRANT_URL, embedded local mode otherwise (design §7.1)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qdrant_client import AsyncQdrantClient, models

from src.infra.knowledge_base.sparse import SparseVec
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

DENSE_VECTOR_NAME = "dense"
SPARSE_VECTOR_NAME = "bm25"
_UPSERT_BATCH_SIZE = 128
_UPSERT_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class ChunkPoint:
    chunk_id: str  # f"{doc_id}:{index}"
    kb_id: str
    doc_id: str
    index: int
    text: str
    start_line: int | None
    end_line: int | None
    dense: list[float]
    sparse: SparseVec


@dataclass(frozen=True)
class VectorHit:
    chunk_id: str
    kb_id: str
    doc_id: str
    index: int
    text: str
    start_line: int | None
    end_line: int | None
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def collection_name_for(dim: int, model_name: str) -> str:
    """kb_{dim}_{sha1(model_name)[:8]}"""
    digest = hashlib.sha1(model_name.encode("utf-8")).hexdigest()[:8]  # noqa: S324
    return f"kb_{dim}_{digest}"


def point_id_for(chunk_id: str) -> str:
    """Deterministic uuid5(NAMESPACE_URL, f"kb-chunk:{chunk_id}") as str."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kb-chunk:{chunk_id}"))


def _match_filter(key: str, values: list[str]) -> models.Filter:
    return models.Filter(must=[models.FieldCondition(key=key, match=models.MatchAny(any=values))])


def _to_hit(point: Any) -> VectorHit:
    payload = dict(point.payload or {})
    return VectorHit(
        chunk_id=str(payload.get("chunk_id", "")),
        kb_id=str(payload.get("kb_id", "")),
        doc_id=str(payload.get("doc_id", "")),
        index=int(payload.get("index", 0)),
        text=str(payload.get("text", "")),
        start_line=payload.get("start_line"),
        end_line=payload.get("end_line"),
        score=float(point.score),
        payload=payload,
    )


class KBVectorStore:
    def __init__(self) -> None:
        self._client: AsyncQdrantClient | None = None
        self._client_lock = asyncio.Lock()
        self._server_mode = False
        self._ensured: set[str] = set()

    async def _get_client(self) -> AsyncQdrantClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                url = str(getattr(settings, "QDRANT_URL", "") or "").strip()
                if url:
                    api_key = getattr(settings, "QDRANT_API_KEY", "") or None
                    self._client = AsyncQdrantClient(url=url, api_key=api_key)
                    self._server_mode = True
                else:
                    path = Path(getattr(settings, "QDRANT_LOCAL_PATH", "") or "data/qdrant")
                    path.mkdir(parents=True, exist_ok=True)
                    self._client = AsyncQdrantClient(path=str(path))
                    self._server_mode = False
                logger.info(
                    f"[KB] Qdrant client ready ({'server' if self._server_mode else 'local'} mode)"
                )
        return self._client

    async def ensure_collection(self, dim: int, model_name: str) -> str:
        """Create the collection (dense COSINE + sparse 'bm25' IDF) if missing; return its name."""
        name = collection_name_for(dim, model_name)
        if name in self._ensured:
            return name
        client = await self._get_client()
        if not await client.collection_exists(name):
            try:
                await client.create_collection(
                    collection_name=name,
                    vectors_config={
                        DENSE_VECTOR_NAME: models.VectorParams(
                            size=dim, distance=models.Distance.COSINE
                        )
                    },
                    sparse_vectors_config={
                        SPARSE_VECTOR_NAME: models.SparseVectorParams(modifier=models.Modifier.IDF)
                    },
                )
            except Exception:
                # Another worker may have created it concurrently.
                if not await client.collection_exists(name):
                    raise
        if self._server_mode:
            await self._ensure_payload_indexes(client, name)
        self._ensured.add(name)
        return name

    async def _ensure_payload_indexes(self, client: AsyncQdrantClient, name: str) -> None:
        schemas: dict[str, Any] = {
            "kb_id": models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=True
            ),
            "doc_id": models.PayloadSchemaType.KEYWORD,
        }
        for field_name, schema in schemas.items():
            try:
                await client.create_payload_index(
                    collection_name=name, field_name=field_name, field_schema=schema
                )
            except Exception as exc:  # noqa: BLE001 - indexes only affect performance
                logger.warning(f"[KB] Payload index {field_name} on {name} failed: {exc}")

    @staticmethod
    def _to_point(point: ChunkPoint) -> models.PointStruct:
        vector: dict[str, Any] = {DENSE_VECTOR_NAME: point.dense}
        if point.sparse.indices:
            vector[SPARSE_VECTOR_NAME] = models.SparseVector(
                indices=point.sparse.indices, values=point.sparse.values
            )
        return models.PointStruct(
            id=point_id_for(point.chunk_id),
            vector=vector,
            payload={
                "kb_id": point.kb_id,
                "doc_id": point.doc_id,
                "chunk_id": point.chunk_id,
                "index": point.index,
                "text": point.text,
                "start_line": point.start_line,
                "end_line": point.end_line,
            },
        )

    async def upsert_chunks(self, collection: str, points: list[ChunkPoint]) -> None:
        client = await self._get_client()
        for start in range(0, len(points), _UPSERT_BATCH_SIZE):
            batch = [self._to_point(p) for p in points[start : start + _UPSERT_BATCH_SIZE]]
            for attempt in range(_UPSERT_ATTEMPTS):
                try:
                    await client.upsert(collection_name=collection, points=batch, wait=True)
                    break
                except Exception as exc:  # noqa: BLE001 - retried, then re-raised
                    if attempt + 1 >= _UPSERT_ATTEMPTS:
                        raise
                    logger.warning(f"[KB] Qdrant upsert attempt {attempt + 1} failed: {exc}")
                    await asyncio.sleep(_RETRY_BASE_DELAY_SECONDS * (2**attempt))

    async def _delete_by_field(self, collection: str, key: str, value: str) -> None:
        client = await self._get_client()
        selector = models.FilterSelector(
            filter=models.Filter(
                must=[models.FieldCondition(key=key, match=models.MatchValue(value=value))]
            )
        )
        try:
            await client.delete(collection_name=collection, points_selector=selector, wait=True)
        except Exception:
            if await client.collection_exists(collection):
                raise
            logger.info(f"[KB] Collection {collection} is missing; nothing to delete")

    async def delete_by_doc(self, collection: str, doc_id: str) -> None:
        await self._delete_by_field(collection, "doc_id", doc_id)

    async def delete_by_kb(self, collection: str, kb_id: str) -> None:
        await self._delete_by_field(collection, "kb_id", kb_id)

    async def _query(
        self, collection: str, kb_ids: list[str], query: Any, using: str, limit: int
    ) -> list[VectorHit]:
        if not kb_ids or limit <= 0:
            return []
        client = await self._get_client()
        try:
            response = await client.query_points(
                collection_name=collection,
                query=query,
                using=using,
                query_filter=_match_filter("kb_id", kb_ids),
                limit=limit,
                with_payload=True,
            )
        except Exception:
            if await client.collection_exists(collection):
                raise
            return []
        return [_to_hit(point) for point in response.points]

    async def search_dense(
        self, collection: str, kb_ids: list[str], vector: list[float], limit: int
    ) -> list[VectorHit]:
        return await self._query(collection, kb_ids, vector, DENSE_VECTOR_NAME, limit)

    async def search_sparse(
        self, collection: str, kb_ids: list[str], vector: SparseVec, limit: int
    ) -> list[VectorHit]:
        """Empty query vector -> []."""
        if not vector.indices:
            return []
        query = models.SparseVector(indices=vector.indices, values=vector.values)
        return await self._query(collection, kb_ids, query, SPARSE_VECTOR_NAME, limit)

    async def close(self) -> None:
        async with self._client_lock:
            client, self._client = self._client, None
            self._ensured.clear()
        if client is not None:
            await client.close()


_store: KBVectorStore | None = None


def get_vector_store() -> KBVectorStore:
    """Process-wide singleton."""
    global _store
    if _store is None:
        _store = KBVectorStore()
    return _store


async def close_vector_store() -> None:
    global _store
    store, _store = _store, None
    if store is not None:
        await store.close()
