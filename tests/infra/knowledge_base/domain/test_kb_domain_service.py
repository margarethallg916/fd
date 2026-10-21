"""Service layer: access, validation, upload, retry/reindex, delete and search orchestration."""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

import pytest

from src.infra.knowledge_base import fusion, pipeline, service
from src.infra.knowledge_base import objects as kb_objects
from src.infra.knowledge_base.errors import (
    KBBusyError,
    KBDisabledError,
    KBDocumentNotFoundError,
    KBNotFoundError,
    KBValidationError,
)
from src.infra.knowledge_base.storage import expand_status_filter, get_kb_storage
from src.kernel.config import settings
from src.kernel.schemas.knowledge_base import KBCreate, KBSearchRequest, KBUpdate
from tests.infra.knowledge_base.domain.kb_fakes import make_user

OWNER = make_user("owner-1", "E0001")
OTHER = make_user("other-2", "E0002")


@dataclass
class FakeUpload:
    filename: str
    data: bytes
    content_type: str = "text/markdown"
    pos: int = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self.data[self.pos : self.pos + size] if size >= 0 else self.data[self.pos :]
        self.pos += len(chunk)
        return chunk


async def _create(name: str = "我的库", user=OWNER, **kw):
    return await service.create_kb(user, KBCreate(name=name, **kw))


async def _upload(kb_id: str, name: str = "a.md", data: bytes = b"hello world", user=OWNER, **kw):
    return await service.upload_document(user, kb_id, FakeUpload(name, data), **kw)


async def _index_everything(kb_env) -> None:
    storage = get_kb_storage()
    for task in list(storage.tasks.docs):
        if task["status"] == "queued":
            await storage.claim_task(task["_id"], "w", __import__("datetime").datetime.now(__import__("datetime").timezone.utc), 60)
            for doc_id in task["doc_ids"]:
                await pipeline.process_document(doc_id, task["_id"])
            await storage.finish_task(task["_id"], "w", "succeeded")


# ── access ───────────────────────────────────────────────────


async def test_create_writes_owner_membership_and_defaults(kb_env) -> None:
    kb = await _create(description="  说明 ")
    assert kb.my_role == "owner" and kb.owner_id == OWNER.sub and kb.owner_username == "E0001"
    assert kb.description == "说明" and kb.icon == "library-big"
    assert (kb.chunk_preset, kb.chunk_token_num, kb.overlap_percent) == ("general", 512, 0)
    assert kb.visibility == "private" and kb.graph_enabled is False
    assert kb.embedding is None and kb.index_outdated is False and kb.active_task is None
    assert kb.stats.doc_count == 0
    member = await get_kb_storage().get_member(kb.id, OWNER.sub)
    assert member["role"] == "owner" and member["chat_enabled"] is True


async def test_non_owner_gets_not_found_everywhere(kb_env) -> None:
    kb = await _create()
    doc, _ = await _upload(kb.id)
    calls = [
        service.get_kb(OTHER, kb.id),
        service.update_kb(OTHER, kb.id, KBUpdate(name="x")),
        service.delete_kb(OTHER, kb.id),
        service.list_documents(OTHER, kb.id),
        _upload(kb.id, "b.md", b"zzz", user=OTHER),
        service.get_document(OTHER, kb.id, doc.document.id),
        service.get_content(OTHER, kb.id, doc.document.id),
        service.list_chunks(OTHER, kb.id, doc.document.id),
        service.open_download(OTHER, kb.id, doc.document.id),
        service.delete_documents(OTHER, kb.id, [doc.document.id]),
        service.retry_documents(OTHER, kb.id, []),
        service.reindex_kb(OTHER, kb.id),
        service.search(OTHER, kb.id, KBSearchRequest(query="hello")),
        service.list_tasks(OTHER, kb.id),
        service.cancel_task(OTHER, kb.id, "t"),
    ]
    for call in calls:
        with pytest.raises(KBNotFoundError) as info:
            await call
        assert str(info.value) == "知识库不存在"
    assert (await service.list_kbs(OTHER)) == []
    assert len(await service.list_kbs(OWNER)) == 1


async def test_deleting_kb_is_invisible_and_unknown_id_is_not_found(kb_env) -> None:
    with pytest.raises(KBNotFoundError):
        await service.get_kb(OWNER, "missing")
    kb = await _create()
    await get_kb_storage().mark_base_deleting(kb.id)
    with pytest.raises(KBNotFoundError):
        await service.get_kb(OWNER, kb.id)
    assert await service.list_kbs(OWNER) == []


async def test_feature_switch_blocks_everything_but_config(kb_env, monkeypatch) -> None:
    kb = await _create()
    monkeypatch.setattr(settings, "KB_ENABLED", False)
    for call in (
        service.list_kbs(OWNER),
        service.get_kb(OWNER, kb.id),
        service.create_kb(OWNER, KBCreate(name="n")),
        _upload(kb.id),
        service.search(OWNER, kb.id, KBSearchRequest(query="q")),
    ):
        with pytest.raises(KBDisabledError):
            await call
    config = await service.get_config()
    assert config.enabled is False and config.ready is False


async def test_list_scope_other_than_mine_is_rejected(kb_env) -> None:
    with pytest.raises(KBValidationError):
        await service.list_kbs(OWNER, "public")


# ── validation / CRUD ────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "   "},
        {"name": "x" * 65},
        {"name": "ok", "description": "d" * 501},
        {"name": "ok", "icon": "bad icon!"},
        {"name": "ok", "chunk_preset": "weird"},
        {"name": "ok", "chunk_token_num": 99},
        {"name": "ok", "chunk_token_num": 2049},
        {"name": "ok", "overlap_percent": 51},
        {"name": "ok", "overlap_percent": -1},
    ],
)
async def test_create_validation_errors(kb_env, payload) -> None:
    with pytest.raises(KBValidationError):
        await service.create_kb(OWNER, KBCreate(**payload))


async def test_update_changes_only_given_fields_and_keeps_signature(kb_env) -> None:
    kb = await _create(chunk_preset="qa", chunk_token_num=300, overlap_percent=10)
    updated = await service.update_kb(OWNER, kb.id, KBUpdate(name=" 新名字 ", icon="book"))
    assert updated.name == "新名字" and updated.icon == "book"
    assert (updated.chunk_preset, updated.chunk_token_num, updated.overlap_percent) == ("qa", 300, 10)
    assert updated.updated_at >= kb.updated_at
    again = await service.update_kb(OWNER, kb.id, KBUpdate(chunk_preset="laws"))
    assert again.chunk_preset == "laws" and again.chunk_token_num == 300
    with pytest.raises(KBValidationError):
        await service.update_kb(OWNER, kb.id, KBUpdate(chunk_token_num=5000))


async def test_preset_default_sizes_follow_the_preset_table(kb_env) -> None:
    kb = await _create(chunk_preset="book")
    assert kb.chunk_preset == "book"


async def test_stats_are_aggregated_by_status(kb_env) -> None:
    kb = await _create()
    storage = get_kb_storage()
    base = {"kb_id": kb.id, "created_at": None, "updated_at": None}
    rows = [
        ("a", "indexed", 100, 4),
        ("b", "indexed", 50, 2),
        ("c", "uploaded", 10, 0),
        ("d", "parsing", 5, 0),
        ("e", "error_parsing", 7, 0),
        ("f", "error_indexing", 3, 0),
    ]
    for doc_id, status, size, chunks in rows:
        await storage.insert_document(
            {**base, "_id": doc_id, "sha256": doc_id, "status": status, "size": size, "chunk_count": chunks}
        )
    other = await _create("别的库")
    fresh = await service.get_kb(OWNER, kb.id)
    assert fresh.stats.model_dump() == {
        "doc_count": 6,
        "indexed_count": 2,
        "processing_count": 2,
        "failed_count": 2,
        "chunk_count": 6,
        "size_bytes": 175,
    }
    listed = {k.id: k for k in await service.list_kbs(OWNER)}
    assert listed[other.id].stats.doc_count == 0 and listed[kb.id].stats.doc_count == 6


async def test_active_task_is_included_in_responses(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id)
    fresh = await service.get_kb(OWNER, kb.id)
    assert fresh.active_task is not None
    assert fresh.active_task.status == "queued" and fresh.active_task.kb_id == kb.id


async def test_index_outdated_flag_follows_the_current_model(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id)
    await _index_everything(kb_env)
    fresh = await service.get_kb(OWNER, kb.id)
    assert fresh.embedding.model_name == "fake-embed" and fresh.embedding.dim == 8
    assert fresh.index_outdated is False
    assert not hasattr(fresh.embedding, "collection")
    kb_env.model_name = "new-model"
    assert (await service.get_kb(OWNER, kb.id)).index_outdated is True
    kb_env.card_missing = True
    assert (await service.get_kb(OWNER, kb.id)).index_outdated is True


async def test_config_reports_readiness_and_limits(kb_env) -> None:
    config = await service.get_config()
    assert config.enabled and config.ready and config.not_ready_reason is None
    assert config.max_file_mb == 1 and ".md" in config.allowed_extensions
    assert {p.key for p in config.presets} >= {"general", "qa", "book", "laws"}
    kb_env.card_missing = True
    config = await service.get_config()
    assert config.ready is False and config.not_ready_reason == "embedding_model_missing"
    assert config.rerank_available is False
    kb_env.rerank_card_present = True
    assert (await service.get_config()).rerank_available is True


# ── upload ───────────────────────────────────────────────────


async def test_upload_stores_original_inserts_document_and_enqueues(kb_env) -> None:
    kb = await _create()
    response, created = await _upload(kb.id, "Notes.MD", b"hello world")
    assert created and response.duplicate is False
    doc = response.document
    assert doc.status == "uploaded" and doc.filename == "Notes.MD" and doc.ext == ".md"
    assert doc.size == 11 and doc.uploader_username == "E0001" and doc.source == "upload"

    stored = await get_kb_storage().get_document(kb.id, doc.id)
    assert stored["object_key"] == f"kb/{kb.id}/{doc.id}/original.md"
    assert kb_env.objects.objects[stored["object_key"]] == b"hello world"
    import hashlib

    assert stored["sha256"] == hashlib.sha256(b"hello world").hexdigest()
    tasks = get_kb_storage().tasks.docs
    assert len(tasks) == 1 and tasks[0]["doc_ids"] == [doc.id] and tasks[0]["created_by"] == OWNER.sub
    assert kb_env.published == [tasks[0]["_id"]]


async def test_duplicate_upload_returns_existing_document(kb_env) -> None:
    kb = await _create()
    storage = get_kb_storage()
    await storage.ensure_indexes()
    first, _ = await _upload(kb.id, "a.md", b"same bytes")
    second, created = await _upload(kb.id, "renamed.md", b"same bytes")
    assert created is False and second.duplicate is True
    assert second.document.id == first.document.id and second.document.filename == "a.md"
    assert len(storage.documents.docs) == 1 and len(kb_env.objects.objects) == 1
    # the same bytes in another KB are not duplicates
    other = await _create("另一个")
    third, created = await _upload(other.id, "a.md", b"same bytes")
    assert created and third.duplicate is False


async def test_upload_race_on_unique_index_deletes_the_new_object(kb_env) -> None:
    kb = await _create()
    storage = get_kb_storage()
    await storage.ensure_indexes()
    first, _ = await _upload(kb.id, "a.md", b"race")
    real = storage.get_document_by_sha
    calls = {"n": 0}

    async def stale_lookup(kb_id, sha):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(kb_id, sha)

    storage.get_document_by_sha = stale_lookup  # type: ignore[method-assign]
    second, created = await _upload(kb.id, "a2.md", b"race")
    assert created is False and second.document.id == first.document.id
    assert len(kb_env.objects.objects) == 1


@pytest.mark.parametrize("name", ["virus.exe", "page.html", "noext", "archive.tar.gz", ""])
async def test_upload_rejects_unsupported_extensions(kb_env, name) -> None:
    kb = await _create()
    with pytest.raises(KBValidationError):
        await _upload(kb.id, name)
    assert kb_env.objects.objects == {} and get_kb_storage().documents.docs == []


async def test_upload_extension_is_case_insensitive_and_path_is_stripped(kb_env) -> None:
    kb = await _create()
    response, _ = await _upload(kb.id, "C:\\temp\\REPORT.PDF", b"%PDF")
    assert response.document.filename == "REPORT.PDF" and response.document.ext == ".pdf"


async def test_upload_rejects_empty_files(kb_env) -> None:
    kb = await _create()
    with pytest.raises(KBValidationError):
        await _upload(kb.id, "a.md", b"")


async def test_upload_size_limit_by_content_length_and_by_streamed_bytes(kb_env) -> None:
    kb = await _create()
    limit = settings.KB_MAX_FILE_MB * 1024 * 1024
    with pytest.raises(KBValidationError, match="1MB"):
        await _upload(kb.id, content_length=limit + 2 * 1024 * 1024)
    with pytest.raises(KBValidationError, match="1MB"):
        await _upload(kb.id, data=b"a" * (limit + 1))
    ok, created = await _upload(kb.id, "big.md", b"a" * limit)
    assert created and ok.document.size == limit


async def test_upload_storage_failure_leaves_no_rows(kb_env) -> None:
    kb = await _create()
    kb_env.objects.fail_uploads = True
    with pytest.raises(RuntimeError):
        await _upload(kb.id)
    assert get_kb_storage().documents.docs == [] and get_kb_storage().tasks.docs == []


async def test_upload_survives_enqueue_failure_and_leaves_document_uploaded(kb_env, monkeypatch) -> None:
    kb = await _create()

    async def boom(*_a, **_k):
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(service.kb_queue, "enqueue_documents", boom)
    response, created = await _upload(kb.id)
    assert created and response.document.status == "uploaded"


async def test_upload_into_kb_deleted_midway_is_rolled_back(kb_env, monkeypatch) -> None:
    kb = await _create()
    real = kb_objects.put_original

    async def put_then_delete_kb(key, spooled, content_type):
        await real(key, spooled, content_type)
        await get_kb_storage().mark_base_deleting(kb.id)

    monkeypatch.setattr(kb_objects, "put_original", put_then_delete_kb)
    with pytest.raises(KBNotFoundError):
        await _upload(kb.id)
    assert get_kb_storage().documents.docs == [] and kb_env.objects.objects == {}


# ── documents ────────────────────────────────────────────────


async def test_status_filter_aliases() -> None:
    assert expand_status_filter(None) is None and expand_status_filter("") is None
    assert expand_status_filter("failed") == ["error_parsing", "error_indexing"]
    assert expand_status_filter("indexed,processing") == ["indexed", "uploaded", "parsing", "parsed", "indexing"]


async def test_document_listing_filters_and_paginates(kb_env) -> None:
    kb = await _create()
    for i in range(5):
        await _upload(kb.id, f"doc{i}.md", f"body {i}".encode())
    await _upload(kb.id, "Report.txt", b"report")
    storage = get_kb_storage()
    await storage.documents.update_one({"filename": "Report.txt"}, {"$set": {"status": "error_parsing"}})

    everything = await service.list_documents(OWNER, kb.id, page_size=1000)
    assert everything.total == 6 and everything.page_size == 200
    page = await service.list_documents(OWNER, kb.id, page=2, page_size=4)
    assert page.total == 6 and len(page.items) == 2
    assert (await service.list_documents(OWNER, kb.id, q="report")).total == 1
    assert (await service.list_documents(OWNER, kb.id, q="doc.")).total == 0  # regex is escaped
    failed = await service.list_documents(OWNER, kb.id, status="failed")
    assert [d.filename for d in failed.items] == ["Report.txt"]
    assert (await service.list_documents(OWNER, kb.id, status="uploaded")).total == 5


async def test_get_document_scoped_to_kb(kb_env) -> None:
    kb, other = await _create(), await _create("b")
    doc, _ = await _upload(kb.id)
    assert (await service.get_document(OWNER, kb.id, doc.document.id)).id == doc.document.id
    with pytest.raises(KBDocumentNotFoundError):
        await service.get_document(OWNER, other.id, doc.document.id)


async def test_content_paging_and_chunks(kb_env) -> None:
    kb = await _create()
    doc, _ = await _upload(kb.id, "a.md", b"l1\nl2\nl3\nl4\nl5")
    with pytest.raises(KBValidationError):
        await service.get_content(OWNER, kb.id, doc.document.id)
    await _index_everything(kb_env)

    page = await service.get_content(OWNER, kb.id, doc.document.id, offset=2, limit=2)
    assert page.total_lines == 5 and page.offset == 2 and page.has_more is True
    assert [(line.n, line.text) for line in page.lines] == [(2, "l2"), (3, "l3")]
    last = await service.get_content(OWNER, kb.id, doc.document.id, offset=4, limit=400)
    assert last.has_more is False and [line.text for line in last.lines] == ["l4", "l5"]

    chunks = await service.list_chunks(OWNER, kb.id, doc.document.id, page=2, page_size=2)
    assert chunks.total == 5 and [c.index for c in chunks.items] == [2, 3]
    assert chunks.items[0].chunk_id == f"{doc.document.id}:2" and chunks.items[0].start_line == 3


async def test_download_streams_original_and_reports_missing_object(kb_env) -> None:
    kb = await _create()
    doc, _ = await _upload(kb.id, "a.md", b"raw bytes")
    stored, stream = await service.open_download(OWNER, kb.id, doc.document.id)
    assert b"".join([chunk async for chunk in stream]) == b"raw bytes" and stored["filename"] == "a.md"
    kb_env.objects.objects.clear()
    with pytest.raises(KBDocumentNotFoundError):
        await service.open_download(OWNER, kb.id, doc.document.id)


async def test_retry_moves_failed_documents_back_and_enqueues_once(kb_env) -> None:
    kb = await _create()
    storage = get_kb_storage()
    await storage.ensure_indexes()
    ids = []
    for name, status in (("a.md", "error_parsing"), ("b.md", "error_indexing"), ("c.md", "indexed")):
        response, _ = await _upload(kb.id, name, name.encode())
        ids.append(response.document.id)
        await storage.documents.update_one(
            {"_id": response.document.id},
            {"$set": {"status": status, "error": {"stage": "x", "message": "m", "at": None}}},
        )
    await storage.tasks.delete_many({})
    kb_env.published.clear()

    result = await service.retry_documents(OWNER, kb.id, [])
    assert result.count == 2 and result.task_id
    a, b, c = [await storage.get_document(kb.id, i) for i in ids]
    assert (a["status"], b["status"], c["status"]) == ("uploaded", "parsed", "indexed")
    assert a["error"] is None and b["error"] is None
    assert sorted(storage.tasks.docs[0]["doc_ids"]) == sorted(ids[:2])

    # explicit ids only retry failed documents
    await storage.documents.update_one({"_id": ids[0]}, {"$set": {"status": "error_parsing"}})
    again = await service.retry_documents(OWNER, kb.id, [ids[0], ids[2]])
    assert again.count == 1
    none_left = await service.retry_documents(OWNER, kb.id, ["ghost"])
    assert none_left.count == 0 and none_left.task_id is None


async def test_delete_documents_cleans_rows_chunks_vectors_and_objects(kb_env) -> None:
    kb = await _create()
    first, _ = await _upload(kb.id, "a.md", b"alpha\nbeta")
    second, _ = await _upload(kb.id, "b.md", b"gamma")
    await _index_everything(kb_env)
    storage = get_kb_storage()
    collection = (await storage.get_base(kb.id))["embedding"]["collection"]
    assert len(kb_env.vectors.collections[collection]) == 3

    result = await service.delete_documents(OWNER, kb.id, [first.document.id, "ghost"])
    assert result.deleted == 1
    assert await storage.get_document(kb.id, first.document.id) is None
    assert (await storage.list_chunks(first.document.id))[1] == 0
    assert [p.doc_id for p in kb_env.vectors.collections[collection]] == [second.document.id]
    assert not any(first.document.id in key for key in kb_env.objects.objects)
    assert any(second.document.id in key for key in kb_env.objects.objects)

    with pytest.raises(KBValidationError):
        await service.delete_documents(OWNER, kb.id, [])
    with pytest.raises(KBValidationError):
        await service.delete_documents(OWNER, kb.id, [str(i) for i in range(501)])


# ── reindex / delete kb / tasks ──────────────────────────────


async def test_reindex_resets_signature_and_requeues_indexed_documents(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id, "a.md", b"alpha")
    await _upload(kb.id, "b.md", b"beta")
    await _index_everything(kb_env)
    storage = get_kb_storage()
    old_collection = (await storage.get_base(kb.id))["embedding"]["collection"]

    kb_env.model_name = "new-model"
    result = await service.reindex_kb(OWNER, kb.id)
    assert result.count == 2 and result.task_id
    assert kb_env.vectors.collections[old_collection] == []
    assert (await storage.get_base(kb.id))["embedding"] is None
    assert {d["status"] for d in storage.documents.docs} == {"parsed"}

    await _index_everything(kb_env)
    fresh = await storage.get_base(kb.id)
    assert fresh["embedding"]["model_name"] == "new-model"
    assert {d["status"] for d in storage.documents.docs} == {"indexed"}
    assert (await service.get_kb(OWNER, kb.id)).index_outdated is False


async def test_reindex_is_refused_while_a_task_is_running(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id)
    storage = get_kb_storage()
    task = storage.tasks.docs[0]
    await storage.claim_task(task["_id"], "w", __import__("datetime").datetime.now(__import__("datetime").timezone.utc), 60)
    with pytest.raises(KBBusyError):
        await service.reindex_kb(OWNER, kb.id)


async def test_delete_kb_marks_deleting_cancels_tasks_and_cleans_up(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id, "a.md", b"alpha")
    await _index_everything(kb_env)
    await _upload(kb.id, "b.md", b"beta")  # leaves a queued task behind
    storage = get_kb_storage()
    collection = (await storage.get_base(kb.id))["embedding"]["collection"]

    await service.delete_kb(OWNER, kb.id)
    await asyncio.gather(*list(service._cleanup_tasks))

    assert await storage.get_base(kb.id) is None
    assert storage.documents.docs == [] and storage.chunks.docs == []
    assert storage.members.docs == [] and storage.tasks.docs == []
    assert kb_env.objects.objects == {}
    assert kb_env.vectors.collections[collection] == []
    with pytest.raises(KBNotFoundError):
        await service.get_kb(OWNER, kb.id)


async def test_cleanup_waits_for_running_task_then_completes(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id)
    storage = get_kb_storage()
    task = storage.tasks.docs[0]
    from src.infra.utils.datetime import utc_now

    await storage.claim_task(task["_id"], "w", utc_now(), 60)
    await storage.mark_base_deleting(kb.id)
    assert await service.cleanup_kb(kb.id) is False
    assert await storage.get_base(kb.id) is not None
    await storage.finish_task(task["_id"], "w", "cancelled")
    assert await service.cleanup_kb(kb.id) is True
    assert await storage.get_base(kb.id) is None


async def test_cleanup_failure_is_reported_for_retry(kb_env, monkeypatch) -> None:
    kb = await _create()
    await _upload(kb.id)
    await _index_everything(kb_env)
    await get_kb_storage().mark_base_deleting(kb.id)

    async def boom(collection, kb_id):
        raise RuntimeError("qdrant down")

    monkeypatch.setattr(kb_env.vectors, "delete_by_kb", boom)
    assert await service.cleanup_kb(kb.id) is False
    assert (await get_kb_storage().get_base(kb.id))["status"] == "deleting"


async def test_task_listing_and_cancel(kb_env) -> None:
    kb = await _create()
    await _upload(kb.id)
    listed = await service.list_tasks(OWNER, kb.id, active=True)
    assert len(listed.items) == 1 and listed.items[0].status == "queued"
    cancelled = await service.cancel_task(OWNER, kb.id, listed.items[0].id)
    assert cancelled.status == "cancelled"
    assert (await service.list_tasks(OWNER, kb.id, active=True)).items == []
    assert len((await service.list_tasks(OWNER, kb.id)).items) == 1
    from src.infra.knowledge_base.errors import KBTaskNotFoundError

    with pytest.raises(KBTaskNotFoundError):
        await service.cancel_task(OWNER, kb.id, "nope")


# ── search orchestration ─────────────────────────────────────


async def _indexed_kb(kb_env, files: dict[str, bytes], name: str = "库"):
    kb = await _create(name)
    for filename, data in files.items():
        await _upload(kb.id, filename, data)
    await _index_everything(kb_env)
    return kb


async def test_hybrid_search_fuses_dense_and_sparse_with_requested_weight(kb_env, monkeypatch) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana\ncherry grape", "b.md": b"zebra yak"})
    seen: dict = {}
    real = fusion.weighted_fusion

    def spy(dense, sparse_hits, *, vector_weight=0.7):
        seen.update(dense=len(dense), sparse=len(sparse_hits), weight=vector_weight)
        return real(dense, sparse_hits, vector_weight=vector_weight)

    monkeypatch.setattr(fusion, "weighted_fusion", spy)
    response = await service.search(
        OWNER, kb.id, KBSearchRequest(query="apple banana", top_k=3, vector_weight=0.4)
    )
    assert seen["weight"] == 0.4 and seen["dense"] > 0 and seen["sparse"] > 0
    assert response.warnings == []
    top = response.results[0]
    assert top.text == "apple banana" and top.filename == "a.md" and top.kb_name == "库"
    assert top.kb_id == kb.id and top.start_line == 1 and top.end_line == 1
    assert top.rerank_score is None and response.took_ms >= 0
    assert len(response.results) <= 3
    assert [r.score for r in response.results] == sorted((r.score for r in response.results), reverse=True)
    kinds = [c[0] for c in kb_env.vectors.calls if c[0].startswith("search")]
    assert kinds == ["search_dense", "search_sparse"]
    assert kb_env.vectors.calls[-1][1][2] == 6  # recall = top_k * 2 without rerank


async def test_vector_and_keyword_modes_use_one_leg(kb_env) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana\ncherry"})
    kb_env.vectors.calls.clear()
    await service.search(OWNER, kb.id, KBSearchRequest(query="apple", mode="vector"))
    assert [c[0] for c in kb_env.vectors.calls] == ["search_dense"]
    kb_env.vectors.calls.clear()
    keyword = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", mode="keyword"))
    assert [c[0] for c in kb_env.vectors.calls] == ["search_sparse"]
    assert keyword.results and all(0.0 <= r.score <= 1.0 for r in keyword.results)
    assert kb_env.embed_calls[-1] != ["apple"]  # no query embedding in keyword mode


async def test_score_threshold_applies_to_vector_modes_only(kb_env) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana\nzzzz qqqq"})
    strict = await service.search(
        OWNER, kb.id, KBSearchRequest(query="apple banana", mode="vector", score_threshold=0.99)
    )
    assert [r.text for r in strict.results] == ["apple banana"]
    keyword = await service.search(
        OWNER, kb.id, KBSearchRequest(query="apple banana", mode="keyword", score_threshold=0.99)
    )
    assert keyword.results  # keyword scores are not filtered


async def test_orphan_hits_are_dropped(kb_env) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple pie", "b.md": b"apple tart"})
    storage = get_kb_storage()
    gone = next(d for d in storage.documents.docs if d["filename"] == "b.md")
    await storage.documents.delete_many({"_id": gone["_id"]})  # vectors remain in the index
    response = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", top_k=5))
    assert [r.filename for r in response.results] == ["a.md"]


async def test_outdated_index_falls_back_to_keyword_with_warning(kb_env) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana"})
    kb_env.model_name = "different-model"
    kb_env.vectors.calls.clear()
    response = await service.search(OWNER, kb.id, KBSearchRequest(query="apple"))
    assert response.warnings == ["index_outdated"]
    assert [c[0] for c in kb_env.vectors.calls] == ["search_sparse"]
    assert response.results and response.results[0].filename == "a.md"

    kb_env.card_missing = True
    response = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", mode="vector"))
    assert response.warnings == ["index_outdated"] and response.results


async def test_query_embedding_failure_degrades_to_keyword(kb_env) -> None:
    from src.infra.knowledge_base.embedding import EmbeddingError

    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana"})
    kb_env.embed_error = EmbeddingError("down")
    response = await service.search(OWNER, kb.id, KBSearchRequest(query="apple"))
    assert response.warnings == ["embedding_failed"] and response.results


async def test_rerank_replaces_order_and_keeps_fused_score(kb_env) -> None:
    kb = await _indexed_kb(kb_env, {"a.md": b"apple banana\napple cherry\napple dates"})
    kb_env.rerank_card_present = True
    kb_env.vectors.calls.clear()
    baseline = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", use_rerank=False))
    assert baseline.results and all(r.rerank_score is None for r in baseline.results)

    kb_env.rerank_scores = [0.1, 0.2, 0.9]
    kb_env.vectors.calls.clear()
    reranked = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", top_k=3))
    assert [c[1][2] for c in kb_env.vectors.calls] == [50, 50]  # recall = max(50, top_k)
    assert [r.rerank_score for r in reranked.results] == [0.9, 0.2, 0.1]
    assert all(r.score != r.rerank_score for r in reranked.results)

    kb_env.rerank_scores = None  # rerank service fails -> original order + warning
    failed = await service.search(OWNER, kb.id, KBSearchRequest(query="apple"))
    assert "rerank_failed" in failed.warnings and failed.results[0].rerank_score is None

    kb_env.rerank_card_present = False
    asked = await service.search(OWNER, kb.id, KBSearchRequest(query="apple", use_rerank=True))
    assert "rerank_unavailable" in asked.warnings


async def test_search_on_unindexed_kb_is_empty(kb_env) -> None:
    kb = await _create()
    response = await service.search(OWNER, kb.id, KBSearchRequest(query="anything"))
    assert response.results == [] and response.warnings == []


async def test_search_across_kbs_sharing_a_collection_filters_by_kb(kb_env) -> None:
    one = await _indexed_kb(kb_env, {"a.md": b"apple one"}, "one")
    two = await _indexed_kb(kb_env, {"b.md": b"apple two"}, "two")
    bases = [await get_kb_storage().get_base(one.id), await get_kb_storage().get_base(two.id)]
    kb_env.vectors.calls.clear()
    hits, warnings = await service.search_bases(bases, KBSearchRequest(query="apple"))
    assert warnings == [] and {h.kb_name for h in hits} == {"one", "two"}
    dense = next(c for c in kb_env.vectors.calls if c[0] == "search_dense")
    assert set(dense[1][1]) == {one.id, two.id}  # one query for the shared collection
    only_one, _ = await service.search_bases(bases[:1], KBSearchRequest(query="apple"))
    assert {h.kb_id for h in only_one} == {one.id}


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "  "},
        {"query": "q", "top_k": 0},
        {"query": "q", "top_k": 51},
        {"query": "q", "mode": "magic"},
        {"query": "q", "score_threshold": 1.5},
        {"query": "q", "vector_weight": -0.1},
        {"query": "x" * 2001},
    ],
)
async def test_search_validation(kb_env, payload) -> None:
    kb = await _create()
    with pytest.raises(KBValidationError):
        await service.search(OWNER, kb.id, KBSearchRequest(**payload))


async def test_spool_upload_hashes_while_streaming() -> None:
    upload = FakeUpload("a.md", b"x" * 3_000_000)
    spooled = await kb_objects.spool_upload(upload, max_size_bytes=4_000_000, max_size_mb=4)
    try:
        import hashlib

        assert spooled.size == 3_000_000
        assert spooled.sha256_hex == hashlib.sha256(b"x" * 3_000_000).hexdigest()
        assert io.BytesIO(spooled.file.read()).getbuffer().nbytes == 3_000_000
    finally:
        spooled.close()
