"""Admin-configured embedding / rerank model access (design §7.3)."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

from src.infra.llm.client import LLMClient
from src.infra.logging import get_logger
from src.kernel.config import settings

logger = get_logger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 1.0
_EMBED_TIMEOUT = httpx.Timeout(60.0, connect=10.0)
_RERANK_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
_DEFAULT_BATCH_SIZE = 32
_API_BASE_SUFFIX = re.compile(r"(?:/v1/embeddings|/v1/rerank|/v1)$", re.IGNORECASE)


class EmbeddingError(Exception):
    """Embedding call failed after retries, or no embedding model is configured."""


@dataclass(frozen=True)
class ModelCard:
    model_id: str
    model_name: str
    api_base: str  # normalized: no trailing '/', '/v1', '/v1/embeddings' or '/v1/rerank'
    api_key: str


def normalize_api_base(api_base: str) -> str:
    base = (api_base or "").strip().rstrip("/")
    base = _API_BASE_SUFFIX.sub("", base)
    return base.rstrip("/")


def configured_embedding_model_id() -> str:
    """settings.KB_EMBEDDING_MODEL_ID or settings.NATIVE_MEMORY_EMBEDDING_MODEL_ID or ''."""
    return str(
        getattr(settings, "KB_EMBEDDING_MODEL_ID", "")
        or getattr(settings, "NATIVE_MEMORY_EMBEDDING_MODEL_ID", "")
        or ""
    ).strip()


async def _resolve_card(model_id: str | None, kind: str) -> ModelCard | None:
    if not model_id:
        return None
    config = await LLMClient.get_card_config(model_id, kind=kind)
    if not config:
        return None
    api_base = normalize_api_base(str(config.get("api_base") or ""))
    api_key = str(config.get("api_key") or "")
    model_name = str(config.get("model") or "")
    if not (api_base and api_key and model_name):
        logger.warning(f"[KB] {kind} model card {model_id} is incomplete")
        return None
    return ModelCard(model_id=model_id, model_name=model_name, api_base=api_base, api_key=api_key)


async def resolve_embedding_card(model_id: str | None = None) -> ModelCard | None:
    """Resolve the given (or configured) embedding card; None when missing/incomplete."""
    return await _resolve_card(model_id or configured_embedding_model_id(), "embedding")


async def resolve_rerank_card() -> ModelCard | None:
    """Resolve settings.KB_RERANK_MODEL_ID; None when empty/missing/incomplete."""
    model_id = str(getattr(settings, "KB_RERANK_MODEL_ID", "") or "").strip()
    return await _resolve_card(model_id, "rerank")


def _new_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


def _headers(card: ModelCard) -> dict[str, str]:
    return {"Authorization": f"Bearer {card.api_key}", "Content-Type": "application/json"}


def _batch_size() -> int:
    try:
        size = int(getattr(settings, "KB_EMBEDDING_BATCH_SIZE", _DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        size = _DEFAULT_BATCH_SIZE
    return size if size > 0 else _DEFAULT_BATCH_SIZE


def _parse_embeddings(payload: Any, expected: int) -> list[list[float]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) != expected:
        raise EmbeddingError("嵌入服务返回的向量数量与输入不一致")
    items: list[tuple[int, Any]] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise EmbeddingError("嵌入服务返回格式无效")
        index = item.get("index")
        items.append((index if isinstance(index, int) else position, item["embedding"]))
    items.sort(key=lambda pair: pair[0])
    vectors = [[float(x) for x in embedding] for _, embedding in items]
    if not vectors[0] or any(len(vec) != len(vectors[0]) for vec in vectors):
        raise EmbeddingError("嵌入服务返回的向量维度无效")
    return vectors


async def _post_embeddings(
    client: httpx.AsyncClient, card: ModelCard, batch: list[str]
) -> list[list[float]]:
    url = f"{card.api_base}/v1/embeddings"
    body = {"model": card.model_name, "input": batch}
    last_error = "unknown error"
    for attempt in range(_MAX_ATTEMPTS):
        retryable = True
        try:
            response = await client.post(url, headers=_headers(card), json=body)
            status = response.status_code
            if status == 429 or status >= 500:
                last_error = f"HTTP {status}"
            elif status >= 400:
                retryable = False
                last_error = f"HTTP {status}"
                logger.warning(f"[KB] Embedding request rejected: {status} {response.text[:200]}")
            else:
                try:
                    return _parse_embeddings(response.json(), len(batch))
                except ValueError as exc:
                    raise EmbeddingError("嵌入服务返回了无效的 JSON") from exc
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if not retryable:
            break
        if attempt + 1 < _MAX_ATTEMPTS:
            await asyncio.sleep(_RETRY_BASE_DELAY_SECONDS * (2**attempt))
    logger.warning(f"[KB] Embedding request failed: {last_error}")
    raise EmbeddingError(f"嵌入服务调用失败（{last_error}）")


async def embed_texts(texts: list[str], card: ModelCard) -> list[list[float]]:
    """Batched POST {api_base}/v1/embeddings (batch = KB_EMBEDDING_BATCH_SIZE), ordered by index.

    Retries 429/5xx/network errors 3 times with exponential backoff; raises EmbeddingError.
    """
    if not texts:
        return []
    size = _batch_size()
    vectors: list[list[float]] = []
    async with _new_client(_EMBED_TIMEOUT) as client:
        for start in range(0, len(texts), size):
            vectors.extend(await _post_embeddings(client, card, texts[start : start + size]))
    dims = {len(vec) for vec in vectors}
    if len(dims) != 1:
        raise EmbeddingError("嵌入服务返回的向量维度不一致")
    return vectors


def _parse_rerank_scores(payload: Any, expected: int) -> list[float] | None:
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return None
    scores: dict[int, float] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        score = item.get("relevance_score")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < expected
            or index in scores
            or isinstance(score, bool)
            or not isinstance(score, int | float)
        ):
            continue
        scores[index] = float(score)
    if len(scores) != expected:
        return None
    return [scores[i] for i in range(expected)]


async def rerank(query: str, documents: list[str], card: ModelCard) -> list[float] | None:
    """Return one relevance score per document (input order), or None on any failure."""
    if not documents:
        return []
    body = {
        "model": card.model_name,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }
    try:
        async with _new_client(_RERANK_TIMEOUT) as client:
            response = await client.post(
                f"{card.api_base}/v1/rerank", headers=_headers(card), json=body
            )
            response.raise_for_status()
            scores = _parse_rerank_scores(response.json(), len(documents))
    except Exception as exc:  # noqa: BLE001 - rerank is optional; any failure falls back
        logger.warning(f"[KB] Rerank failed: {type(exc).__name__}: {exc}")
        return None
    if scores is None:
        logger.warning("[KB] Rerank response did not cover every document")
    return scores
