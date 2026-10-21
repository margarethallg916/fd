"""Object-storage keys and IO for knowledge base files (never publicly readable)."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any

from src.infra.async_utils import run_blocking_io
from src.infra.knowledge_base.errors import KBValidationError
from src.infra.logging import get_logger
from src.infra.storage.s3.service import get_or_init_storage

logger = get_logger(__name__)

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024
UPLOAD_SPOOL_MEMORY_LIMIT = 2 * 1024 * 1024
MARKDOWN_CONTENT_TYPE = "text/markdown; charset=utf-8"


def original_key(kb_id: str, doc_id: str, ext: str) -> str:
    return f"kb/{kb_id}/{doc_id}/original{ext}"


def markdown_key(kb_id: str, doc_id: str) -> str:
    return f"kb/{kb_id}/{doc_id}/parsed.md"


@dataclass
class SpooledUpload:
    file: Any
    sha256_hex: str
    size: int

    def close(self) -> None:
        self.file.close()


async def spool_upload(file: Any, *, max_size_bytes: int, max_size_mb: int) -> SpooledUpload:
    """Stream an UploadFile into a bounded spool while hashing and counting bytes."""
    digest = hashlib.sha256()
    total = 0
    spooled = SpooledTemporaryFile(max_size=UPLOAD_SPOOL_MEMORY_LIMIT, mode="w+b")
    try:
        while True:
            chunk = await file.read(UPLOAD_READ_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_size_bytes:
                raise KBValidationError(f"文件大小超过 {max_size_mb}MB 限制")
            digest.update(chunk)
            await run_blocking_io(spooled.write, chunk)
        await run_blocking_io(spooled.seek, 0)
        return SpooledUpload(file=spooled, sha256_hex=digest.hexdigest(), size=total)
    except Exception:
        spooled.close()
        raise


async def put_original(key: str, spooled: SpooledUpload, content_type: str | None) -> None:
    storage = await get_or_init_storage()
    await storage.upload_stream_to_key(
        spooled.file,
        key,
        content_type or "application/octet-stream",
        skip_size_limit=True,
    )


async def put_markdown(key: str, markdown: str) -> None:
    storage = await get_or_init_storage()
    await storage.upload_to_key(
        markdown.encode("utf-8"), key, MARKDOWN_CONTENT_TYPE, skip_size_limit=True
    )


async def read_object(key: str) -> bytes:
    """Read a whole object; stream-collected so the internal download limit does not apply."""
    storage = await get_or_init_storage()
    buffer = bytearray()
    async for chunk in storage.download_stream(key):
        buffer.extend(chunk)
    return bytes(buffer)


async def read_markdown(key: str) -> str:
    return (await read_object(key)).decode("utf-8", errors="replace")


async def object_exists(key: str) -> bool:
    storage = await get_or_init_storage()
    return bool(await storage.file_exists(key))


async def stream_object(key: str) -> AsyncIterator[bytes]:
    storage = await get_or_init_storage()
    async for chunk in storage.download_stream(key):
        yield chunk


async def delete_objects(keys: list[str | None]) -> None:
    """Best-effort delete; storage failures are logged and never block the caller."""
    wanted = [k for k in keys if k]
    if not wanted:
        return
    try:
        storage = await get_or_init_storage()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] object storage unavailable for delete: %s", exc)
        return
    for key in wanted:
        try:
            await storage.delete_file(key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[KB] delete object %s failed: %s", key, exc)


async def delete_prefix(prefix: str) -> None:
    """Delete leftovers under a prefix (covers objects whose rows are already gone)."""
    try:
        storage = await get_or_init_storage()
        keys = await storage.list_files(prefix)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[KB] list %s failed: %s", prefix, exc)
        return
    await delete_objects(list(keys))
