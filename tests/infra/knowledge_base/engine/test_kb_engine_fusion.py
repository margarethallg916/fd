"""Hybrid score fusion, thresholds and rerank merge (design §7.4)."""

from __future__ import annotations

import pytest

from src.infra.knowledge_base.fusion import apply_rerank_scores, apply_threshold, weighted_fusion
from src.infra.knowledge_base.vector_store import VectorHit


def hit(chunk_id: str, score: float, text: str = "") -> VectorHit:
    return VectorHit(
        chunk_id=chunk_id,
        kb_id="kb",
        doc_id=chunk_id.split(":")[0],
        index=int(chunk_id.split(":")[1]),
        text=text or chunk_id,
        start_line=1,
        end_line=2,
        score=score,
        payload={"chunk_id": chunk_id},
    )


def scores(hits: list[VectorHit]) -> dict[str, float]:
    return {h.chunk_id: h.score for h in hits}


def test_fusion_combines_dense_and_minmax_sparse() -> None:
    dense = [hit("d:0", 0.9), hit("d:1", 0.5)]
    sparse = [hit("d:1", 8.0), hit("d:2", 4.0), hit("d:3", 2.0)]
    fused = weighted_fusion(dense, sparse)
    assert scores(fused) == pytest.approx(
        {
            "d:0": 0.7 * 0.9,  # sparse leg missing -> 0
            "d:1": 0.7 * 0.5 + 0.3 * 1.0,  # sparse max -> 1.0
            "d:2": 0.3 * (2.0 / 6.0),  # dense leg missing -> 0
            "d:3": 0.0,  # sparse min -> 0
        }
    )
    assert [h.chunk_id for h in fused] == ["d:1", "d:0", "d:2", "d:3"]


def test_fusion_respects_vector_weight() -> None:
    dense = [hit("d:0", 0.8)]
    sparse = [hit("d:0", 3.0), hit("d:1", 1.0)]
    assert scores(weighted_fusion(dense, sparse, vector_weight=1.0))["d:0"] == pytest.approx(0.8)
    assert scores(weighted_fusion(dense, sparse, vector_weight=0.0))["d:0"] == pytest.approx(1.0)
    clamped = weighted_fusion(dense, sparse, vector_weight=5)
    assert scores(clamped)["d:0"] == pytest.approx(0.8)


def test_fusion_single_leg_inputs() -> None:
    assert weighted_fusion([], []) == []
    only_dense = weighted_fusion([hit("d:0", 0.6)], [])
    assert scores(only_dense) == pytest.approx({"d:0": 0.42})
    only_sparse = weighted_fusion([], [hit("d:0", 7.0)])
    assert scores(only_sparse) == pytest.approx({"d:0": 0.3})  # lone sparse score normalizes to 1


def test_fusion_equal_sparse_scores_all_normalize_to_one() -> None:
    fused = weighted_fusion([], [hit("d:0", 2.0), hit("d:1", 2.0)])
    assert scores(fused) == pytest.approx({"d:0": 0.3, "d:1": 0.3})


def test_fusion_ties_break_by_chunk_id_and_ignores_input_order() -> None:
    a = weighted_fusion([hit("b:0", 0.5), hit("a:0", 0.5), hit("c:0", 0.5)], [])
    b = weighted_fusion([hit("c:0", 0.5), hit("a:0", 0.5), hit("b:0", 0.5)], [])
    assert [h.chunk_id for h in a] == ["a:0", "b:0", "c:0"]
    assert a == b


def test_fusion_keeps_hit_metadata_and_does_not_mutate_inputs() -> None:
    dense = [hit("d:0", 0.9, text="dense text")]
    fused = weighted_fusion(dense, [hit("d:0", 1.0, text="sparse text")])
    assert fused[0].text == "dense text"
    assert fused[0].payload == {"chunk_id": "d:0"}
    assert dense[0].score == 0.9


def test_fusion_clamps_negative_dense_scores() -> None:
    fused = weighted_fusion([hit("d:0", -0.2)], [])
    assert fused[0].score == 0.0


def test_apply_threshold_keeps_scores_at_or_above() -> None:
    hits = [hit("d:0", 0.9), hit("d:1", 0.5), hit("d:2", 0.4)]
    assert [h.chunk_id for h in apply_threshold(hits, 0.5)] == ["d:0", "d:1"]
    assert apply_threshold(hits, 0.0) == hits
    assert apply_threshold(hits, 0.95) == []
    assert apply_threshold([], 0.5) == []


def test_apply_rerank_scores_replaces_and_sorts() -> None:
    hits = [hit("d:0", 0.9), hit("d:1", 0.8), hit("d:2", 0.7)]
    reranked = apply_rerank_scores(hits, [0.1, 0.95, 0.4])
    assert [(h.chunk_id, h.score) for h in reranked] == [("d:1", 0.95), ("d:2", 0.4), ("d:0", 0.1)]
    assert hits[0].score == 0.9


def test_apply_rerank_scores_ties_keep_retrieval_order_and_mismatch_is_noop() -> None:
    hits = [hit("d:0", 0.9), hit("d:1", 0.8)]
    assert [h.chunk_id for h in apply_rerank_scores(hits, [0.5, 0.5])] == ["d:0", "d:1"]
    assert apply_rerank_scores(hits, [0.5]) == hits
    assert apply_rerank_scores([], []) == []
