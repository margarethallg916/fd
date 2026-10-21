"""Admin-configured embedding / rerank model access (design §7.3)."""

from __future__ import annotations

from dataclasses import dataclass


class EmbeddingError(Exception):
    """Embedding call failed after retries, or no embedding model is configured."""


@dataclass(frozen=True)
class ModelCard:
    model_id: str
    model_name: str
    api_base: str  # normalized: no trailing '/', '/v1', '/v1/embeddings' or '/v1/rerank'
    api_key: str


def normalize_api_base(api_base: str) -> str:
    raise NotImplementedError


def configured_embedding_model_id() -> str:
    """settings.KB_EMBEDDING_MODEL_ID or settings.NATIVE_MEMORY_EMBEDDING_MODEL_ID or ''."""
    raise NotImplementedError


async def resolve_embedding_card(model_id: str | None = None) -> ModelCard | None:
    """Resolve the given (or configured) embedding card; None when missing/incomplete."""
    raise NotImplementedError


async def resolve_rerank_card() -> ModelCard | None:
    """Resolve settings.KB_RERANK_MODEL_ID; None when empty/missing/incomplete."""
    raise NotImplementedError


async def embed_texts(texts: list[str], card: ModelCard) -> list[list[float]]:
    """Batched POST {api_base}/v1/embeddings (batch = KB_EMBEDDING_BATCH_SIZE), ordered by index.

    Retries 429/5xx/network errors 3 times with exponential backoff; raises EmbeddingError.
    """
    raise NotImplementedError


async def rerank(query: str, documents: list[str], card: ModelCard) -> list[float] | None:
    """Return one relevance score per document (input order), or None on any failure."""
    raise NotImplementedError
