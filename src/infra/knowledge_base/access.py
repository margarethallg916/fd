"""Knowledge base access resolution.

Membership rows (``kb_members``) are the only grant. PR1 only writes the owner row, but the
capability table already covers the roles later PRs add (editor/viewer/subscriber).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.infra.knowledge_base.errors import KBForbiddenError, KBNotFoundError
from src.infra.knowledge_base.storage import get_kb_storage
from src.kernel.schemas.knowledge_base import (
    KB_ROLE_EDITOR,
    KB_ROLE_OWNER,
    KB_ROLE_SUBSCRIBER,
    KB_ROLE_VIEWER,
    KB_STATUS_ACTIVE,
)

# role -> (can_read, can_write, can_manage)
ROLE_CAPABILITIES: dict[str, tuple[bool, bool, bool]] = {
    KB_ROLE_OWNER: (True, True, True),
    KB_ROLE_EDITOR: (True, True, False),
    KB_ROLE_VIEWER: (True, False, False),
    KB_ROLE_SUBSCRIBER: (True, False, False),
}


@dataclass(frozen=True)
class KBAccess:
    kb: dict[str, Any]
    role: str
    can_read: bool
    can_write: bool
    can_manage: bool

    @property
    def kb_id(self) -> str:
        return str(self.kb["_id"])


async def resolve_kb_access(user_id: str, kb_id: str) -> KBAccess | None:
    """Return the caller's access to an active KB, or ``None`` (indistinguishable from absent)."""
    storage = get_kb_storage()
    member = await storage.get_member(kb_id, user_id)
    if member is None:
        return None
    capabilities = ROLE_CAPABILITIES.get(str(member.get("role")))
    if capabilities is None:
        return None
    kb = await storage.get_base(kb_id)
    if kb is None or kb.get("status") != KB_STATUS_ACTIVE:
        return None
    can_read, can_write, can_manage = capabilities
    return KBAccess(
        kb=kb,
        role=str(member["role"]),
        can_read=can_read,
        can_write=can_write,
        can_manage=can_manage,
    )


async def require_kb_access(
    user_id: str, kb_id: str, *, write: bool = False, manage: bool = False
) -> KBAccess:
    """Resolve access or raise: no access -> not found; insufficient role -> forbidden."""
    access = await resolve_kb_access(user_id, kb_id)
    if access is None or not access.can_read:
        raise KBNotFoundError()
    if manage and not access.can_manage:
        raise KBForbiddenError()
    if write and not access.can_write:
        raise KBForbiddenError()
    return access
