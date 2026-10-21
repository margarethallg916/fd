"""Chunking presets adapted from Yuxi's ragflow_like engine (design §6)."""

from __future__ import annotations

import bisect
from dataclasses import dataclass

from src.infra.knowledge_base.chunking import nlp
from src.infra.knowledge_base.chunking.parsers import book, general, laws, qa

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
    key = (preset or "").strip().lower()
    if key == "naive":
        return DEFAULT_CHUNK_PRESET
    return key if key in CHUNK_PRESETS else DEFAULT_CHUNK_PRESET


def count_tokens(text: str) -> int:
    """Approximate tokens: each [A-Za-z0-9_]+ run or CJK char counts as one."""
    return nlp.count_tokens(text)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _split_by_preset(
    preset: str, filename: str, markdown: str, parser_config: dict[str, int | str]
) -> list[str]:
    if preset == "qa":
        return qa.chunk_markdown(filename, markdown, parser_config)
    if preset == "book":
        return book.chunk_markdown(markdown, parser_config)
    if preset == "laws":
        return laws.chunk_markdown(filename, markdown, parser_config)
    return general.chunk_markdown(markdown, parser_config)


class _LineLocator:
    """Maps chunk text back to 1-based inclusive line ranges of the source markdown."""

    _MIN_FRAGMENT_CHARS = 8

    def __init__(self, markdown: str) -> None:
        self._markdown = markdown
        self._line_starts = [0]
        for offset, char in enumerate(markdown):
            if char == "\n":
                self._line_starts.append(offset + 1)
        self._norm_lines = [line.strip() for line in markdown.split("\n")]
        self._char_cursor = 0
        self._prev_start = 0
        self._line_cursor = 0  # 0-based line index where the previous chunk ended

    def locate(self, text: str) -> tuple[int | None, int | None]:
        found = self._locate_exact(text)
        if found is None:
            found = self._locate_by_lines(text)
        if found is None:
            return None, None
        first, last = found
        self._line_cursor = max(self._line_cursor, last)
        return first + 1, last + 1

    def _line_index(self, offset: int) -> int:
        return bisect.bisect_right(self._line_starts, offset) - 1

    def _locate_exact(self, text: str) -> tuple[int, int] | None:
        # Overlapping chunks start before the previous chunk's end, so retry from its start.
        pos = self._markdown.find(text, self._char_cursor)
        if pos < 0:
            pos = self._markdown.find(text, self._prev_start + 1)
        if pos < 0:
            return None
        self._prev_start = pos
        self._char_cursor = pos + len(text)
        return self._line_index(pos), self._line_index(pos + len(text) - 1)

    def _find_first_line(self, line: str) -> int | None:
        lines = self._norm_lines
        start = self._line_cursor
        for idx in range(start, len(lines)):
            if lines[idx] == line:
                return idx
        # Ancestor headings prepended by hierarchical presets sit before the cursor.
        for idx in range(min(start, len(lines)) - 1, -1, -1):
            if lines[idx] == line:
                return idx
        if len(line) >= self._MIN_FRAGMENT_CHARS:
            # Overlap makes a chunk begin with the tail of a line.
            for idx in range(start, len(lines)):
                if lines[idx].endswith(line):
                    return idx
        return None

    def _locate_by_lines(self, text: str) -> tuple[int, int] | None:
        chunk_lines = [line.strip() for line in text.split("\n") if line.strip()]
        if not chunk_lines:
            return None
        first = self._find_first_line(chunk_lines[0])
        if first is None:
            return None
        if len(chunk_lines) == 1:
            return first, first
        last_line = chunk_lines[-1]
        for idx in range(max(first, self._line_cursor), len(self._norm_lines)):
            if self._norm_lines[idx] == last_line:
                return first, idx
        return None


def chunk_markdown(
    markdown: str,
    *,
    filename: str,
    preset: str | None = None,
    chunk_token_num: int | None = None,
    overlap_percent: int | None = None,
) -> list[TextChunk]:
    """Split markdown with the given preset; values are clamped to the allowed ranges."""
    key = normalize_preset(preset)
    defaults = CHUNK_PRESETS[key]
    token_num = _clamp(
        defaults["chunk_token_num"] if chunk_token_num is None else chunk_token_num,
        MIN_CHUNK_TOKEN_NUM,
        MAX_CHUNK_TOKEN_NUM,
    )
    overlap = _clamp(
        defaults["overlap_percent"] if overlap_percent is None else overlap_percent,
        0,
        MAX_OVERLAP_PERCENT,
    )
    parser_config: dict[str, int | str] = {
        "delimiter": "\n",
        "chunk_token_num": token_num,
        "overlapped_percent": overlap,
    }

    texts = _split_by_preset(key, filename or "", markdown or "", parser_config)

    locator = _LineLocator(markdown or "")
    chunks: list[TextChunk] = []
    for raw in texts:
        text = (raw or "").strip()
        if not text:
            continue
        start_line, end_line = locator.locate(text)
        chunks.append(
            TextChunk(
                index=len(chunks),
                text=text,
                start_line=start_line,
                end_line=end_line,
                token_count=count_tokens(text),
            )
        )
    return chunks
