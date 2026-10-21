"""Settings service integration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from src.infra.logging import get_logger

from .base import settings

if TYPE_CHECKING:
    from src.infra.settings.service import SettingsService

logger = get_logger(__name__)

# SettingsService integration
_settings_service: Optional["SettingsService"] = None

# Cache for all settings from database
_settings_cache: dict[str, Any] = {}

_ALLOW_EMPTY_STRING_SETTINGS = {
    "DEFAULT_MODEL_ID",
    "NATIVE_MEMORY_COMPACTION_MODEL_ID",
    "SESSION_TITLE_MODEL_ID",
    "NATIVE_MEMORY_MODEL_ID",
    "AUDIO_TRANSCRIPTION_MODEL_ID",
    "NATIVE_MEMORY_EMBEDDING_MODEL_ID",
    "NATIVE_MEMORY_RERANK_MODEL_ID",
    "DIFY_KB_RERANK_MODEL_ID",
    "KB_EMBEDDING_MODEL_ID",
    "KB_RERANK_MODEL_ID",
}

_CHECKPOINT_AFFECTED_SETTINGS = {
    "CHECKPOINT_BACKEND",
    "CHECKPOINT_PG_HOST",
    "CHECKPOINT_PG_PORT",
    "CHECKPOINT_PG_USER",
    "CHECKPOINT_PG_PASSWORD",
    "CHECKPOINT_PG_DB",
    "CHECKPOINT_PG_POOL_MIN_SIZE",
    "CHECKPOINT_PG_POOL_MAX_SIZE",
}

# Sandbox settings that require the SessionSandboxManager singleton to be rebuilt
# so the new platform/adapter/params take effect without a restart. Mirrors the
# checkpoint hot-reload pattern. Soft reset: bindings & running sandboxes untouched.
_SANDBOX_AFFECTED_SETTINGS = {
    "ENABLE_SANDBOX",
    "SANDBOX_PLATFORM",
    "DAYTONA_API_KEY",
    "DAYTONA_SERVER_URL",
    "DAYTONA_TIMEOUT",
    "DAYTONA_IMAGE",
    "DAYTONA_AUTO_STOP_INTERVAL",
    "DAYTONA_AUTO_ARCHIVE_INTERVAL",
    "DAYTONA_AUTO_DELETE_INTERVAL",
    "E2B_API_KEY",
    "E2B_TEMPLATE",
    "E2B_TIMEOUT",
    "E2B_AUTO_PAUSE",
    "E2B_AUTO_RESUME",
    "OPENSANDBOX_DOMAIN",
    "OPENSANDBOX_API_KEY",
    "OPENSANDBOX_IMAGE",
    "OPENSANDBOX_TIMEOUT",
    "OPENSANDBOX_WORK_DIR",
    "OPENSANDBOX_USE_SERVER_PROXY",
    "OPENSANDBOX_NODES",
}


async def _reset_checkpoint_runtime_state(reason: str) -> None:
    try:
        from src.infra.storage.checkpoint import reset_checkpointer_runtime_state

        await reset_checkpointer_runtime_state()
        logger.info("[Settings] Checkpointer runtime state reset after %s", reason)
    except Exception as exc:
        logger.warning(
            "[Settings] Failed to reset checkpointer runtime state after %s: %s",
            reason,
            exc,
        )


async def _reset_sandbox_runtime_state(reason: str) -> None:
    """Rebuild the SessionSandboxManager singleton so sandbox config changes take
    effect without a restart. Soft reset — does not stop running sandboxes or
    clear persisted bindings.
    """
    try:
        from src.infra.sandbox.session_manager import reset_session_sandbox_manager

        reset_session_sandbox_manager()
        logger.info("[Settings] Sandbox manager rebuilt after %s", reason)
    except Exception as exc:
        logger.warning(
            "[Settings] Failed to rebuild sandbox manager after %s: %s",
            reason,
            exc,
        )


async def _migrate_session_title_model_to_id() -> None:
    """One-time migration: backfill SESSION_TITLE_MODEL_ID from the legacy
    SESSION_TITLE_MODEL triplet.

    If the legacy `SESSION_TITLE_MODEL` was explicitly set in the database
    (non-default value) and `SESSION_TITLE_MODEL_ID` is empty, look up a model
    card by value (trying both the raw value and a provider-prefixed form) and
    backfill the new ID. Idempotent and best-effort: never raises.
    """
    await _migrate_legacy_model_triplet_to_id(
        legacy_key="SESSION_TITLE_MODEL",
        new_id_key="SESSION_TITLE_MODEL_ID",
        expected_kind="chat",
        provider_prefixes=("anthropic", "openai"),
    )


async def _migrate_native_memory_model_to_id() -> None:
    """One-time migration: backfill NATIVE_MEMORY_MODEL_ID from the legacy
    NATIVE_MEMORY_MODEL triplet."""
    await _migrate_legacy_model_triplet_to_id(
        legacy_key="NATIVE_MEMORY_MODEL",
        new_id_key="NATIVE_MEMORY_MODEL_ID",
        expected_kind="chat",
        provider_prefixes=("anthropic", "openai"),
    )


async def _migrate_audio_transcription_model_to_id() -> None:
    """One-time migration: backfill AUDIO_TRANSCRIPTION_MODEL_ID from the
    legacy AUDIO_TRANSCRIPTION_MODEL bare string."""
    await _migrate_legacy_model_triplet_to_id(
        legacy_key="AUDIO_TRANSCRIPTION_MODEL",
        new_id_key="AUDIO_TRANSCRIPTION_MODEL_ID",
        expected_kind="transcribe",
        provider_prefixes=("openai",),
    )


async def _migrate_native_memory_embedding_model_to_id() -> None:
    """One-time migration: backfill NATIVE_MEMORY_EMBEDDING_MODEL_ID from the
    legacy NATIVE_MEMORY_EMBEDDING_MODEL bare string."""
    await _migrate_legacy_model_triplet_to_id(
        legacy_key="NATIVE_MEMORY_EMBEDDING_MODEL",
        new_id_key="NATIVE_MEMORY_EMBEDDING_MODEL_ID",
        expected_kind="embedding",
        provider_prefixes=("openai",),
    )


async def _migrate_native_memory_rerank_model_to_id() -> None:
    """One-time migration: backfill NATIVE_MEMORY_RERANK_MODEL_ID from the
    legacy NATIVE_MEMORY_RERANK_MODEL bare string."""
    await _migrate_legacy_model_triplet_to_id(
        legacy_key="NATIVE_MEMORY_RERANK_MODEL",
        new_id_key="NATIVE_MEMORY_RERANK_MODEL_ID",
        expected_kind="rerank",
        provider_prefixes=("openai",),
    )


async def _migrate_legacy_model_triplet_to_id(
    *,
    legacy_key: str,
    new_id_key: str,
    expected_kind: str,
    provider_prefixes: tuple[str, ...],
) -> None:
    """One-time migration helper: backfill a `<NEW>_MODEL_ID` setting from a
    legacy bare-string model setting.

    If the legacy setting was explicitly written to the DB (non-default) and
    the new `_MODEL_ID` is empty, look up a model card by value (trying both
    the raw value and provider-prefixed forms). Cards are filtered to the
    expected `kind` when the stored card carries one (legacy cards without a
    `kind` are treated as chat and still match for chat migrations). Idempotent
    and best-effort: never raises.
    """
    if _settings_service is None:
        return

    try:
        legacy = await _settings_service._storage.get_raw(legacy_key)
    except Exception as exc:
        logger.debug("[Settings] %s migration: failed to read legacy value: %s", legacy_key, exc)
        return

    # Only migrate when the legacy setting was explicitly written to the DB.
    if legacy is None or legacy.updated_at is None:
        return

    legacy_value = legacy.value
    if not isinstance(legacy_value, str) or not legacy_value.strip():
        return

    # Skip if the new ID is already configured (explicitly set in DB).
    new_setting = await _settings_service._storage.get_raw(new_id_key)
    if new_setting is not None and new_setting.updated_at is not None:
        return
    if getattr(settings, new_id_key, ""):
        return

    try:
        from src.infra.agent.model_storage import get_model_storage

        storage = get_model_storage()
        candidates: list[str] = [legacy_value]
        # Model cards may store the value with a provider prefix (e.g.
        # "anthropic/claude-...") while the legacy setting was the bare model
        # name. Try both forms to maximize the chance of a match.
        if "/" not in legacy_value:
            for prefix in provider_prefixes:
                candidates.append(f"{prefix}/{legacy_value}")

        matched_id: Optional[str] = None
        for candidate in candidates:
            stored = await storage.get_by_value(candidate)
            if not stored or not stored.id:
                continue
            # Filter by kind when the card carries an explicit kind. Legacy
            # cards without `kind` default to "chat" and only match chat
            # migrations; non-chat migrations require an explicit kind match.
            stored_kind = getattr(stored, "kind", None) or "chat"
            if stored_kind != expected_kind:
                continue
            matched_id = stored.id
            break

        if matched_id:
            await _settings_service._storage.set(new_id_key, matched_id, "system:migration")
            setattr(settings, new_id_key, matched_id)
            _settings_cache[new_id_key] = matched_id
            logger.info(
                "[Settings] Migrated %s='%s' -> %s='%s'",
                legacy_key,
                legacy_value,
                new_id_key,
                matched_id,
            )
        else:
            logger.warning(
                "[Settings] Could not auto-migrate %s='%s': no matching model "
                "card found by value (kind=%s). Please create a model card of "
                "this kind and select it for %s.",
                legacy_key,
                legacy_value,
                expected_kind,
                new_id_key,
            )
    except Exception as exc:
        logger.warning("[Settings] %s migration failed: %s", legacy_key, exc)


async def initialize_settings() -> None:
    """Initialize settings from database, importing from .env if needed.

    After calling this function, the global `settings` object will have its
    attributes overridden by values from the database (database > env > default).
    """
    global _settings_service, _settings_cache

    from src.infra.settings.service import SettingsService

    _settings_service = SettingsService.get_instance()
    await _settings_service.initialize()
    logger.info("[Settings] SettingsService initialized")

    # Load all settings from database and update the global settings object
    all_settings = await _settings_service.get_all(admin_mode=True, mask_sensitive=False)
    logger.info(f"[Settings] Loaded {len(all_settings)} categories from database")

    # Flatten the settings dict and cache them
    loaded_count = 0
    for category, items in all_settings.items():
        logger.debug(f"[Settings] Category {category}: {len(items)} items")
        for item in items:
            # Empty strings usually mean "keep env fallback", but selected model
            # settings use "" as an intentional "automatic/default" value.
            if (
                item
                and item.value is not None
                and (item.value != "" or item.key in _ALLOW_EMPTY_STRING_SETTINGS)
            ):
                _settings_cache[item.key] = item.value
                # Only update if the field exists in Settings class
                if hasattr(settings, item.key):
                    setattr(settings, item.key, item.value)
                    loaded_count += 1

    logger.info(f"[Settings] Loaded {loaded_count} settings into cache")
    logger.info(f"[Settings] REDIS_URL = {settings.REDIS_URL}")

    # One-time migration of legacy bare-string triplets -> *_MODEL_ID card refs
    await _migrate_session_title_model_to_id()
    await _migrate_native_memory_model_to_id()
    await _migrate_audio_transcription_model_to_id()
    await _migrate_native_memory_embedding_model_to_id()
    await _migrate_native_memory_rerank_model_to_id()


async def refresh_settings(key: Optional[str] = None) -> None:
    """Refresh settings from database.

    Args:
        key: Specific key to refresh, or None for all settings.

    This should be called after database settings are updated.
    """
    global _settings_cache

    if _settings_service is None:
        return

    # Settings that affect LLM model cache (used for title generation etc.)
    llm_affected_settings = {
        "DEFAULT_MODEL_ID",
        "SESSION_TITLE_MODEL_ID",
        "NATIVE_MEMORY_MODEL_ID",
        "LLM_MAX_RETRIES",
    }

    # Settings that require memory backend reinitialization
    memory_affected_settings = {
        "ENABLE_MEMORY",
        "NATIVE_MEMORY_EMBEDDING_MODEL_ID",
    }

    if key:
        # Refresh single setting
        setting = await _settings_service._storage.get_raw(key)
        # Dedicated OpenSandbox node configuration is revisioned in its own
        # collection, not the scalar settings table.  Redis fan-out still uses
        # the existing settings channel, so reset replicas even when get_raw()
        # has no ordinary setting row to return.
        if key == "OPENSANDBOX_NODES" and not setting:
            await _reset_sandbox_runtime_state(f"setting '{key}' changed")
            return
        if (
            setting
            and setting.value is not None
            and (setting.value != "" or key in _ALLOW_EMPTY_STRING_SETTINGS)
        ):
            _settings_cache[key] = setting.value
            setattr(settings, key, setting.value)
            # Clear LLM model cache if this setting affects it
            if key in llm_affected_settings:
                from src.infra.llm.client import LLMClient

                cleared = LLMClient.clear_cache_by_model()
                logger.info(
                    f"[Settings] Cleared {cleared} LLM model cache entries after setting '{key}' changed"
                )
            # Reset memory backend if this setting affects it
            if key in memory_affected_settings:
                from src.infra.memory.tools import schedule_backend_reset

                schedule_backend_reset()
                logger.info(f"[Settings] Memory backend reset after setting '{key}' changed")
            if key in _CHECKPOINT_AFFECTED_SETTINGS:
                await _reset_checkpoint_runtime_state(f"setting '{key}' changed")
            if key in _SANDBOX_AFFECTED_SETTINGS:
                await _reset_sandbox_runtime_state(f"setting '{key}' changed")
    else:
        # Refresh all settings
        all_settings = await _settings_service.get_all(admin_mode=True, mask_sensitive=False)
        any_llm_setting_changed = False
        any_memory_setting_changed = False
        any_checkpoint_setting_changed = False
        any_sandbox_setting_changed = False
        for items in all_settings.values():
            for item in items:
                if (
                    item
                    and item.value is not None
                    and (item.value != "" or item.key in _ALLOW_EMPTY_STRING_SETTINGS)
                ):
                    _settings_cache[item.key] = item.value
                    setattr(settings, item.key, item.value)
                    if item.key in llm_affected_settings:
                        any_llm_setting_changed = True
                    if item.key in memory_affected_settings:
                        any_memory_setting_changed = True
                    if item.key in _CHECKPOINT_AFFECTED_SETTINGS:
                        any_checkpoint_setting_changed = True
                    if item.key in _SANDBOX_AFFECTED_SETTINGS:
                        any_sandbox_setting_changed = True

        # Clear LLM model cache if any affected setting changed
        if any_llm_setting_changed:
            from src.infra.llm.client import LLMClient

            cleared = LLMClient.clear_cache_by_model()
            logger.info(
                f"[Settings] Cleared {cleared} LLM model cache entries after settings refresh"
            )

        # Reset memory backend if any affected setting changed
        if any_memory_setting_changed:
            from src.infra.memory.tools import schedule_backend_reset

            schedule_backend_reset()
            logger.info("[Settings] Memory backend reset after settings refresh")

        if any_checkpoint_setting_changed:
            await _reset_checkpoint_runtime_state("settings refresh")

        if any_sandbox_setting_changed:
            await _reset_sandbox_runtime_state("settings refresh")
