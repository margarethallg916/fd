"""jieba-based BM25 sparse vectors for Qdrant (design §7.2)."""

from __future__ import annotations

import re
import zlib
from collections import Counter
from dataclasses import dataclass

import jieba

from src.infra.async_utils import run_blocking_io

_K1 = 1.2
_B = 0.75
_AVG_DOC_LEN = 256.0
_WORD_CHAR = re.compile(r"\w")
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "及", "或", "也", "就", "都", "而", "之", "以",
        "为", "对", "把", "被", "让", "给", "这", "那", "我", "你", "他", "她", "它", "们",
        "有", "没", "不", "吗", "呢", "吧", "啊", "哦", "着", "过", "个",
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for", "is", "are",
        "was", "were", "be", "been", "it", "this", "that", "with", "as", "by", "from",
    }
)  # fmt: skip
_warmed_up = False


@dataclass(frozen=True)
class SparseVec:
    indices: list[int]
    values: list[float]


def tokenize(text: str) -> list[str]:
    """jieba.lcut_for_search, lower-cased, punctuation/whitespace/stopwords removed."""
    tokens: list[str] = []
    for raw in jieba.lcut_for_search(text or ""):
        token = raw.strip().lower()
        if not token or token in _STOPWORDS or not _WORD_CHAR.search(token):
            continue
        tokens.append(token)
    return tokens


def _token_index(token: str) -> int:
    return zlib.crc32(token.encode("utf-8")) & 0x7FFFFFFF


def encode_document(text: str) -> SparseVec:
    """BM25 TF-saturated weights (k1=1.2, b=0.75, avgdl=256); IDF is applied by Qdrant."""
    tokens = tokenize(text)
    if not tokens:
        return SparseVec(indices=[], values=[])
    term_freq: Counter[int] = Counter(_token_index(token) for token in tokens)
    norm = _K1 * (1 - _B + _B * len(tokens) / _AVG_DOC_LEN)
    indices = sorted(term_freq)
    values = [term_freq[i] * (_K1 + 1) / (term_freq[i] + norm) for i in indices]
    return SparseVec(indices=indices, values=values)


def encode_query(text: str) -> SparseVec:
    """Unique query tokens with weight 1.0 (may be empty)."""
    indices = sorted({_token_index(token) for token in tokenize(text)})
    return SparseVec(indices=indices, values=[1.0] * len(indices))


def _load_dictionary() -> None:
    global _warmed_up
    jieba.initialize()
    _warmed_up = True


async def warmup() -> None:
    """Load the jieba dictionary off the event loop (idempotent)."""
    if not _warmed_up:
        await run_blocking_io(_load_dictionary)
