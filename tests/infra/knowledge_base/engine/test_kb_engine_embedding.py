"""Embedding / rerank model-card access (design §7.3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.infra.knowledge_base import embedding
from src.infra.knowledge_base.embedding import (
    EmbeddingError,
    ModelCard,
    configured_embedding_model_id,
    embed_texts,
    normalize_api_base,
    rerank,
    resolve_embedding_card,
    resolve_rerank_card,
)

CARD = ModelCard(
    model_id="m1", model_name="bge-m3", api_base="https://api.example.com", api_key="sk-test"
)


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


def install(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> Recorder:
    recorder = Recorder(handler)
    monkeypatch.setattr(
        embedding,
        "_new_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(recorder), timeout=timeout),
    )
    monkeypatch.setattr(embedding, "_RETRY_BASE_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(embedding, "settings", SimpleNamespace(KB_EMBEDDING_BATCH_SIZE=2))
    return recorder


def embeddings_response(
    request: httpx.Request, reverse: bool = False, dim: int = 3
) -> httpx.Response:
    inputs = json.loads(request.content)["input"]
    data = [
        {
            "index": i,
            "embedding": [float(len(text)), float(i), float(dim)][:dim] + [0.5] * (dim - 3),
        }
        for i, text in enumerate(inputs)
    ]
    return httpx.Response(200, json={"data": list(reversed(data)) if reverse else data})


def test_normalize_api_base() -> None:
    assert normalize_api_base("https://x.com") == "https://x.com"
    assert normalize_api_base("https://x.com/") == "https://x.com"
    assert normalize_api_base("https://x.com/v1") == "https://x.com"
    assert normalize_api_base("https://x.com/v1/") == "https://x.com"
    assert normalize_api_base("https://x.com/V1/embeddings") == "https://x.com"
    assert normalize_api_base("https://x.com/v1/embeddings/") == "https://x.com"
    assert normalize_api_base("https://x.com/v1/rerank") == "https://x.com"
    assert normalize_api_base("  https://x.com/api/v1  ") == "https://x.com/api"
    assert normalize_api_base("http://host:8000/prefix") == "http://host:8000/prefix"
    assert normalize_api_base("") == ""


def test_configured_embedding_model_id_prefers_kb_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    both = SimpleNamespace(KB_EMBEDDING_MODEL_ID="kb", NATIVE_MEMORY_EMBEDDING_MODEL_ID="mem")
    monkeypatch.setattr(embedding, "settings", both)
    assert configured_embedding_model_id() == "kb"
    fallback = SimpleNamespace(KB_EMBEDDING_MODEL_ID="", NATIVE_MEMORY_EMBEDDING_MODEL_ID="mem")
    monkeypatch.setattr(embedding, "settings", fallback)
    assert configured_embedding_model_id() == "mem"
    monkeypatch.setattr(embedding, "settings", SimpleNamespace())
    assert configured_embedding_model_id() == ""


class FakeLLMClient:
    configs: dict[tuple[str, str], dict[str, Any]] = {}
    calls: list[tuple[str | None, str]] = []

    @staticmethod
    async def get_card_config(model_id: str | None, *, kind: str) -> dict[str, Any] | None:
        FakeLLMClient.calls.append((model_id, kind))
        return FakeLLMClient.configs.get((model_id or "", kind))


@pytest.fixture
def cards(monkeypatch: pytest.MonkeyPatch) -> type[FakeLLMClient]:
    FakeLLMClient.configs = {}
    FakeLLMClient.calls = []
    monkeypatch.setattr(embedding, "LLMClient", FakeLLMClient)
    monkeypatch.setattr(
        embedding,
        "settings",
        SimpleNamespace(
            KB_EMBEDDING_MODEL_ID="emb-1",
            NATIVE_MEMORY_EMBEDDING_MODEL_ID="",
            KB_RERANK_MODEL_ID="rr-1",
        ),
    )
    return FakeLLMClient


async def test_resolve_embedding_card_normalizes_api_base(cards: type[FakeLLMClient]) -> None:
    cards.configs[("emb-1", "embedding")] = {
        "api_base": "https://x.com/v1/",
        "api_key": "k",
        "model": "bge",
    }
    card = await resolve_embedding_card()
    assert card == ModelCard(
        model_id="emb-1", model_name="bge", api_base="https://x.com", api_key="k"
    )
    assert cards.calls == [("emb-1", "embedding")]


async def test_resolve_embedding_card_explicit_id_overrides_setting(
    cards: type[FakeLLMClient],
) -> None:
    cards.configs[("other", "embedding")] = {
        "api_base": "https://y.com",
        "api_key": "k",
        "model": "m",
    }
    card = await resolve_embedding_card("other")
    assert card is not None and card.model_id == "other"


@pytest.mark.parametrize(
    "config",
    [
        None,
        {"api_base": "", "api_key": "k", "model": "m"},
        {"api_base": "https://x.com", "api_key": "", "model": "m"},
        {"api_base": "https://x.com", "api_key": "k", "model": ""},
        {"api_base": None, "api_key": None, "model": None},
    ],
)
async def test_resolve_card_missing_or_incomplete_is_none(
    cards: type[FakeLLMClient], config: Any
) -> None:
    if config is not None:
        cards.configs[("emb-1", "embedding")] = config
        cards.configs[("rr-1", "rerank")] = config
    assert await resolve_embedding_card() is None
    assert await resolve_rerank_card() is None


async def test_resolve_embedding_card_without_any_id_skips_lookup(
    cards: type[FakeLLMClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        embedding,
        "settings",
        SimpleNamespace(KB_EMBEDDING_MODEL_ID="", NATIVE_MEMORY_EMBEDDING_MODEL_ID=""),
    )
    assert await resolve_embedding_card() is None
    assert await resolve_rerank_card() is None
    assert cards.calls == []


async def test_resolve_rerank_card(cards: type[FakeLLMClient]) -> None:
    cards.configs[("rr-1", "rerank")] = {
        "api_base": "https://r.com/v1/rerank",
        "api_key": "k",
        "model": "rr",
    }
    card = await resolve_rerank_card()
    assert card == ModelCard(
        model_id="rr-1", model_name="rr", api_base="https://r.com", api_key="k"
    )
    assert cards.calls == [("rr-1", "rerank")]


async def test_embed_texts_batches_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, lambda request: embeddings_response(request, reverse=True))
    texts = ["a", "bb", "ccc", "dddd", "eeeee"]
    vectors = await embed_texts(texts, CARD)
    assert [v[0] for v in vectors] == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert [b["input"] for b in recorder.bodies()] == [["a", "bb"], ["ccc", "dddd"], ["eeeee"]]
    request = recorder.requests[0]
    assert str(request.url) == "https://api.example.com/v1/embeddings"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert recorder.bodies()[0]["model"] == "bge-m3"


async def test_embed_texts_empty_makes_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, lambda request: embeddings_response(request))
    assert await embed_texts([], CARD) == []
    assert recorder.requests == []


async def test_embed_texts_default_batch_size_is_32(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, lambda request: embeddings_response(request))
    monkeypatch.setattr(embedding, "settings", SimpleNamespace())
    await embed_texts([f"t{i}" for i in range(70)], CARD)
    assert [len(b["input"]) for b in recorder.bodies()] == [32, 32, 6]


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_embed_texts_retries_transient_statuses(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(status, text="try later")
        return embeddings_response(request)

    recorder = install(monkeypatch, handler)
    vectors = await embed_texts(["a", "bb"], CARD)
    assert len(vectors) == 2
    assert len(recorder.requests) == 3


async def test_embed_texts_retries_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return embeddings_response(request)

    recorder = install(monkeypatch, handler)
    assert len(await embed_texts(["a"], CARD)) == 1
    assert len(recorder.requests) == 2


async def test_embed_texts_gives_up_after_three_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, lambda request: httpx.Response(503, text="down"))
    with pytest.raises(EmbeddingError, match="503"):
        await embed_texts(["a"], CARD)
    assert len(recorder.requests) == 3


async def test_embed_texts_backs_off_exponentially(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda request: httpx.Response(500))
    monkeypatch.setattr(embedding, "_RETRY_BASE_DELAY_SECONDS", 1.0)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(embedding.asyncio, "sleep", fake_sleep)
    with pytest.raises(EmbeddingError):
        await embed_texts(["a"], CARD)
    assert delays == [1.0, 2.0]


async def test_embed_texts_client_errors_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, lambda request: httpx.Response(401, text="bad key"))
    with pytest.raises(EmbeddingError, match="401"):
        await embed_texts(["a"], CARD)
    assert len(recorder.requests) == 1


async def test_embed_texts_error_does_not_leak_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda request: httpx.Response(401, text="bad key"))
    with pytest.raises(EmbeddingError) as excinfo:
        await embed_texts(["a"], CARD)
    assert "sk-test" not in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"index": 0, "embedding": [0.1]}]},  # fewer vectors than inputs
        {"data": "nope"},
        {"unexpected": True},
        {"data": [{"index": 0}, {"index": 1}]},
        {"data": [{"index": 0, "embedding": []}, {"index": 1, "embedding": []}]},
        {"data": [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.1, 0.2]}]},
    ],
)
async def test_embed_texts_rejects_malformed_responses(
    monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    recorder = install(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(EmbeddingError):
        await embed_texts(["a", "bb"], CARD)
    assert len(recorder.requests) == 1


async def test_embed_texts_rejects_non_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, lambda request: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(EmbeddingError):
        await embed_texts(["a"], CARD)


async def test_embed_texts_rejects_dimension_drift_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dims = iter([3, 4])
    install(monkeypatch, lambda request: embeddings_response(request, dim=next(dims)))
    with pytest.raises(EmbeddingError, match="维度"):
        await embed_texts(["a", "bb", "ccc"], CARD)


# --- rerank ------------------------------------------------------------------------


def rerank_handler(results: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, json={"results": results})


async def test_rerank_returns_scores_in_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(
        monkeypatch,
        rerank_handler(
            [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.2},
                {"index": 1, "relevance_score": 0},
            ]
        ),
    )
    scores = await rerank("q", ["d0", "d1", "d2"], CARD)
    assert scores == [0.2, 0.0, 0.9]
    request = recorder.requests[0]
    assert str(request.url) == "https://api.example.com/v1/rerank"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert recorder.bodies()[0] == {
        "model": "bge-m3",
        "query": "q",
        "documents": ["d0", "d1", "d2"],
        "top_n": 3,
    }


async def test_rerank_empty_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = install(monkeypatch, rerank_handler([]))
    assert await rerank("q", [], CARD) == []
    assert recorder.requests == []


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 0.5}],  # missing document 1
        [{"index": 0, "relevance_score": 0.5}, {"index": 0, "relevance_score": 0.6}],  # duplicate
        [
            {"index": 0, "relevance_score": 0.5},
            {"index": 5, "relevance_score": 0.6},
        ],  # out of range
        [{"index": 0, "relevance_score": "high"}, {"index": 1, "relevance_score": 0.1}],
        [{"index": 0}, {"index": 1}],
        ["x", None],
        "nope",
        None,
    ],
)
async def test_rerank_returns_none_for_unusable_results(
    monkeypatch: pytest.MonkeyPatch, results: Any
) -> None:
    install(monkeypatch, rerank_handler(results))
    assert await rerank("q", ["a", "b"], CARD) is None


async def test_rerank_returns_none_on_http_or_network_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, rerank_handler([], status=500))
    assert await rerank("q", ["a"], CARD) is None

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    install(monkeypatch, boom)
    assert await rerank("q", ["a"], CARD) is None

    install(monkeypatch, lambda request: httpx.Response(200, text="not json"))
    assert await rerank("q", ["a"], CARD) is None
