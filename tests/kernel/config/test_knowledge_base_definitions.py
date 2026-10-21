"""Knowledge base settings: definitions, Settings defaults, restart-required and env-only keys."""

from __future__ import annotations

import pytest

from src.kernel.config.base import Settings
from src.kernel.config.constants import RESTART_REQUIRED_SETTINGS, SENSITIVE_SETTINGS
from src.kernel.config.definitions import SETTING_DEFINITIONS
from src.kernel.config.service import _ALLOW_EMPTY_STRING_SETTINGS
from src.kernel.schemas.setting import SettingCategory, SettingType

# key -> (type, default, minimum, maximum)
KB_SETTINGS = {
    "KB_ENABLED": (SettingType.BOOLEAN, True, None, None),
    "KB_EMBEDDING_MODEL_ID": (SettingType.STRING, "", None, None),
    "KB_RERANK_MODEL_ID": (SettingType.STRING, "", None, None),
    "KB_WORKER_CONCURRENCY": (SettingType.NUMBER, 4, 1, 16),
    "KB_MAX_FILE_MB": (SettingType.NUMBER, 100, 1, 1024),
    "KB_EMBEDDING_BATCH_SIZE": (SettingType.NUMBER, 32, 1, 256),
}
ENV_ONLY = {
    "QDRANT_URL": "",
    "QDRANT_API_KEY": "",
    "QDRANT_LOCAL_PATH": "data/qdrant",
    "KB_ARQ_QUEUE_NAME": "kunxiaozhi:kb",
    "KB_JOB_TIMEOUT_SECONDS": 7200,
}


def test_category_exists() -> None:
    assert SettingCategory.KNOWLEDGE_BASE.value == "knowledge_base"


@pytest.mark.parametrize("key,expected", KB_SETTINGS.items())
def test_definition_matches_settings_default_and_ranges(key, expected) -> None:
    setting_type, default, minimum, maximum = expected
    definition = SETTING_DEFINITIONS[key]
    assert definition["type"] == setting_type
    assert definition["category"] == SettingCategory.KNOWLEDGE_BASE
    assert definition["default"] == default
    assert getattr(Settings(), key) == default
    assert definition.get("minimum") == minimum
    assert definition.get("maximum") == maximum
    assert definition["description"] == f"settingDesc.{key}"
    assert definition.get("subcategory")
    assert definition.get("is_sensitive", False) is False


@pytest.mark.parametrize("key", [k for k in KB_SETTINGS if k != "KB_ENABLED"])
def test_everything_but_the_master_switch_depends_on_it(key) -> None:
    assert SETTING_DEFINITIONS[key]["depends_on"] == "KB_ENABLED"
    assert SETTING_DEFINITIONS[key].get("frontend_visible", False) is False


def test_master_switch_is_visible_to_the_frontend_and_has_no_dependency() -> None:
    definition = SETTING_DEFINITIONS["KB_ENABLED"]
    assert definition["frontend_visible"] is True
    assert "depends_on" not in definition


def test_no_other_settings_in_the_category() -> None:
    in_category = {
        key
        for key, definition in SETTING_DEFINITIONS.items()
        if definition["category"] == SettingCategory.KNOWLEDGE_BASE
    }
    assert in_category == set(KB_SETTINGS)


def test_model_ids_may_be_saved_empty() -> None:
    assert {"KB_EMBEDDING_MODEL_ID", "KB_RERANK_MODEL_ID"} <= _ALLOW_EMPTY_STRING_SETTINGS


def test_only_worker_concurrency_requires_a_restart() -> None:
    assert "KB_WORKER_CONCURRENCY" in RESTART_REQUIRED_SETTINGS
    assert not (set(KB_SETTINGS) - {"KB_WORKER_CONCURRENCY"}) & RESTART_REQUIRED_SETTINGS


@pytest.mark.parametrize("key,default", ENV_ONLY.items())
def test_infrastructure_values_are_environment_only(key, default) -> None:
    assert key not in SETTING_DEFINITIONS
    assert getattr(Settings(), key) == default
    assert key not in SENSITIVE_SETTINGS


def test_environment_overrides_infrastructure_values(monkeypatch) -> None:
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("KB_ARQ_QUEUE_NAME", "custom:kb")
    loaded = Settings()
    assert loaded.QDRANT_URL == "http://qdrant:6333" and loaded.KB_ARQ_QUEUE_NAME == "custom:kb"
