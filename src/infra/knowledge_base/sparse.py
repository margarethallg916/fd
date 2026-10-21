"""jieba-based BM25 sparse vectors for Qdrant (design §7.2)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SparseVec:
    indices: list[int]
    values: list[float]


def tokenize(text: str) -> list[str]:
    """jieba.lcut_for_search, lower-cased, punctuation/whitespace/stopwords removed."""
    raise NotImplementedError


def encode_document(text: str) -> SparseVec:
    """BM25 TF-saturated weights (k1=1.2, b=0.75, avgdl=256); IDF is applied by Qdrant."""
    raise NotImplementedError


def encode_query(text: str) -> SparseVec:
    """Unique query tokens with weight 1.0 (may be empty)."""
    raise NotImplementedError


async def warmup() -> None:
    """Load the jieba dictionary off the event loop (idempotent)."""
    raise NotImplementedError
