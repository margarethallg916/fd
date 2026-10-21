"""Chunking presets adapted from Yuxi's ragflow_like engine (design §6)."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_CHUNK_PRESET = "general"
DEFAULT_CHUNK_TOKEN_NUM = 512
DEFAULT_OVERLAP_PERCENT = 0
MIN_CHUNK_TOKEN_NUM = 100
MAX_CHUNK_TOKEN_NUM = 2048
MAX_OVERLAP_PERCENT = 50

# key -> {"chunk_token_num": int, "overlap_percent": int}
CHUNK_PRESETS: dict[str, dict[str, int]] = {
    "general": {"chunk_token_num": 512, "overlap_percent": 0},
    "qa": {"chunk_token_num": 512, "overlap_percent": 0},
    "book": {"chunk_token_num": 512, "overlap_percent": 0},
    "laws": {"chunk_token_num": 512, "overlap_percent": 0},
}


@dataclass(frozen=True)
class TextChunk:
    index: int  # 0-based, contiguous after empty chunks are dropped
    text: str
    start_line: int | None  # 1-based inclusive line range in the source markdown
    end_line: int | None
    token_count: int


def normalize_preset(preset: str | None) -> str:
    """Unknown/empty preset -> DEFAULT_CHUNK_PRESET."""
    raise NotImplementedError


def count_tokens(text: str) -> int:
    """Approximate tokens: each [A-Za-z0-9_]+ run or CJK char counts as one."""
    raise NotImplementedError


def chunk_markdown(
    markdown: str,
    *,
    filename: str,
    preset: str | None = None,
    chunk_token_num: int | None = None,
    overlap_percent: int | None = None,
) -> list[TextChunk]:
    """Split markdown with the given preset; values are clamped to the allowed ranges."""
    raise NotImplementedError
