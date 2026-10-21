"""Task queue: merge, claim CAS, fairness, cancel, lease recovery and orphan enqueue."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pymongo.errors import DuplicateKeyError

from src.infra.knowledge_base import pipeline
from src.infra.knowledge_base import queue as kb_queue
from src.infra.knowledge_base import runtime as kb_runtime
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.utils.datetime import utc_now
from tests.infra.knowledge_base.domain import kb_fakes


async def _doc(kb_id: str, doc_id: str, status: str = "uploaded", **extra) -> dict:
    doc = {
        "_id": doc_id,
        "kb_id": kb_id,
        "filename": f"{doc_id}.md",
        "sha256": doc_id,
        "status": status,
        "size": 10,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        **extra,
    }
    await get_kb_storage().insert_document(doc)
    return doc


async def _base(kb_id: str = "kb1", owner: str = "u1") -> None:
    await get_kb_storage().insert_base(
        {
            "_id": kb_id,
            "owner_id": owner,
            "name": "n",
            "status": "active",
            "embedding": None,
            "created_at": utc_now(),
            "updated_at": utc_now(),
        }
    )
    await get_kb_storage().insert_member(
        {"_id": f"m-{kb_id}", "kb_id": kb_id, "user_id": owner, "role": "owner"}
    )


async def test_ensure_indexes_declares_partial_unique_queued_index(kb_env) -> None:
    storage = get_kb_storage()
    await storage.ensure_indexes()
    index = next(i for i in storage.tasks.indexes if i.unique)
    assert index.keys == [("kb_id", 1), ("type", 1)]
    assert index.partial == {"status": "queued"}
    assert any(i.unique and i.keys == [("kb_id", 1), ("sha256", 1)] for i in storage.documents.indexes)


async def test_enqueue_merges_into_queued_task_until_it_is_claimed(kb_env) -> None:
    storage = get_kb_storage()
    await storage.ensure_indexes()
    first = await kb_queue.enqueue_documents("kb1", ["d1"], "u1")
    second = await kb_queue.enqueue_documents("kb1", ["d2", "d1"], "u1")
    assert first["_id"] == second["_id"]
    assert second["doc_ids"] == ["d1", "d2"]
    assert kb_env.published == [first["_id"], first["_id"]]

    claimed = await storage.claim_task(first["_id"], "w1", utc_now(), 60)
    assert claimed["status"] == "running" and claimed["progress"]["total"] == 2
    third = await kb_queue.enqueue_documents("kb1", ["d3"], "u1")
    assert third["_id"] != first["_id"]
    assert third["doc_ids"] == ["d3"]


async def test_enqueue_retries_once_on_duplicate_key(kb_env, monkeypatch) -> None:
    storage = get_kb_storage()
    real = storage.tasks.find_one_and_update
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DuplicateKeyError("race")
        return await real(*args, **kwargs)

    monkeypatch.setattr(storage.tasks, "find_one_and_update", flaky)
    task = await storage.enqueue_task("kb1", ["d1"], "u1")
    assert calls["n"] == 2 and task["doc_ids"] == ["d1"]

    async def always(*_a, **_k):
        raise DuplicateKeyError("race")

    monkeypatch.setattr(storage.tasks, "find_one_and_update", always)
    with pytest.raises(DuplicateKeyError):
        await storage.enqueue_task("kb1", ["d2"], "u1")


async def test_claim_is_compare_and_set(kb_env) -> None:
    storage = get_kb_storage()
    task = await storage.enqueue_task("kb1", ["d1"], "u1")
    assert await storage.claim_task(task["_id"], "w1", utc_now(), 60) is not None
    assert await storage.claim_task(task["_id"], "w2", utc_now(), 60) is None
    stored = await storage.get_task(task["_id"])
    assert stored["lease_owner"] == "w1" and stored["attempts"] == 1


async def test_process_task_runs_documents_serially_and_records_progress(kb_env, monkeypatch) -> None:
    await _base()
    for doc_id in ("d1", "d2", "d3"):
        await _doc("kb1", doc_id)
    task = await get_kb_storage().enqueue_task("kb1", ["d1", "d2", "d3"], "u1")

    order: list[str] = []
    running = 0

    async def fake_process(doc_id: str, task_id: str) -> str:
        nonlocal running
        running += 1
        assert running == 1, "documents of one task must be processed serially"
        order.append(doc_id)
        await asyncio.sleep(0)
        running -= 1
        return pipeline.OUTCOME_FAILED if doc_id == "d2" else pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    await kb_queue.process_kb_task(task["_id"])

    stored = await get_kb_storage().get_task(task["_id"])
    assert order == ["d1", "d2", "d3"]
    assert stored["status"] == "succeeded"
    assert stored["progress"] == {"total": 3, "done": 3, "failed": 1, "current_doc_id": None}
    assert stored["finished_at"] is not None


async def test_process_task_survives_a_crashing_document(kb_env, monkeypatch) -> None:
    await _base()
    task = await get_kb_storage().enqueue_task("kb1", ["d1", "d2"], "u1")

    async def fake_process(doc_id: str, task_id: str) -> str:
        if doc_id == "d1":
            raise RuntimeError("boom")
        return pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    await kb_queue.process_kb_task(task["_id"])
    stored = await get_kb_storage().get_task(task["_id"])
    assert stored["status"] == "succeeded"
    assert stored["progress"]["failed"] == 1 and stored["progress"]["done"] == 2


async def test_per_user_fairness_leaves_second_task_queued(kb_env, monkeypatch) -> None:
    storage = get_kb_storage()
    await _base("kb1", "u1")
    await _base("kb2", "u1")
    await _base("kb3", "u2")
    running = await storage.enqueue_task("kb1", ["d1"], "u1")
    await storage.claim_task(running["_id"], "w1", utc_now(), 60)
    blocked = await storage.enqueue_task("kb2", ["d2"], "u1")
    other_user = await storage.enqueue_task("kb3", ["d3"], "u2")

    processed: list[str] = []

    async def fake_process(doc_id: str, task_id: str) -> str:
        processed.append(doc_id)
        return pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    await kb_queue.process_kb_task(blocked["_id"])
    assert (await storage.get_task(blocked["_id"]))["status"] == "queued"
    assert processed == []

    await kb_queue.process_kb_task(other_user["_id"])
    assert (await storage.get_task(other_user["_id"]))["status"] == "succeeded"
    assert processed == ["d3"]


async def test_finished_task_republishes_waiting_tasks(kb_env, monkeypatch) -> None:
    storage = get_kb_storage()
    await _base("kb1", "u1")
    await _base("kb2", "u1")
    first = await storage.enqueue_task("kb1", ["d1"], "u1")
    waiting = await storage.enqueue_task("kb2", ["d2"], "u1")

    async def fake_process(doc_id: str, task_id: str) -> str:
        return pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    await kb_queue.process_kb_task(first["_id"])
    assert waiting["_id"] in kb_env.published


async def test_cancel_queued_task_fails_its_documents(kb_env) -> None:
    await _base()
    await _doc("kb1", "d1", "uploaded")
    await _doc("kb1", "d2", "parsed")
    task = await get_kb_storage().enqueue_task("kb1", ["d1", "d2"], "u1")

    cancelled = await kb_queue.cancel_task("kb1", task["_id"])
    assert cancelled["status"] == "cancelled"
    storage = get_kb_storage()
    d1 = await storage.get_document("kb1", "d1")
    d2 = await storage.get_document("kb1", "d2")
    assert d1["status"] == "error_parsing" and d1["error"]["message"] == kb_queue.CANCELLED_MESSAGE
    assert d2["status"] == "error_indexing"
    # a cancelled task is never claimed afterwards
    assert await storage.claim_task(task["_id"], "w", utc_now(), 60) is None
    assert await kb_queue.cancel_task("kb-other", task["_id"]) is None


async def test_cancel_running_task_stops_between_documents(kb_env, monkeypatch) -> None:
    await _base()
    for doc_id in ("d1", "d2", "d3"):
        await _doc("kb1", doc_id)
    task = await get_kb_storage().enqueue_task("kb1", ["d1", "d2", "d3"], "u1")
    seen: list[str] = []

    async def fake_process(doc_id: str, task_id: str) -> str:
        seen.append(doc_id)
        if doc_id == "d1":
            await kb_queue.cancel_task("kb1", task_id)
        return pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    await kb_queue.process_kb_task(task["_id"])

    storage = get_kb_storage()
    assert seen == ["d1"]
    assert (await storage.get_task(task["_id"]))["status"] == "cancelled"
    assert (await storage.get_document("kb1", "d2"))["status"] == "error_parsing"
    assert (await storage.get_document("kb1", "d3"))["error"]["message"] == kb_queue.CANCELLED_MESSAGE


async def test_heartbeat_renews_lease_and_flags_cancel(kb_env, monkeypatch) -> None:
    monkeypatch.setattr(kb_queue, "HEARTBEAT_SECONDS", 0.01)
    storage = get_kb_storage()
    task = await storage.enqueue_task("kb1", ["d1"], "u1")
    claimed = await storage.claim_task(task["_id"], "w1", utc_now() - timedelta(seconds=30), 60)
    before = claimed["lease_until"]
    stop, cancel, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    beat = asyncio.create_task(kb_queue._heartbeat(task["_id"], "w1", stop, cancel, lost))
    await kb_fakes.wait_until(lambda: storage.tasks.docs[0]["lease_until"] > before)
    await storage.request_cancel_running(task["_id"])
    await kb_fakes.wait_until(cancel.is_set)
    stop.set()
    await beat
    assert not lost.is_set()


async def test_heartbeat_detects_lost_lease(kb_env, monkeypatch) -> None:
    monkeypatch.setattr(kb_queue, "HEARTBEAT_SECONDS", 0.01)
    storage = get_kb_storage()
    task = await storage.enqueue_task("kb1", ["d1"], "u1")
    await storage.claim_task(task["_id"], "someone-else", utc_now(), 60)
    stop, cancel, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    await asyncio.wait_for(kb_queue._heartbeat(task["_id"], "w1", stop, cancel, lost), 2)
    assert lost.is_set() and cancel.is_set()


async def _expire(task_id: str, attempts: int | None = None) -> None:
    storage = get_kb_storage()
    doc = next(t for t in storage.tasks.docs if t["_id"] == task_id)
    doc["lease_until"] = utc_now() - timedelta(seconds=5)
    if attempts is not None:
        doc["attempts"] = attempts


async def test_reconcile_requeues_expired_lease_and_rolls_back_documents(kb_env) -> None:
    storage = get_kb_storage()
    await _base()
    await _doc("kb1", "d1", "parsing")
    await _doc("kb1", "d2", "indexing")
    await _doc("kb1", "d3", "indexed")
    task = await storage.enqueue_task("kb1", ["d1", "d2", "d3"], "u1")
    await storage.claim_task(task["_id"], "dead-worker", utc_now(), 60)
    await _expire(task["_id"])

    stats = await kb_queue.reconcile_tasks()
    assert stats["requeued"] == 1
    stored = await storage.get_task(task["_id"])
    assert stored["status"] == "queued" and stored["lease_owner"] is None
    assert (await storage.get_document("kb1", "d1"))["status"] == "uploaded"
    assert (await storage.get_document("kb1", "d2"))["status"] == "parsed"
    assert (await storage.get_document("kb1", "d3"))["status"] == "indexed"
    assert task["_id"] in kb_env.published


async def test_reconcile_fails_task_after_max_attempts(kb_env) -> None:
    storage = get_kb_storage()
    await _base()
    await _doc("kb1", "d1", "parsing")
    await _doc("kb1", "d2", "indexing")
    await _doc("kb1", "d3", "uploaded")
    task = await storage.enqueue_task("kb1", ["d1", "d2", "d3"], "u1")
    await storage.claim_task(task["_id"], "dead-worker", utc_now(), 60)
    await _expire(task["_id"], attempts=kb_queue.MAX_ATTEMPTS)

    stats = await kb_queue.reconcile_tasks()
    assert stats["failed"] == 1
    stored = await storage.get_task(task["_id"])
    assert stored["status"] == "failed" and stored["error"] == kb_queue.LEASE_EXHAUSTED_MESSAGE
    assert (await storage.get_document("kb1", "d1"))["status"] == "error_parsing"
    assert (await storage.get_document("kb1", "d2"))["status"] == "error_indexing"
    assert (await storage.get_document("kb1", "d3"))["status"] == "error_parsing"


async def test_reconcile_ignores_live_leases_and_cancels_flagged_tasks(kb_env) -> None:
    storage = get_kb_storage()
    await _base()
    await _doc("kb1", "d1", "parsing")
    live = await storage.enqueue_task("kb1", ["d1"], "u1")
    await storage.claim_task(live["_id"], "w1", utc_now(), 60)
    assert (await kb_queue.reconcile_tasks())["requeued"] == 0
    assert (await storage.get_task(live["_id"]))["status"] == "running"

    await storage.request_cancel_running(live["_id"])
    await _expire(live["_id"])
    assert (await kb_queue.reconcile_tasks())["cancelled"] == 1
    assert (await storage.get_task(live["_id"]))["status"] == "cancelled"
    assert (await storage.get_document("kb1", "d1"))["status"] == "error_parsing"


async def test_reconcile_merges_requeued_task_into_newer_queued_task(kb_env) -> None:
    storage = get_kb_storage()
    await storage.ensure_indexes()
    await _base()
    await _doc("kb1", "d1", "parsing")
    old = await storage.enqueue_task("kb1", ["d1"], "u1")
    await storage.claim_task(old["_id"], "dead-worker", utc_now(), 60)
    newer = await storage.enqueue_task("kb1", ["d2"], "u1")
    await _expire(old["_id"])

    await kb_queue.reconcile_tasks()
    merged = await storage.get_task(newer["_id"])
    assert sorted(merged["doc_ids"]) == ["d1", "d2"]
    assert (await storage.get_task(old["_id"]))["status"] == "cancelled"


async def test_reconcile_republishes_stale_queued_tasks_only(kb_env) -> None:
    storage = get_kb_storage()
    await _base("kb1")
    await _base("kb2")
    stale = await storage.enqueue_task("kb1", ["d1"], "u1")
    fresh = await storage.enqueue_task("kb2", ["d2"], "u1")
    await storage.mark_published(fresh["_id"])
    next(t for t in storage.tasks.docs if t["_id"] == stale["_id"])["last_published_at"] = (
        utc_now() - timedelta(seconds=120)
    )
    await kb_queue.reconcile_tasks()
    assert kb_env.published == [stale["_id"]]


async def test_orphaned_documents_are_enqueued_after_two_minutes(kb_env) -> None:
    storage = get_kb_storage()
    await _base()
    old = utc_now() - timedelta(seconds=300)
    await _doc("kb1", "orphan", "uploaded", updated_at=old)
    await _doc("kb1", "orphan2", "parsed", updated_at=old)
    await _doc("kb1", "recent", "uploaded")
    await _doc("kb1", "covered", "uploaded", updated_at=old)
    await _doc("kb1", "done", "indexed", updated_at=old)
    covering = await storage.enqueue_task("kb1", ["covered"], "u1")

    queued = await kb_runtime._enqueue_orphans()
    assert queued == 2
    tasks = [t for t in storage.tasks.docs if t["status"] == "queued"]
    assert len(tasks) == 1
    assert sorted(tasks[0]["doc_ids"]) == ["covered", "orphan", "orphan2"]
    assert tasks[0]["_id"] == covering["_id"]
    assert tasks[0]["created_by"] == "u1"


async def test_orphans_without_a_base_row_are_removed(kb_env) -> None:
    storage = get_kb_storage()
    old = utc_now() - timedelta(seconds=300)
    await _doc("ghost", "d1", "uploaded", updated_at=old, object_key="kb/ghost/d1/original.md")
    kb_env.objects.objects["kb/ghost/d1/original.md"] = b"x"
    await kb_runtime._enqueue_orphans()
    assert await storage.get_document_by_id("d1") is None
    assert kb_env.objects.objects == {}


async def test_terminal_tasks_are_pruned_to_twenty_per_kb(kb_env) -> None:
    storage = get_kb_storage()
    base = utc_now()
    for i in range(25):
        await storage.tasks.insert_one(
            {
                "_id": f"t{i}",
                "kb_id": "kb1",
                "status": "succeeded",
                "created_at": base + timedelta(seconds=i),
            }
        )
    await storage.tasks.insert_one(
        {"_id": "active", "kb_id": "kb1", "status": "queued", "created_at": base}
    )
    assert await storage.kb_ids_over_task_limit(kb_queue.TERMINAL_TASKS_KEEP) == ["kb1"]
    stats = await kb_runtime.reconcile_once()
    assert stats["pruned"] == 5
    remaining = {t["_id"] for t in storage.tasks.docs}
    assert "active" in remaining and "t0" not in remaining and "t24" in remaining
    assert len(remaining) == 21


async def _wait_status(task_id: str, status: str) -> None:
    storage = get_kb_storage()
    await kb_fakes.wait_until(
        lambda: next(t for t in storage.tasks.docs if t["_id"] == task_id)["status"] == status
    )


async def test_local_runner_processes_published_tasks(kb_env, monkeypatch) -> None:
    kb_queue.set_task_publisher(None)
    await _base()
    task = await get_kb_storage().enqueue_task("kb1", ["d1"], "u1")
    done = asyncio.Event()

    async def fake_process(doc_id: str, task_id: str) -> str:
        done.set()
        return pipeline.OUTCOME_INDEXED

    monkeypatch.setattr(pipeline, "process_document", fake_process)
    assert kb_queue.use_arq_publisher() is False
    await kb_queue.publish_task(task["_id"])
    await asyncio.wait_for(done.wait(), 2)
    await _wait_status(task["_id"], "succeeded")
    await kb_queue.close_publisher()


async def test_publish_failure_is_swallowed(kb_env) -> None:
    async def boom(task_id: str) -> None:
        raise RuntimeError("redis down")

    kb_queue.set_task_publisher(boom)
    await kb_queue.publish_task("whatever")  # must not raise
