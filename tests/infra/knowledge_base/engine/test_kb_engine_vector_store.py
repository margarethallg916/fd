"""Qdrant wrapper in embedded local mode (design §7.1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.infra.knowledge_base import vector_store
from src.infra.knowledge_base.sparse import SparseVec, encode_document, encode_query
from src.infra.knowledge_base.vector_store import (
    ChunkPoint,
    KBVectorStore,
    close_vector_store,
    collection_name_for,
    get_vector_store,
    point_id_for,
)

DIM = 4
MODEL = "test-embedding-model"


def vec(*values: float) -> list[float]:
    return list(values)


def point(
    doc_id: str,
    index: int,
    text: str,
    dense: list[float],
    kb_id: str = "kb1",
    start_line: int | None = 1,
) -> ChunkPoint:
    return ChunkPoint(
        chunk_id=f"{doc_id}:{index}",
        kb_id=kb_id,
        doc_id=doc_id,
        index=index,
        text=text,
        start_line=start_line,
        end_line=None if start_line is None else start_line + 1,
        dense=dense,
        sparse=encode_document(text),
    )


@pytest.fixture
async def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[KBVectorStore]:
    monkeypatch.setattr(
        vector_store,
        "settings",
        SimpleNamespace(
            QDRANT_URL="", QDRANT_API_KEY="", QDRANT_LOCAL_PATH=str(tmp_path / "qdrant")
        ),
    )
    monkeypatch.setattr(vector_store, "_store", None)
    monkeypatch.setattr(vector_store, "_RETRY_BASE_DELAY_SECONDS", 0.0)
    instance = get_vector_store()
    yield instance
    await close_vector_store()


def test_collection_name_and_point_id_are_deterministic() -> None:
    name = collection_name_for(1024, "bge-m3")
    assert name == collection_name_for(1024, "bge-m3")
    assert name.startswith("kb_1024_") and len(name) == len("kb_1024_") + 8
    assert name != collection_name_for(1024, "other-model")
    assert name != collection_name_for(768, "bge-m3")
    pid = point_id_for("doc:3")
    assert pid == point_id_for("doc:3") != point_id_for("doc:4")
    assert pid == str(uuid.uuid5(uuid.NAMESPACE_URL, "kb-chunk:doc:3"))


def test_get_vector_store_is_a_singleton(store: KBVectorStore) -> None:
    assert get_vector_store() is store


async def test_close_resets_the_singleton(store: KBVectorStore) -> None:
    await store.ensure_collection(DIM, MODEL)
    await close_vector_store()
    fresh = get_vector_store()
    assert fresh is not store
    # Data persisted on disk is visible to the new client.
    name = await fresh.ensure_collection(DIM, MODEL)
    assert name == collection_name_for(DIM, MODEL)


async def test_local_mode_creates_the_data_directory(store: KBVectorStore, tmp_path: Path) -> None:
    await store.ensure_collection(DIM, MODEL)
    assert (tmp_path / "qdrant").is_dir()


async def test_ensure_collection_is_idempotent(store: KBVectorStore) -> None:
    first = await store.ensure_collection(DIM, MODEL)
    second = await store.ensure_collection(DIM, MODEL)
    assert first == second == collection_name_for(DIM, MODEL)
    client = await store._get_client()
    info = await client.get_collection(first)
    assert info.config.params.vectors["dense"].size == DIM  # type: ignore[index]
    assert "bm25" in (info.config.params.sparse_vectors or {})


async def seed(store: KBVectorStore) -> str:
    collection = await store.ensure_collection(DIM, MODEL)
    await store.upsert_chunks(
        collection,
        [
            point("docA", 0, "北京今天天气晴朗", vec(1, 0, 0, 0)),
            point("docA", 1, "上海明天有大雨", vec(0, 1, 0, 0)),
            point("docB", 0, "Python asyncio tutorial", vec(0, 0, 1, 0)),
            point("docC", 0, "北京烤鸭的做法", vec(0.9, 0.1, 0, 0), kb_id="kb2", start_line=None),
        ],
    )
    return collection


async def test_dense_search_ranks_by_cosine_and_filters_by_kb(store: KBVectorStore) -> None:
    collection = await seed(store)
    hits = await store.search_dense(collection, ["kb1"], vec(1, 0.1, 0, 0), limit=10)
    assert [h.chunk_id for h in hits] == ["docA:0", "docA:1", "docB:0"]
    assert hits[0].score > hits[1].score >= hits[2].score
    assert all(h.kb_id == "kb1" for h in hits)
    top = hits[0]
    assert (top.doc_id, top.index, top.text, top.start_line, top.end_line) == (
        "docA",
        0,
        "北京今天天气晴朗",
        1,
        2,
    )
    assert top.payload["chunk_id"] == "docA:0"

    both = await store.search_dense(collection, ["kb1", "kb2"], vec(1, 0, 0, 0), limit=2)
    assert [h.chunk_id for h in both] == ["docA:0", "docC:0"]
    assert both[1].start_line is None and both[1].end_line is None


async def test_sparse_search_matches_keywords(store: KBVectorStore) -> None:
    collection = await seed(store)
    hits = await store.search_sparse(collection, ["kb1", "kb2"], encode_query("北京"), limit=10)
    assert {h.chunk_id for h in hits} == {"docA:0", "docC:0"}
    only_kb2 = await store.search_sparse(collection, ["kb2"], encode_query("北京"), limit=10)
    assert [h.chunk_id for h in only_kb2] == ["docC:0"]
    english = await store.search_sparse(collection, ["kb1"], encode_query("asyncio"), limit=10)
    assert [h.chunk_id for h in english] == ["docB:0"]
    assert english[0].score > 0


async def test_sparse_search_with_empty_or_unmatched_query(store: KBVectorStore) -> None:
    collection = await seed(store)
    assert await store.search_sparse(collection, ["kb1"], SparseVec([], []), limit=5) == []
    assert (
        await store.search_sparse(collection, ["kb1"], encode_query("完全不存在的词语xyz"), limit=5)
        == []
    )


async def test_search_with_no_kb_ids_or_limit_returns_nothing(store: KBVectorStore) -> None:
    collection = await seed(store)
    assert await store.search_dense(collection, [], vec(1, 0, 0, 0), limit=5) == []
    assert await store.search_dense(collection, ["kb1"], vec(1, 0, 0, 0), limit=0) == []


async def test_search_missing_collection_returns_empty(store: KBVectorStore) -> None:
    assert await store.search_dense("kb_missing", ["kb1"], vec(1, 0, 0, 0), limit=5) == []
    assert await store.search_sparse("kb_missing", ["kb1"], encode_query("北京"), limit=5) == []


async def test_upsert_is_idempotent_per_chunk_id(store: KBVectorStore) -> None:
    collection = await seed(store)
    await store.upsert_chunks(collection, [point("docA", 0, "北京已更新的内容", vec(1, 0, 0, 0))])
    hits = await store.search_dense(collection, ["kb1"], vec(1, 0, 0, 0), limit=50)
    assert [h.chunk_id for h in hits].count("docA:0") == 1
    assert next(h for h in hits if h.chunk_id == "docA:0").text == "北京已更新的内容"


async def test_upsert_in_batches_of_128(store: KBVectorStore) -> None:
    collection = await store.ensure_collection(DIM, MODEL)
    points = [point("big", i, f"词{i} 内容", vec(1, i % 7, 0, 0)) for i in range(300)]
    await store.upsert_chunks(collection, points)
    client = await store._get_client()
    assert (await client.count(collection, exact=True)).count == 300
    assert await store.upsert_chunks(collection, []) is None


async def test_upsert_accepts_chunks_without_sparse_terms(store: KBVectorStore) -> None:
    collection = await store.ensure_collection(DIM, MODEL)
    await store.upsert_chunks(collection, [point("docX", 0, "的 了 ，。", vec(0, 0, 0, 1))])
    hits = await store.search_dense(collection, ["kb1"], vec(0, 0, 0, 1), limit=5)
    assert [h.chunk_id for h in hits] == ["docX:0"]


async def test_upsert_retries_then_succeeds(
    store: KBVectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = await store.ensure_collection(DIM, MODEL)
    client = await store._get_client()
    real_upsert = client.upsert
    calls = {"n": 0}

    async def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary failure")
        return await real_upsert(**kwargs)

    monkeypatch.setattr(client, "upsert", flaky)
    await store.upsert_chunks(collection, [point("docR", 0, "重试成功", vec(1, 1, 0, 0))])
    assert calls["n"] == 3
    assert [
        h.chunk_id for h in await store.search_dense(collection, ["kb1"], vec(1, 1, 0, 0), 5)
    ] == ["docR:0"]


async def test_upsert_raises_after_three_failures(
    store: KBVectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    collection = await store.ensure_collection(DIM, MODEL)
    client = await store._get_client()
    calls = {"n": 0}

    async def broken(**kwargs: Any) -> Any:
        calls["n"] += 1
        raise RuntimeError("down")

    monkeypatch.setattr(client, "upsert", broken)
    with pytest.raises(RuntimeError, match="down"):
        await store.upsert_chunks(collection, [point("docR", 0, "x", vec(1, 1, 0, 0))])
    assert calls["n"] == 3


async def test_delete_by_doc_removes_only_that_doc(store: KBVectorStore) -> None:
    collection = await seed(store)
    await store.delete_by_doc(collection, "docA")
    hits = await store.search_dense(collection, ["kb1", "kb2"], vec(1, 1, 1, 0), limit=50)
    assert sorted(h.chunk_id for h in hits) == ["docB:0", "docC:0"]
    assert await store.search_sparse(collection, ["kb1"], encode_query("上海"), limit=5) == []


async def test_delete_by_kb_removes_every_chunk_of_the_kb(store: KBVectorStore) -> None:
    collection = await seed(store)
    await store.delete_by_kb(collection, "kb1")
    hits = await store.search_dense(collection, ["kb1", "kb2"], vec(1, 1, 1, 0), limit=50)
    assert [h.chunk_id for h in hits] == ["docC:0"]


async def test_delete_is_a_noop_for_unknown_ids_and_missing_collections(
    store: KBVectorStore,
) -> None:
    collection = await seed(store)
    await store.delete_by_doc(collection, "ghost")
    await store.delete_by_kb(collection, "ghost")
    await store.delete_by_doc("kb_missing", "docA")
    await store.delete_by_kb("kb_missing", "kb1")
    hits = await store.search_dense(collection, ["kb1", "kb2"], vec(1, 1, 1, 0), limit=50)
    assert len(hits) == 4


async def test_concurrent_first_use_creates_a_single_client(store: KBVectorStore) -> None:
    import asyncio

    clients = await asyncio.gather(*(store._get_client() for _ in range(5)))
    assert all(c is clients[0] for c in clients)


async def test_server_mode_uses_url_and_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            created.append(kwargs)

        async def close(self) -> None:
            created.append({"closed": True})

    monkeypatch.setattr(
        vector_store,
        "settings",
        SimpleNamespace(
            QDRANT_URL="http://qdrant:6333", QDRANT_API_KEY="key", QDRANT_LOCAL_PATH="unused"
        ),
    )
    monkeypatch.setattr(vector_store, "AsyncQdrantClient", FakeClient)
    instance = KBVectorStore()
    await instance._get_client()
    assert created == [{"url": "http://qdrant:6333", "api_key": "key"}]
    assert instance._server_mode is True
    await instance.close()
    assert created[-1] == {"closed": True}


class RecordingServerClient:
    def __init__(self, exists: list[bool], create_error: Exception | None = None) -> None:
        self.exists = exists
        self.create_error = create_error
        self.created: list[dict[str, Any]] = []
        self.indexes: list[dict[str, Any]] = []

    async def collection_exists(self, name: str) -> bool:
        return self.exists.pop(0)

    async def create_collection(self, **kwargs: Any) -> None:
        self.created.append(kwargs)
        if self.create_error:
            raise self.create_error

    async def create_payload_index(self, **kwargs: Any) -> None:
        self.indexes.append(kwargs)
        if kwargs["field_name"] == "doc_id":
            raise RuntimeError("index failure must not break ensure_collection")


async def test_server_mode_creates_collection_schema_and_payload_indexes() -> None:
    client = RecordingServerClient(exists=[False])
    instance = KBVectorStore()
    instance._client = client  # type: ignore[assignment]
    instance._server_mode = True
    name = await instance.ensure_collection(DIM, MODEL)
    assert name == collection_name_for(DIM, MODEL)
    (created,) = client.created
    dense = created["vectors_config"]["dense"]
    assert (dense.size, dense.distance.value) == (DIM, "Cosine")
    assert created["sparse_vectors_config"]["bm25"].modifier.value == "idf"
    kb_index, doc_index = client.indexes
    assert kb_index["field_name"] == "kb_id" and kb_index["field_schema"].is_tenant is True
    assert doc_index["field_name"] == "doc_id"
    await instance.ensure_collection(DIM, MODEL)  # cached: no more calls
    assert len(client.created) == 1


async def test_ensure_collection_tolerates_concurrent_creation() -> None:
    client = RecordingServerClient(
        exists=[False, True], create_error=RuntimeError("already exists")
    )
    instance = KBVectorStore()
    instance._client = client  # type: ignore[assignment]
    assert await instance.ensure_collection(DIM, MODEL) == collection_name_for(DIM, MODEL)

    failing = RecordingServerClient(exists=[False, False], create_error=RuntimeError("boom"))
    other = KBVectorStore()
    other._client = failing  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="boom"):
        await other.ensure_collection(DIM, MODEL)


async def test_local_mode_skips_payload_indexes(
    store: KBVectorStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await store._get_client()
    calls: list[Any] = []

    async def fail(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(client, "create_payload_index", fail)
    await store.ensure_collection(DIM, MODEL)
    assert calls == []
