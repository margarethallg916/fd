"""Domain errors for the personal knowledge base; routes map them to HTTP codes."""

from __future__ import annotations


class KBError(Exception):
    default_message = "知识库操作失败"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.default_message)


class KBNotFoundError(KBError):
    """Missing or inaccessible knowledge base (deliberately indistinguishable)."""

    default_message = "知识库不存在"


class KBDocumentNotFoundError(KBError):
    default_message = "文档不存在"


class KBTaskNotFoundError(KBError):
    default_message = "任务不存在"


class KBDisabledError(KBError):
    default_message = "知识库功能未开启"


class KBValidationError(KBError, ValueError):
    default_message = "参数不合法"


class KBForbiddenError(KBError):
    default_message = "无权执行此操作"


class KBBusyError(KBError):
    default_message = "知识库正在处理文档，请稍后再试"
