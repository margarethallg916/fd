"""Qdrant wrapper: server mode via QDRANT_URL, embedded local mode otherwise (design §7.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.infra.knowledge_base.sparse import SparseVec


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
    raise NotImplementedError


def point_id_for(chunk_id: str) -> str:
    """Deterministic uuid5(NAMESPACE_URL, f"kb-chunk:{chunk_id}") as str."""
    raise NotImplementedError


class KBVectorStore:
    async def ensure_collection(self, dim: int, model_name: str) -> str:
        """Create the collection (dense COSINE + sparse 'bm25' IDF) if missing; return its name."""
        raise NotImplementedError

    async def upsert_chunks(self, collection: str, points: list[ChunkPoint]) -> None:
        raise NotImplementedError

    async def delete_by_doc(self, collection: str, doc_id: str) -> None:
        raise NotImplementedError

    async def delete_by_kb(self, collection: str, kb_id: str) -> None:
        raise NotImplementedError

    async def search_dense(
        self, collection: str, kb_ids: list[str], vector: list[float], limit: int
    ) -> list[VectorHit]:
        raise NotImplementedError

    async def search_sparse(
        self, collection: str, kb_ids: list[str], vector: SparseVec, limit: int
    ) -> list[VectorHit]:
        """Empty query vector -> []."""
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


def get_vector_store() -> KBVectorStore:
    """Process-wide singleton."""
    raise NotImplementedError


async def close_vector_store() -> None:
    raise NotImplementedError
