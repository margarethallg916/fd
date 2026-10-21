"""Best-effort WebSocket pushes for knowledge base state changes."""

from __future__ import annotations

from typing import Any

from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.logging import get_logger
from src.infra.websocket import get_connection_manager

logger = get_logger(__name__)


async def push_event(kb_id: str, event_type: str, data: dict[str, Any]) -> None:
    """Fan an event out to every member of the knowledge base; never raises."""
    try:
        user_ids = await get_kb_storage().list_member_user_ids(kb_id)
        if not user_ids:
            return
        manager = get_connection_manager()
        message = {"type": event_type, "data": {"kb_id": kb_id, **data}}
        for user_id in user_ids:
            await manager.send_to_user_with_broadcast(user_id, message)
    except Exception as exc:  # noqa: BLE001 - pushes are an optimisation, polling is the fallback
        logger.warning("[KB] push %s for %s failed: %s", event_type, kb_id, exc)


async def push_document_updated(kb_id: str, doc_id: str, status: str) -> None:
    await push_event(kb_id, "kb:document_updated", {"doc_id": doc_id, "status": status})


async def push_task_updated(task: dict[str, Any]) -> None:
    await push_event(
        str(task.get("kb_id")),
        "kb:task_updated",
        {
            "task_id": task.get("_id"),
            "status": task.get("status"),
            "progress": task.get("progress") or {},
        },
    )
