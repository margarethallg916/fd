from __future__ import annotations

import pytest

from tests.infra.knowledge_base.domain import kb_fakes


@pytest.fixture
def kb_env(monkeypatch: pytest.MonkeyPatch):
    yield from kb_fakes.kb_env_gen(monkeypatch)
