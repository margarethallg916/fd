"""Document parsing: file bytes -> markdown (design §5)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ParseMethod = Literal["mineru", "pdf_text", "local"]

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pdf", ".docx", ".pptx",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff",
        ".txt", ".md", ".html", ".htm", ".json", ".csv", ".xlsx", ".xls", ".zip",
    }
)
# Extensions that can only be parsed by MinerU (no local fallback).
MINERU_ONLY_EXTENSIONS: frozenset[str] = frozenset(
    {".pptx", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
)


class ParseError(Exception):
    """User-presentable parse failure (message is Chinese, shown in the UI)."""


@dataclass(frozen=True)
class ParsedDocument:
    markdown: str
    method: ParseMethod


def normalize_extension(filename: str) -> str:
    """Return the lower-cased final suffix including the dot ('' when none)."""
    raise NotImplementedError


def is_supported_filename(filename: str) -> bool:
    raise NotImplementedError


def mineru_available() -> bool:
    """True iff settings.ENABLE_DOCUMENT_PARSE and MINERU_API_BASE_URL is non-empty."""
    raise NotImplementedError


async def parse_document(
    filename: str, data: bytes, content_type: str | None = None
) -> ParsedDocument:
    """Parse raw bytes into markdown.

    Raises ParseError for unsupported/empty/unparseable input. Blocking parsers run via
    run_blocking_io. Never returns empty markdown (raises ParseError instead).
    """
    raise NotImplementedError
