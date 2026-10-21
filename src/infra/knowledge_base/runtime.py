"""Knowledge base runtime: embedded arq worker (queue ``KB_ARQ_QUEUE_NAME``) and reconciler."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import timedelta
from typing import Any, Callable

from arq.worker import Worker

from src.infra.knowledge_base import objects as kb_objects
from src.infra.knowledge_base import queue as kb_queue
from src.infra.knowledge_base import service as kb_service
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.logging import get_logger
from src.infra.task.arq_settings import build_arq_redis_settings
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.knowledge_base import KB_STATUS_ACTIVE

logger = get_logger(__name__)

RECONCILE_INTERVAL_SECONDS = 30.0
RECONCILE_STARTUP_DELAY_SECONDS = 5.0
ORPHAN_AGE_SECONDS = 120.0


class KBRuntime:
    """Own the embedded arq worker for the KB queue (the local runner needs no lifecycle)."""

    def __init__(self, worker_factory: Callable[..., Any] = Worker) -> None:
        self._worker_factory = worker_factory
        self._worker: Any | None = None
        self._task: asyncio.Task[Any] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.is_running or not kb_queue.use_arq_publisher():
            return
        self._worker = self._worker_factory(
            [kb_queue.kb_process_task],
            queue_name=settings.KB_ARQ_QUEUE_NAME,
            redis_settings=build_arq_redis_settings(settings),
            handle_signals=False,
            max_jobs=settings.KB_WORKER_CONCURRENCY,
            job_timeout=settings.KB_JOB_TIMEOUT_SECONDS,
            keep_result=0,
            max_tries=1,
        )
        self._task = asyncio.create_task(self._worker.async_run(), name="kb:arq-worker")
        logger.info("[KB] embedded arq worker started (queue=%s)", settings.KB_ARQ_QUEUE_NAME)

    async def stop(self) -> None:
        if self._worker is not None:
            close = getattr(self._worker, "close", None)
            if close is not None:
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except AttributeError as exc:
                    # arq's close() references signal.SIGUSR1, which Windows lacks.
                    if "SIGUSR1" not in str(exc):
                        raise
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._worker = None
        self._task = None
        await kb_queue.close_publisher()
        await kb_service.drain_cleanup_tasks()


_runtime: KBRuntime | None = None


def get_kb_runtime() -> KBRuntime:
    global _runtime
    if _runtime is None:
        _runtime = KBRuntime()
    return _runtime


async def start_kb_runtime() -> None:
    await get_kb_runtime().start()


async def stop_kb_runtime() -> None:
    await get_kb_runtime().stop()


async def _enqueue_orphans() -> int:
    """Queue ``uploaded``/``parsed`` documents no task knows about (crash between steps)."""
    storage = get_kb_storage()
    cutoff = utc_now() - timedelta(seconds=ORPHAN_AGE_SECONDS)
    by_kb: dict[str, list[str]] = defaultdict(list)
    for doc in await storage.list_unqueued_stale_documents(cutoff):
        by_kb[str(doc["kb_id"])].append(str(doc["_id"]))

    queued = 0
    for kb_id, doc_ids in by_kb.items():
        kb = await storage.get_base(kb_id)
        if kb is None:
            # A base always exists before its documents; rows without one are leftovers.
            removed = await storage.delete_documents(kb_id, doc_ids)
            await storage.delete_chunks_by_docs(doc_ids)
            await kb_objects.delete_objects(
                [key for d in removed for key in (d.get("object_key"), d.get("markdown_key"))]
            )
            continue
        if kb.get("status") != KB_STATUS_ACTIVE:
            continue
        await kb_queue.enqueue_documents(kb_id, doc_ids, str(kb["owner_id"]))
        queued += len(doc_ids)
    return queued


async def reconcile_once() -> dict[str, int]:
    """One recovery pass; every step is idempotent so replicas may overlap."""
    storage = get_kb_storage()
    stats = await kb_queue.reconcile_tasks()
    stats["orphans"] = await _enqueue_orphans()

    cleaned = 0
    for kb in await storage.list_deleting_bases():
        if await kb_service.cleanup_kb(str(kb["_id"])):
            cleaned += 1
    stats["cleaned"] = cleaned

    pruned = 0
    for kb_id in await storage.kb_ids_over_task_limit(kb_queue.TERMINAL_TASKS_KEEP):
        pruned += await storage.prune_terminal_tasks(kb_id, kb_queue.TERMINAL_TASKS_KEEP)
    stats["pruned"] = pruned
    return stats


async def run_reconcile_loop(interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
    await asyncio.sleep(RECONCILE_STARTUP_DELAY_SECONDS)
    while True:
        try:
            stats = await reconcile_once()
            if any(stats.values()):
                logger.info("[KB] reconcile pass: %s", stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the loop alive across Mongo/Redis blips
            logger.warning("[KB] reconcile pass failed: %s", exc)
        await asyncio.sleep(interval)
