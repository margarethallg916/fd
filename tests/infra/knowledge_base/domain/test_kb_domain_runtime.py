"""Runtime wiring: embedded arq worker lifecycle, reconcile loop and publisher selection."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.infra import runtime_services
from src.infra.knowledge_base import queue as kb_queue
from src.infra.knowledge_base import runtime as kb_runtime
from src.infra.knowledge_base import service
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from tests.infra.knowledge_base.domain import kb_fakes


class FakeWorker:
    instances: list[FakeWorker] = []

    def __init__(self, functions, **kwargs) -> None:
        self.functions = functions
        self.kwargs = kwargs
        self.closed = False
        self.started = asyncio.Event()
        FakeWorker.instances.append(self)

    async def async_run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_workers():
    FakeWorker.instances.clear()
    yield


async def test_arq_worker_uses_dedicated_queue_and_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "TASK_BACKEND", "arq")
    monkeypatch.setattr(settings, "ARQ_EMBEDDED_WORKER", True)
    monkeypatch.setattr(settings, "KB_WORKER_CONCURRENCY", 3)
    monkeypatch.setattr(kb_runtime, "build_arq_redis_settings", lambda s: "redis-settings")
    runtime = kb_runtime.KBRuntime(worker_factory=FakeWorker)

    await runtime.start()
    worker = FakeWorker.instances[0]
    assert runtime.is_running
    assert [fn.__name__ for fn in worker.functions] == ["kb_process_task"]
    assert worker.kwargs == {
        "queue_name": settings.KB_ARQ_QUEUE_NAME,
        "redis_settings": "redis-settings",
        "handle_signals": False,
        "max_jobs": 3,
        "job_timeout": settings.KB_JOB_TIMEOUT_SECONDS,
        "keep_result": 0,
        "max_tries": 1,
    }
    assert settings.KB_ARQ_QUEUE_NAME == "kunxiaozhi:kb" and settings.KB_JOB_TIMEOUT_SECONDS == 7200
    await runtime.start()  # idempotent
    assert len(FakeWorker.instances) == 1

    await runtime.stop()
    assert worker.closed and not runtime.is_running


@pytest.mark.parametrize(
    "backend,embedded", [("local", True), ("arq", False)]
)
async def test_no_worker_without_embedded_arq(monkeypatch, backend, embedded) -> None:
    monkeypatch.setattr(settings, "TASK_BACKEND", backend)
    monkeypatch.setattr(settings, "ARQ_EMBEDDED_WORKER", embedded)
    runtime = kb_runtime.KBRuntime(worker_factory=FakeWorker)
    await runtime.start()
    assert FakeWorker.instances == [] and not runtime.is_running
    assert kb_queue.use_arq_publisher() is False
    await runtime.stop()


async def test_stop_tolerates_windows_sigusr1_error() -> None:
    class Worker(FakeWorker):
        async def close(self) -> None:
            raise AttributeError("module 'signal' has no attribute 'SIGUSR1'")

    runtime = kb_runtime.KBRuntime(worker_factory=Worker)
    runtime._worker = Worker([], )
    await runtime.stop()


async def test_arq_publisher_enqueues_without_job_id(kb_env, monkeypatch) -> None:
    kb_queue.set_task_publisher(None)
    monkeypatch.setattr(settings, "TASK_BACKEND", "arq")
    monkeypatch.setattr(settings, "ARQ_EMBEDDED_WORKER", True)
    task = await get_kb_storage().enqueue_task("kb1", ["d1"], "u1")
    enqueued: list[tuple] = []

    class Pool:
        async def enqueue_job(self, *args, **kwargs):
            enqueued.append((args, kwargs))

        async def aclose(self) -> None:
            enqueued.append(("closed",))

    async def fake_pool():
        return Pool()

    monkeypatch.setattr(kb_queue, "_get_arq_pool", fake_pool)
    await kb_queue.publish_task(task["_id"])
    assert enqueued == [(("kb_process_task", task["_id"]), {})]
    stored = await get_kb_storage().get_task(task["_id"])
    assert stored["last_published_at"] is not None


async def test_arq_pool_is_created_on_the_kb_queue(monkeypatch) -> None:
    import arq

    captured = {}

    async def fake_create_pool(redis_settings, **kwargs):
        captured.update(redis_settings=redis_settings, **kwargs)
        return SimpleNamespace(aclose=lambda: asyncio.sleep(0))

    monkeypatch.setattr(arq, "create_pool", fake_create_pool)
    monkeypatch.setattr(kb_queue, "build_arq_redis_settings", lambda s: "rs")
    monkeypatch.setattr(kb_queue, "_arq_pool", None)
    pool = await kb_queue._get_arq_pool()
    assert captured == {"redis_settings": "rs", "default_queue_name": "kunxiaozhi:kb"}
    assert await kb_queue._get_arq_pool() is pool
    await kb_queue.close_publisher()
    assert kb_queue._arq_pool is None


async def test_reconcile_once_recovers_everything_in_one_pass(kb_env) -> None:
    storage = get_kb_storage()
    now = utc_now()
    await storage.insert_base(
        {"_id": "kb1", "owner_id": "u1", "name": "n", "status": "active", "created_at": now}
    )
    await storage.insert_document(
        {
            "_id": "orphan",
            "kb_id": "kb1",
            "sha256": "o",
            "status": "uploaded",
            "created_at": now,
            "updated_at": now - timedelta(minutes=10),
        }
    )
    await storage.insert_base(
        {"_id": "kb-del", "owner_id": "u1", "name": "gone", "status": "deleting", "created_at": now}
    )
    await storage.insert_member({"_id": "m", "kb_id": "kb-del", "user_id": "u1", "role": "owner"})

    stats = await kb_runtime.reconcile_once()
    assert stats["orphans"] == 1 and stats["cleaned"] == 1
    assert await storage.get_base("kb-del") is None and storage.members.docs == []
    assert [t["doc_ids"] for t in storage.tasks.docs] == [["orphan"]]


async def test_reconcile_loop_survives_errors_and_stops_on_cancel(monkeypatch) -> None:
    monkeypatch.setattr(kb_runtime, "RECONCILE_STARTUP_DELAY_SECONDS", 0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("mongo down")
        return {"requeued": 0}

    monkeypatch.setattr(kb_runtime, "reconcile_once", flaky)
    loop = asyncio.create_task(kb_runtime.run_reconcile_loop(interval=0.01))
    await kb_fakes.wait_until(lambda: calls["n"] >= 3)
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop


class _Service:
    def __init__(self) -> None:
        self.calls = 0

    async def start_listener(self) -> None:
        self.calls += 1

    async def stop_listener(self) -> None:
        self.calls += 1

    async def start_pubsub_listener(self) -> None:
        self.calls += 1

    async def stop_pubsub_listener(self) -> None:
        self.calls += 1


def _patch_common(monkeypatch, order: list[str]) -> None:
    async def noop() -> None:
        return None

    service_obj = _Service()
    for name in ("start_arq_runtime", "stop_arq_runtime"):
        async def mark(name=name) -> None:
            order.append(name)

        monkeypatch.setattr(runtime_services, name, mark)
    for name in ("start_event_loop_lag_monitor", "stop_event_loop_lag_monitor"):
        monkeypatch.setattr(runtime_services, name, noop)
    monkeypatch.setattr(runtime_services, "settings", SimpleNamespace(ENABLE_MEMORY=False), raising=False)
    monkeypatch.setattr(runtime_services, "get_task_manager", lambda: service_obj)
    for name in (
        "get_settings_pubsub",
        "get_model_config_pubsub",
        "get_tool_cache_pubsub",
        "get_mcp_cache_pubsub",
        "get_connection_manager",
    ):
        monkeypatch.setattr(runtime_services, name, lambda: service_obj, raising=False)
    monkeypatch.setattr(
        runtime_services, "get_runtime_scheduler", lambda: SimpleNamespace(start=lambda: None, stop=noop)
    )
    monkeypatch.setattr(runtime_services, "shutdown_blocking_io_executor", lambda: None)
    for name in (
        "drain_mcp_cache_background_tasks",
        "drain_mcp_global_background_tasks",
        "drain_recommend_background_tasks",
        "close_mcp_pool_connections",
        "drain_upload_delete_tasks",
        "drain_user_s3_cleanup_tasks",
        "drain_project_cleanup_tasks",
        "drain_llm_client_close_tasks",
        "drain_dual_writer_event_buffer",
    ):
        monkeypatch.setattr(runtime_services, name, noop, raising=False)


async def test_kb_worker_starts_next_to_arq_and_failures_never_block_startup(monkeypatch) -> None:
    order: list[str] = []
    _patch_common(monkeypatch, order)

    async def start_ok() -> None:
        order.append("start_kb")

    monkeypatch.setattr(runtime_services, "start_kb_runtime", start_ok)
    await runtime_services.start_runtime_services()
    assert order == ["start_arq_runtime", "start_kb"]

    async def boom() -> None:
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(runtime_services, "start_kb_runtime", boom)
    await runtime_services.start_runtime_services()  # must not raise


async def test_kb_worker_stops_before_arq_and_failures_never_block_shutdown(monkeypatch) -> None:
    order: list[str] = []
    _patch_common(monkeypatch, order)

    async def stop_ok() -> None:
        order.append("stop_kb")

    monkeypatch.setattr(runtime_services, "stop_kb_runtime", stop_ok)
    await runtime_services.stop_runtime_services()
    assert order == ["stop_kb", "stop_arq_runtime"]

    async def boom() -> None:
        raise RuntimeError("close failed")

    order.clear()
    monkeypatch.setattr(runtime_services, "stop_kb_runtime", boom)
    await runtime_services.stop_runtime_services()
    assert order == ["stop_arq_runtime"]


async def test_drain_cleanup_tasks_cancels_pending_cleanups(kb_env) -> None:
    started = asyncio.Event()

    async def slow() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(slow())
    service._cleanup_tasks.add(task)
    await started.wait()
    await service.drain_cleanup_tasks()
    assert task.cancelled()
    service._cleanup_tasks.discard(task)
