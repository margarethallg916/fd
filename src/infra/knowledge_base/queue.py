"""Knowledge base task queue: Mongo ``kb_tasks`` + arq (or local) wake-up messages.

The task row is the source of truth; publishing only wakes a worker, so duplicate or lost
messages are harmless (claims are CAS, the reconciler republishes).
"""

from __future__ import annotations

import asyncio
import inspect
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from src.infra.knowledge_base import pipeline
from src.infra.knowledge_base.events import push_task_updated
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.logging import get_logger
from src.infra.task.arq_settings import build_arq_redis_settings
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.knowledge_base import (
    TASK_CANCELLED,
    TASK_FAILED,
    TASK_QUEUED,
    TASK_RUNNING,
    TASK_SUCCEEDED,
)

logger = get_logger(__name__)

LEASE_SECONDS = 60.0
HEARTBEAT_SECONDS = 15.0
MAX_ATTEMPTS = 3
STALE_PUBLISH_SECONDS = 30.0
TERMINAL_TASKS_KEEP = 20
REPUBLISH_LIMIT = 10
CANCELLED_MESSAGE = "处理已取消"
LEASE_EXHAUSTED_MESSAGE = "处理超时或服务中断，请重试"
ARQ_FUNCTION_NAME = "kb_process_task"

Publisher = Callable[[str], Awaitable[None]]
_publisher_override: Publisher | None = None
_arq_pool: Any = None
_arq_pool_lock: asyncio.Lock | None = None


def set_task_publisher(publisher: Publisher | None) -> None:
    """Swap the wake-up mechanism (tests); ``None`` restores arq/local selection."""
    global _publisher_override
    _publisher_override = publisher


def use_arq_publisher() -> bool:
    return getattr(settings, "TASK_BACKEND", "local") == "arq" and bool(
        getattr(settings, "ARQ_EMBEDDED_WORKER", True)
    )


async def _get_arq_pool() -> Any:
    global _arq_pool, _arq_pool_lock
    if _arq_pool is not None:
        return _arq_pool
    if _arq_pool_lock is None:
        _arq_pool_lock = asyncio.Lock()
    async with _arq_pool_lock:
        if _arq_pool is None:
            from arq import create_pool

            _arq_pool = await create_pool(
                build_arq_redis_settings(settings),
                default_queue_name=settings.KB_ARQ_QUEUE_NAME,
            )
    return _arq_pool


async def _publish_arq(task_id: str) -> None:
    pool = await _get_arq_pool()
    await pool.enqueue_job(ARQ_FUNCTION_NAME, task_id)


class LocalTaskRunner:
    """In-process fallback: one asyncio task per message, gated by a semaphore."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()
        self._inflight: set[str] = set()
        self._semaphore: asyncio.Semaphore | None = None

    def submit(self, task_id: str) -> None:
        if task_id in self._inflight:
            return
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(max(1, int(settings.KB_WORKER_CONCURRENCY)))
        self._inflight.add(task_id)
        task = asyncio.create_task(self._run(task_id), name=f"kb-task:{task_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, task_id: str) -> None:
        try:
            assert self._semaphore is not None
            async with self._semaphore:
                await process_kb_task(task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[KB] local runner failed for task %s", task_id)
        finally:
            self._inflight.discard(task_id)

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._semaphore = None


_local_runner: LocalTaskRunner | None = None


def get_local_runner() -> LocalTaskRunner:
    global _local_runner
    if _local_runner is None:
        _local_runner = LocalTaskRunner()
    return _local_runner


async def _publish_default(task_id: str) -> None:
    if use_arq_publisher():
        await _publish_arq(task_id)
    else:
        get_local_runner().submit(task_id)


async def publish_task(task_id: str) -> None:
    """Wake a worker for ``task_id``; failures are logged and left to the reconciler."""
    publisher = _publisher_override or _publish_default
    try:
        await publisher(task_id)
        await get_kb_storage().mark_published(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] publish task %s failed: %s", task_id, exc)


async def close_publisher() -> None:
    global _arq_pool, _arq_pool_lock
    pool, _arq_pool, _arq_pool_lock = _arq_pool, None, None
    if pool is not None:
        try:
            closer = getattr(pool, "aclose", None) or pool.close
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KB] closing arq pool failed: %s", exc)
    if _local_runner is not None:
        await _local_runner.shutdown()


async def enqueue_documents(kb_id: str, doc_ids: list[str], created_by: str) -> dict[str, Any]:
    """Add documents to the KB's queued ingest task (creating it) and wake a worker."""
    task = await get_kb_storage().enqueue_task(kb_id, doc_ids, created_by)
    await publish_task(task["_id"])
    await push_task_updated(task)
    return task


def _worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


async def _heartbeat(
    task_id: str,
    owner: str,
    stop: asyncio.Event,
    cancel: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    storage = get_kb_storage()
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
            return
        except asyncio.TimeoutError:
            pass
        try:
            task = await storage.renew_lease(task_id, owner, utc_now(), LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 - transient; the next beat retries
            logger.warning("[KB] heartbeat for task %s failed: %s", task_id, exc)
            continue
        if task is None:
            lost.set()
            cancel.set()
            return
        if task.get("cancel_requested"):
            cancel.set()


async def process_kb_task(task_id: str) -> None:
    """Claim and run one queued ingest task (idempotent: losing the claim is a no-op)."""
    storage = get_kb_storage()
    task = await storage.get_task(task_id)
    if task is None or task.get("status") != TASK_QUEUED:
        return
    if task.get("cancel_requested"):
        await cancel_task(task["kb_id"], task_id)
        return

    now = utc_now()
    if await storage.has_running_for_user(task["created_by"], now, task_id):
        # One running task per user keeps big uploads from starving everyone else; the
        # finishing task (or the reconciler) republishes this one.
        return

    owner = _worker_id()
    claimed = await storage.claim_task(task_id, owner, now, LEASE_SECONDS)
    if claimed is None:
        return
    await push_task_updated(claimed)

    stop, cancel, lost = asyncio.Event(), asyncio.Event(), asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(task_id, owner, stop, cancel, lost))
    try:
        await _run_documents(claimed, owner, cancel, lost)
    finally:
        stop.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        await _after_task(claimed["kb_id"])


async def _cancel_requested(task_id: str, cancel: asyncio.Event) -> bool:
    if cancel.is_set():
        return True
    current = await get_kb_storage().get_task(task_id)
    if current is None or current.get("cancel_requested"):
        cancel.set()
    return cancel.is_set()


async def _run_documents(
    task: dict[str, Any], owner: str, cancel: asyncio.Event, lost: asyncio.Event
) -> None:
    storage = get_kb_storage()
    task_id, kb_id = task["_id"], task["kb_id"]
    doc_ids: list[str] = list(task.get("doc_ids") or [])
    done = failed = 0
    processed = 0

    for doc_id in doc_ids:
        if await _cancel_requested(task_id, cancel):
            break
        await storage.update_progress(
            task_id, owner, done=done, failed=failed, current_doc_id=doc_id
        )
        try:
            outcome = await pipeline.process_document(doc_id, task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[KB] document %s crashed in task %s", doc_id, task_id)
            outcome = pipeline.OUTCOME_FAILED
        processed += 1
        done += 1
        failed += 1 if outcome == pipeline.OUTCOME_FAILED else 0
        if not await storage.update_progress(
            task_id, owner, done=done, failed=failed, current_doc_id=None
        ):
            lost.set()
            cancel.set()
            break
        await push_task_updated(
            {
                "_id": task_id,
                "kb_id": kb_id,
                "status": TASK_RUNNING,
                "progress": {"total": len(doc_ids), "done": done, "failed": failed},
            }
        )

    if lost.is_set():
        logger.warning("[KB] task %s lost its lease; leaving it to the reconciler", task_id)
        return

    cancelled = cancel.is_set() and processed < len(doc_ids)
    if cancelled:
        await storage.settle_documents(doc_ids[processed:], CANCELLED_MESSAGE)
    finished = await storage.finish_task(
        task_id, owner, TASK_CANCELLED if cancelled else TASK_SUCCEEDED
    )
    if finished is not None:
        await push_task_updated(finished)


async def _after_task(kb_id: str) -> None:
    """Hand the freed slot to waiting tasks and trim history; never raises."""
    storage = get_kb_storage()
    try:
        await storage.prune_terminal_tasks(kb_id, TERMINAL_TASKS_KEEP)
        for queued in await storage.oldest_queued(REPUBLISH_LIMIT):
            await publish_task(queued["_id"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] post-task housekeeping failed: %s", exc)


async def kb_process_task(ctx: dict[str, Any], task_id: str) -> None:
    """arq entry point (the function name is the registered job name)."""
    await process_kb_task(task_id)


async def cancel_task(kb_id: str, task_id: str) -> dict[str, Any] | None:
    """Cancel a task: queued -> cancelled now; running -> cooperative flag. ``None`` if absent."""
    storage = get_kb_storage()
    task = await storage.get_kb_task(kb_id, task_id)
    if task is None:
        return None
    if task["status"] == TASK_QUEUED:
        cancelled = await storage.cancel_queued_task(task_id)
        if cancelled is not None:
            await storage.settle_documents(list(cancelled.get("doc_ids") or []), CANCELLED_MESSAGE)
            await push_task_updated(cancelled)
            return cancelled
        task = await storage.get_kb_task(kb_id, task_id)
        if task is None:
            return None
    if task["status"] == TASK_RUNNING:
        flagged = await storage.request_cancel_running(task_id)
        return flagged or await storage.get_kb_task(kb_id, task_id)
    return task


async def cancel_kb_tasks(kb_id: str) -> None:
    """Cancel every active task of a KB (used when it is being deleted)."""
    for task in await get_kb_storage().list_active_tasks_by_kb(kb_id):
        await cancel_task(kb_id, task["_id"])


async def reconcile_tasks() -> dict[str, int]:
    """Recover expired leases and republish stale queued tasks."""
    storage = get_kb_storage()
    now = utc_now()
    stats = {"requeued": 0, "failed": 0, "cancelled": 0, "republished": 0}

    for task in await storage.list_expired_running(now):
        doc_ids = list(task.get("doc_ids") or [])
        task_id, kb_id = task["_id"], task["kb_id"]
        if task.get("cancel_requested"):
            if await storage.close_expired(task, TASK_CANCELLED, CANCELLED_MESSAGE, now):
                await storage.settle_documents(doc_ids, CANCELLED_MESSAGE)
                stats["cancelled"] += 1
            continue
        if task.get("attempts", 0) >= MAX_ATTEMPTS:
            if await storage.close_expired(task, TASK_FAILED, LEASE_EXHAUSTED_MESSAGE, now):
                await storage.settle_documents(doc_ids, LEASE_EXHAUSTED_MESSAGE)
                stats["failed"] += 1
                logger.warning("[KB] task %s failed after %s attempts", task_id, MAX_ATTEMPTS)
            continue
        # Roll documents back first: if we crash before requeueing, the next pass repeats this.
        await storage.rollback_documents(doc_ids)
        try:
            requeued = await storage.requeue_expired(task, now)
        except DuplicateKeyError:
            # A newer queued task exists for this KB; fold the leftovers into it.
            await storage.merge_into_queued(kb_id, doc_ids)
            await storage.close_expired(task, TASK_CANCELLED, "已合并到后续任务", now)
            stats["requeued"] += 1
            continue
        if requeued:
            stats["requeued"] += 1
            await publish_task(task_id)

    stale_before = now - timedelta(seconds=STALE_PUBLISH_SECONDS)
    for task in await storage.list_stale_queued(stale_before):
        await publish_task(task["_id"])
        stats["republished"] += 1
    return stats


__all__ = [
    "LocalTaskRunner",
    "cancel_kb_tasks",
    "cancel_task",
    "close_publisher",
    "enqueue_documents",
    "kb_process_task",
    "process_kb_task",
    "publish_task",
    "reconcile_tasks",
    "set_task_publisher",
]
