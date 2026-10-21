"""Pure scoring helpers for hybrid retrieval (design §7.4)."""

from __future__ import annotations

from collections.abc import Sequence

from src.infra.knowledge_base.vector_store import VectorHit


def weighted_fusion(
    dense_hits: Sequence[VectorHit],
    sparse_hits: Sequence[VectorHit],
    *,
    vector_weight: float = 0.7,
) -> list[VectorHit]:
    """Merge by chunk_id: score = w*dense + (1-w)*minmax(sparse); missing leg counts 0.

    Returns hits sorted by fused score desc (ties broken by chunk_id for determinism).
    """
    raise NotImplementedError


def apply_threshold(hits: Sequence[VectorHit], threshold: float) -> list[VectorHit]:
    raise NotImplementedError


def apply_rerank_scores(hits: Sequence[VectorHit], scores: Sequence[float]) -> list[VectorHit]:
    """Replace scores with rerank scores and sort desc; len mismatch -> original order."""
    raise NotImplementedError
