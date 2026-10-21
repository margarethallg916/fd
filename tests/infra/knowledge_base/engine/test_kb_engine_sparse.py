"""jieba BM25 sparse encoding (design §7.2)."""

from __future__ import annotations

import zlib

import pytest

from src.infra.knowledge_base import sparse
from src.infra.knowledge_base.sparse import (
    SparseVec,
    encode_document,
    encode_query,
    tokenize,
    warmup,
)


def idx(token: str) -> int:
    return zlib.crc32(token.encode()) & 0x7FFFFFFF


def test_tokenize_lowercases_and_drops_punctuation_and_stopwords() -> None:
    tokens = tokenize("今天的北京天气很好, Python is GREAT!  ... --")
    assert "北京" in tokens and "天气" in tokens
    assert "python" in tokens and "great" in tokens
    assert "的" not in tokens and "is" not in tokens
    assert all(t == t.lower() and t.strip() for t in tokens)
    assert not any(t in {",", "!", "...", "--", " "} for t in tokens)


def test_tokenize_empty_inputs() -> None:
    assert tokenize("") == []
    assert tokenize("   \n") == []
    assert tokenize("，。！？") == []


def test_encode_query_is_unique_sorted_and_deterministic() -> None:
    vec = encode_query("北京 北京 天气")
    assert vec == encode_query("北京 北京 天气")
    assert vec.indices == sorted(set(vec.indices))
    assert idx("北京") in vec.indices and idx("天气") in vec.indices
    assert vec.values == [1.0] * len(vec.indices)
    assert 0 <= min(vec.indices) and max(vec.indices) <= 0x7FFFFFFF


def test_encode_query_empty() -> None:
    assert encode_query("，。") == SparseVec(indices=[], values=[])


def test_encode_document_applies_bm25_term_frequency_saturation() -> None:
    k1, b, avgdl = 1.2, 0.75, 256.0
    vec = encode_document("北京 北京 天气")
    tokens = tokenize("北京 北京 天气")
    assert len(tokens) == 3
    norm = k1 * (1 - b + b * 3 / avgdl)
    expected = {idx("北京"): 2 * (k1 + 1) / (2 + norm), idx("天气"): 1 * (k1 + 1) / (1 + norm)}
    assert dict(zip(vec.indices, vec.values)) == pytest.approx(expected)
    assert vec.indices == sorted(vec.indices)


def test_longer_documents_get_lower_weights_for_the_same_term() -> None:
    short = encode_document("北京 天气")
    long = encode_document("北京 天气 " + " ".join(f"词{i}" for i in range(600)))
    short_w = dict(zip(short.indices, short.values))[idx("北京")]
    long_w = dict(zip(long.indices, long.values))[idx("北京")]
    assert long_w < short_w
    saturated = encode_document(" ".join(["北京"] * 50))
    assert dict(zip(saturated.indices, saturated.values))[idx("北京")] < 2.2 + 1e-9


def test_encode_document_empty_and_stopword_only() -> None:
    assert encode_document("") == SparseVec(indices=[], values=[])
    assert encode_document("的 了 ， 。") == SparseVec(indices=[], values=[])


def test_colliding_token_indices_are_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sparse, "_token_index", lambda token: 42)
    doc = encode_document("北京 天气")
    assert doc.indices == [42]
    k1, b = 1.2, 0.75
    assert doc.values == pytest.approx([2 * (k1 + 1) / (2 + k1 * (1 - b + b * 2 / 256))])
    assert encode_query("北京 天气") == SparseVec(indices=[42], values=[1.0])


async def test_warmup_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    await warmup()
    assert sparse._warmed_up is True

    calls: list[int] = []
    monkeypatch.setattr(sparse, "_load_dictionary", lambda: calls.append(1))
    await warmup()
    assert calls == []
