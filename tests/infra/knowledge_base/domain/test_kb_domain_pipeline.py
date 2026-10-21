"""Document pipeline: state transitions, failure messages, signature lock and orphan guards."""

from __future__ import annotations

from src.infra.knowledge_base import embedding, parsing, pipeline
from src.infra.knowledge_base import objects as kb_objects
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.utils.datetime import utc_now

KB = "kb1"


async def _setup(kb_env, text: str = "alpha beta\ngamma delta\n\nepsilon", **kb_extra) -> str:
    storage = get_kb_storage()
    await storage.insert_base(
        {
            "_id": KB,
            "owner_id": "u1",
            "name": "库",
            "status": "active",
            "chunk_preset": "general",
            "chunk_token_num": 512,
            "overlap_percent": 0,
            "embedding": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
            **kb_extra,
        }
    )
    key = kb_objects.original_key(KB, "d1", ".md")
    kb_env.objects.objects[key] = text.encode()
    await storage.insert_document(
        {
            "_id": "d1",
            "kb_id": KB,
            "filename": "a.md",
            "ext": ".md",
            "mime": "text/markdown",
            "size": len(text),
            "sha256": "s1",
            "object_key": key,
            "markdown_key": None,
            "status": "uploaded",
            "error": None,
            "chunk_count": 0,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    return "d1"


async def test_happy_path_parses_chunks_embeds_and_indexes(kb_env) -> None:
    doc_id = await _setup(kb_env)
    outcome = await pipeline.process_document(doc_id, "task1")
    assert outcome == pipeline.OUTCOME_INDEXED

    storage = get_kb_storage()
    doc = await storage.get_document_by_id(doc_id)
    assert doc["status"] == "indexed"
    assert doc["parse_method"] == "local"
    assert doc["markdown_key"] == f"kb/{KB}/d1/parsed.md"
    assert doc["line_count"] == 4
    assert doc["chunk_count"] == 3 and doc["token_count"] == 5
    assert doc["error"] is None and doc["indexed_at"] is not None
    assert doc["processing_task_id"] == "task1"
    assert kb_env.objects.objects[doc["markdown_key"]] == "alpha beta\ngamma delta\n\nepsilon".encode()

    chunks, total = await storage.list_chunks(doc_id)
    assert total == 3 and [c["_id"] for c in chunks] == ["d1:0", "d1:1", "d1:2"]
    assert chunks[1]["start_line"] == 2 and chunks[2]["start_line"] == 4

    kb = await storage.get_base(KB)
    signature = kb["embedding"]
    assert signature["model_name"] == "fake-embed" and signature["dim"] == 8
    assert signature["model_id"] == "card-1"
    assert signature["collection"].startswith("kb_8_")
    points = kb_env.vectors.collections[signature["collection"]]
    assert sorted(p.chunk_id for p in points) == ["d1:0", "d1:1", "d1:2"]
    assert kb_env.embed_calls == [["alpha beta", "gamma delta", "epsilon"]]


async def test_markdown_line_breaks_are_normalised_for_stable_line_numbers(kb_env) -> None:
    doc_id = await _setup(kb_env, "one\r\ntwo\rthree\u2028four")
    await pipeline.process_document(doc_id, "t")
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["line_count"] == 4
    assert kb_env.objects.objects[doc["markdown_key"]] == b"one\ntwo\nthree\nfour"


async def test_reindexing_replaces_previous_chunks_and_vectors(kb_env) -> None:
    doc_id = await _setup(kb_env)
    await pipeline.process_document(doc_id, "t1")
    storage = get_kb_storage()
    await storage.transition_document(doc_id, ["indexed"], {"status": "parsed"})
    await pipeline.process_document(doc_id, "t2")

    doc = await storage.get_document_by_id(doc_id)
    assert doc["status"] == "indexed"
    _, total = await storage.list_chunks(doc_id)
    assert total == 3
    collection = (await storage.get_base(KB))["embedding"]["collection"]
    assert len(kb_env.vectors.collections[collection]) == 3
    # parse stage is skipped on the second pass (document was already ``parsed``)
    assert kb_env.embed_calls and len(kb_env.embed_calls) == 2


async def test_parse_error_message_is_user_visible(kb_env) -> None:
    doc_id = await _setup(kb_env)
    kb_env.parse_error = parsing.ParseError("PDF 没有可提取的文本层")
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_FAILED
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_parsing"
    assert doc["error"]["stage"] == "parse"
    assert doc["error"]["message"] == "PDF 没有可提取的文本层"
    assert doc["error"]["at"] is not None


async def test_unexpected_parse_exception_gets_generic_message(kb_env, caplog) -> None:
    doc_id = await _setup(kb_env)
    kb_env.parse_error = RuntimeError("secret internal path /srv/x")
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_FAILED
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_parsing"
    assert doc["error"]["message"] == pipeline.PARSE_FAILED_MESSAGE
    assert "secret" not in doc["error"]["message"]


async def test_embedding_error_marks_index_failure(kb_env) -> None:
    doc_id = await _setup(kb_env)
    kb_env.embed_error = embedding.EmbeddingError("向量服务暂时不可用")
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_FAILED
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_indexing"
    assert doc["error"]["stage"] == "index" and doc["error"]["message"] == "向量服务暂时不可用"
    assert doc["markdown_key"]  # parsing result is kept so a retry skips straight to indexing
    assert (await get_kb_storage().get_base(KB))["embedding"] is None


async def test_unexpected_index_exception_gets_generic_message(kb_env) -> None:
    doc_id = await _setup(kb_env)
    kb_env.embed_error = ValueError("boom")
    await pipeline.process_document(doc_id, "t")
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["error"]["message"] == pipeline.INDEX_FAILED_MESSAGE


async def test_missing_embedding_model_fails_with_admin_hint(kb_env) -> None:
    doc_id = await _setup(kb_env)
    kb_env.card_missing = True
    await pipeline.process_document(doc_id, "t")
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_indexing"
    assert doc["error"]["message"] == pipeline.NO_EMBEDDING_MODEL_MESSAGE


async def test_empty_chunking_result_fails(kb_env) -> None:
    doc_id = await _setup(kb_env, "\n\n  \n")
    await pipeline.process_document(doc_id, "t")
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_indexing"
    assert doc["error"]["message"] == pipeline.NO_CONTENT_MESSAGE


async def test_changed_embedding_model_requires_reindex(kb_env) -> None:
    doc_id = await _setup(
        kb_env,
        embedding={
            "model_id": "old",
            "model_name": "older-model",
            "dim": 8,
            "collection": "kb_8_old",
        },
    )
    await pipeline.process_document(doc_id, "t")
    doc = await get_kb_storage().get_document_by_id(doc_id)
    assert doc["status"] == "error_indexing"
    assert doc["error"]["message"] == pipeline.MODEL_CHANGED_MESSAGE
    assert kb_env.vectors.collections == {}


async def test_matching_signature_reuses_collection(kb_env) -> None:
    doc_id = await _setup(kb_env)
    await pipeline.process_document(doc_id, "t")
    signature = (await get_kb_storage().get_base(KB))["embedding"]

    # second document of the same KB indexes into the locked collection
    storage = get_kb_storage()
    await storage.insert_document(
        {
            "_id": "d2",
            "kb_id": KB,
            "filename": "b.md",
            "sha256": "s2",
            "object_key": "k2",
            "status": "parsed",
            "markdown_key": "kb/kb1/d2/parsed.md",
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    kb_env.objects.objects["kb/kb1/d2/parsed.md"] = b"zeta"
    assert await pipeline.process_document("d2", "t") == pipeline.OUTCOME_INDEXED
    assert (await storage.get_base(KB))["embedding"] == signature
    assert len(kb_env.vectors.collections) == 1


async def test_lost_signature_race_adopts_winner_or_fails_on_mismatch(kb_env, monkeypatch) -> None:
    doc_id = await _setup(kb_env)
    storage = get_kb_storage()
    real_lock = storage.lock_embedding_signature

    async def lose(kb_id, signature):
        # another document won the race with a different model
        await real_lock(
            kb_id,
            {"model_id": "x", "model_name": "other", "dim": 8, "collection": "kb_8_other"},
        )
        return await real_lock(kb_id, signature)

    monkeypatch.setattr(storage, "lock_embedding_signature", lose)
    await pipeline.process_document(doc_id, "t")
    doc = await storage.get_document_by_id(doc_id)
    assert doc["status"] == "error_indexing"
    assert doc["error"]["message"] == pipeline.MODEL_CHANGED_MESSAGE


async def test_document_deleted_before_start_is_skipped(kb_env) -> None:
    assert await pipeline.process_document("ghost", "t") == pipeline.OUTCOME_SKIPPED


async def test_document_in_other_state_is_skipped(kb_env) -> None:
    doc_id = await _setup(kb_env)
    storage = get_kb_storage()
    for status in ("indexed", "error_parsing", "error_indexing", "parsing", "indexing"):
        await storage.documents.update_one({"_id": doc_id}, {"$set": {"status": status}})
        assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_SKIPPED
    assert kb_env.embed_calls == []


async def test_document_deleted_while_parsing_leaves_no_markdown(kb_env, monkeypatch) -> None:
    doc_id = await _setup(kb_env)
    storage = get_kb_storage()
    original_parse = parsing.parse_document

    async def parse_then_delete(*args, **kwargs):
        result = await original_parse(*args, **kwargs)
        await storage.documents.delete_many({"_id": doc_id})
        return result

    monkeypatch.setattr(parsing, "parse_document", parse_then_delete)
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_SKIPPED
    assert not any(k.endswith("parsed.md") for k in kb_env.objects.objects)


async def test_document_deleted_while_indexing_removes_orphan_vectors(kb_env, monkeypatch) -> None:
    doc_id = await _setup(kb_env)
    storage = get_kb_storage()
    original_upsert = kb_env.vectors.upsert_chunks

    async def upsert_then_delete(collection, points):
        await original_upsert(collection, points)
        await storage.documents.delete_many({"_id": doc_id})

    monkeypatch.setattr(kb_env.vectors, "upsert_chunks", upsert_then_delete)
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_SKIPPED
    collection = (await storage.get_base(KB))["embedding"]["collection"]
    assert kb_env.vectors.collections[collection] == []
    assert (await storage.list_chunks(doc_id))[1] == 0


async def test_kb_deleted_before_indexing_skips(kb_env) -> None:
    doc_id = await _setup(kb_env)
    await get_kb_storage().bases.update_one({"_id": KB}, {"$set": {"status": "deleting"}})
    assert await pipeline.process_document(doc_id, "t") == pipeline.OUTCOME_SKIPPED
    assert kb_env.vectors.collections == {}
