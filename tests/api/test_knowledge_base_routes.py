"""Route contract for /api/kb (bare FastAPI, fake Mongo/object storage/engine)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api import deps as api_deps
from src.api.main import _is_body_limit_exempt
from src.api.routes import knowledge_base as routes
from src.infra.knowledge_base import pipeline
from src.infra.knowledge_base.storage import get_kb_storage
from src.infra.utils.datetime import utc_now
from src.kernel.config import settings
from src.kernel.schemas.user import TokenPayload
from tests.infra.knowledge_base.domain import kb_fakes

BASE = "/api/kb"


@pytest.fixture
def kb_env(monkeypatch: pytest.MonkeyPatch):
    yield from kb_fakes.kb_env_gen(monkeypatch)


@dataclass
class Env:
    client: AsyncClient
    fakes: kb_fakes.EngineFakes
    current: dict = field(default_factory=dict)

    def as_user(self, sub: str) -> None:
        self.current["user"] = TokenPayload(sub=sub, username=f"name-{sub}", roles=["user"])

    async def create(self, **body) -> dict:
        resp = await self.client.post(BASE, json={"name": "我的库", **body})
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def upload(self, kb_id: str, name: str = "a.md", data: bytes = b"hello world"):
        return await self.client.post(
            f"{BASE}/{kb_id}/documents/upload",
            files={"file": (name, data, "text/markdown")},
        )

    async def index_all(self) -> None:
        storage = get_kb_storage()
        for task in list(storage.tasks.docs):
            if task["status"] == "queued":
                await storage.claim_task(task["_id"], "w", utc_now(), 60)
                for doc_id in task["doc_ids"]:
                    await pipeline.process_document(doc_id, task["_id"])
                await storage.finish_task(task["_id"], "w", "succeeded")


@pytest_asyncio.fixture
async def env(kb_env) -> AsyncIterator[Env]:
    app = FastAPI()
    app.include_router(routes.router, prefix=BASE)
    current: dict = {}
    app.dependency_overrides[api_deps.get_current_user_required] = lambda: current["user"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        holder = Env(client=client, fakes=kb_env, current=current)
        holder.as_user("owner-1")
        yield holder


async def test_config_is_not_shadowed_by_kb_id_route(env: Env) -> None:
    resp = await env.client.get(f"{BASE}/config")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "enabled",
        "ready",
        "not_ready_reason",
        "presets",
        "allowed_extensions",
        "mineru_enabled",
        "max_file_mb",
        "rerank_available",
    }
    assert body["enabled"] is True and body["ready"] is True and body["not_ready_reason"] is None
    assert body["max_file_mb"] == 1 and ".md" in body["allowed_extensions"]
    assert {"key", "chunk_token_num", "overlap_percent"} == set(body["presets"][0])

    env.fakes.card_missing = True
    assert (await env.client.get(f"{BASE}/config")).json()["not_ready_reason"] == "embedding_model_missing"


async def test_kb_crud_contract(env: Env) -> None:
    created = await env.create(description="d", chunk_preset="qa", chunk_token_num=256, overlap_percent=5)
    assert set(created) == {
        "id",
        "name",
        "description",
        "icon",
        "owner_id",
        "owner_username",
        "my_role",
        "chunk_preset",
        "chunk_token_num",
        "overlap_percent",
        "visibility",
        "graph_enabled",
        "embedding",
        "index_outdated",
        "stats",
        "active_task",
        "created_at",
        "updated_at",
    }
    assert created["my_role"] == "owner" and created["owner_id"] == "owner-1"
    assert set(created["stats"]) == {
        "doc_count",
        "indexed_count",
        "processing_count",
        "failed_count",
        "chunk_count",
        "size_bytes",
    }
    kb_id = created["id"]

    listed = await env.client.get(BASE, params={"scope": "mine"})
    assert listed.status_code == 200 and [k["id"] for k in listed.json()["items"]] == [kb_id]
    assert (await env.client.get(f"{BASE}/{kb_id}")).json()["name"] == "我的库"

    patched = await env.client.patch(f"{BASE}/{kb_id}", json={"name": "新名", "overlap_percent": 10})
    assert patched.status_code == 200
    assert patched.json()["name"] == "新名" and patched.json()["overlap_percent"] == 10

    deleted = await env.client.delete(f"{BASE}/{kb_id}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert (await env.client.get(f"{BASE}/{kb_id}")).status_code == 404


async def test_validation_errors_are_400_with_string_detail(env: Env) -> None:
    for body in ({"name": ""}, {"name": "ok", "chunk_token_num": 10}, {"name": "ok", "chunk_preset": "nope"}):
        resp = await env.client.post(BASE, json=body)
        assert resp.status_code == 400 and isinstance(resp.json()["detail"], str)
    kb = await env.create()
    resp = await env.client.post(f"{BASE}/{kb['id']}/search", json={"query": "", "top_k": 5})
    assert resp.status_code == 400
    resp = await env.client.get(BASE, params={"scope": "shared"})
    assert resp.status_code == 400


async def test_other_users_get_404_with_fixed_message(env: Env) -> None:
    kb = await env.create()
    uploaded = (await env.upload(kb["id"])).json()["document"]
    env.as_user("intruder")
    doc_url = f"{BASE}/{kb['id']}/documents/{uploaded['id']}"
    requests = [
        ("get", f"{BASE}/{kb['id']}", None),
        ("patch", f"{BASE}/{kb['id']}", {"name": "n"}),
        ("delete", f"{BASE}/{kb['id']}", None),
        ("get", f"{BASE}/{kb['id']}/documents", None),
        ("get", doc_url, None),
        ("get", doc_url + "/content", None),
        ("get", doc_url + "/chunks", None),
        ("get", doc_url + "/download", None),
        ("post", f"{BASE}/{kb['id']}/documents/delete", {"doc_ids": [uploaded["id"]]}),
        ("post", f"{BASE}/{kb['id']}/documents/retry", {"doc_ids": []}),
        ("post", f"{BASE}/{kb['id']}/search", {"query": "hello"}),
        ("get", f"{BASE}/{kb['id']}/tasks", None),
        ("post", f"{BASE}/{kb['id']}/tasks/t1/cancel", None),
        ("post", f"{BASE}/{kb['id']}/reindex", None),
    ]
    for method, url, body in requests:
        resp = await env.client.request(method, url, json=body)
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json() == {"detail": "知识库不存在"}
    upload = await env.upload(kb["id"], "z.md", b"zzz")
    assert upload.status_code == 404
    assert (await env.client.get(BASE)).json() == {"items": []}


async def test_kb_disabled_returns_structured_403(env: Env, monkeypatch) -> None:
    kb = await env.create()
    monkeypatch.setattr(settings, "KB_ENABLED", False)
    expected = {"detail": {"code": "kb_disabled", "message": "知识库功能未开启"}}
    for method, url in (
        ("get", BASE),
        ("post", BASE),
        ("get", f"{BASE}/{kb['id']}"),
        ("get", f"{BASE}/{kb['id']}/documents"),
    ):
        resp = await env.client.request(method, url, json={"name": "x"} if method == "post" else None)
        assert resp.status_code == 403 and resp.json() == expected
    config = await env.client.get(f"{BASE}/config")
    assert config.status_code == 200 and config.json()["enabled"] is False


async def test_upload_returns_201_then_200_for_duplicates(env: Env) -> None:
    kb = await env.create()
    first = await env.upload(kb["id"], "a.md", b"first body")
    assert first.status_code == 201
    payload = first.json()
    assert payload["duplicate"] is False
    doc = payload["document"]
    assert set(doc) == {
        "id",
        "kb_id",
        "filename",
        "ext",
        "mime",
        "size",
        "status",
        "error",
        "parse_method",
        "chunk_count",
        "token_count",
        "line_count",
        "uploader_username",
        "source",
        "created_at",
        "updated_at",
        "indexed_at",
    }
    assert doc["status"] == "uploaded" and doc["uploader_username"] == "name-owner-1"

    again = await env.upload(kb["id"], "copy.md", b"first body")
    assert again.status_code == 200
    assert again.json()["duplicate"] is True and again.json()["document"]["id"] == doc["id"]

    fresh = (await env.client.get(f"{BASE}/{kb['id']}")).json()
    assert fresh["stats"]["doc_count"] == 1 and fresh["active_task"]["status"] == "queued"


async def test_upload_rejections(env: Env) -> None:
    kb = await env.create()
    bad_type = await env.upload(kb["id"], "run.exe", b"MZ")
    assert bad_type.status_code == 400 and isinstance(bad_type.json()["detail"], str)
    too_big = await env.upload(kb["id"], "big.md", b"a" * (1024 * 1024 + 1))
    assert too_big.status_code == 400 and "1MB" in too_big.json()["detail"]
    empty = await env.upload(kb["id"], "empty.md", b"")
    assert empty.status_code == 400
    assert (await env.client.post(f"{BASE}/{kb['id']}/documents/upload")).status_code == 422


async def test_document_endpoints_after_indexing(env: Env) -> None:
    kb = await env.create()
    doc = (await env.upload(kb["id"], "a.md", b"alpha one\nbeta two\ngamma three")).json()["document"]
    await env.index_all()
    base = f"{BASE}/{kb['id']}/documents"

    listing = (await env.client.get(base, params={"status": "indexed", "q": "A", "page": 1, "page_size": 10})).json()
    assert set(listing) == {"items", "total", "page", "page_size"}
    assert listing["total"] == 1 and listing["items"][0]["status"] == "indexed"
    assert listing["items"][0]["chunk_count"] == 3 and listing["items"][0]["parse_method"] == "local"
    assert (await env.client.get(base, params={"status": "failed"})).json()["total"] == 0

    detail = (await env.client.get(f"{base}/{doc['id']}")).json()
    assert detail["id"] == doc["id"] and detail["status"] == "indexed"
    assert (await env.client.get(f"{base}/missing")).status_code == 404

    content = (await env.client.get(f"{base}/{doc['id']}/content", params={"offset": 2, "limit": 1})).json()
    assert content == {
        "doc_id": doc["id"],
        "filename": "a.md",
        "total_lines": 3,
        "offset": 2,
        "lines": [{"n": 2, "text": "beta two"}],
        "has_more": True,
    }
    chunks = (await env.client.get(f"{base}/{doc['id']}/chunks", params={"page_size": 2})).json()
    assert set(chunks) == {"items", "total", "page", "page_size"} and chunks["total"] == 3
    assert set(chunks["items"][0]) == {
        "chunk_id",
        "index",
        "text",
        "start_line",
        "end_line",
        "token_count",
    }

    deleted = await env.client.post(f"{base}/delete", json={"doc_ids": [doc["id"]]})
    assert deleted.status_code == 200 and deleted.json() == {"deleted": 1}
    assert (await env.client.post(f"{base}/delete", json={"doc_ids": []})).status_code == 400


async def test_content_before_parsing_is_400(env: Env) -> None:
    kb = await env.create()
    doc = (await env.upload(kb["id"])).json()["document"]
    resp = await env.client.get(f"{BASE}/{kb['id']}/documents/{doc['id']}/content")
    assert resp.status_code == 400


async def test_download_is_an_attachment_with_nosniff(env: Env) -> None:
    kb = await env.create()
    doc = (await env.upload(kb["id"], "报告 final.md", b"raw payload")).json()["document"]
    resp = await env.client.get(f"{BASE}/{kb['id']}/documents/{doc['id']}/download")
    assert resp.status_code == 200 and resp.content == b"raw payload"
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["x-content-type-options"] == "nosniff"
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "filename*=UTF-8''" in disposition
    assert resp.headers["content-length"] == str(len(b"raw payload"))

    env.fakes.objects.objects.clear()
    assert (await env.client.get(f"{BASE}/{kb['id']}/documents/{doc['id']}/download")).status_code == 404


async def test_retry_and_reindex_contract(env: Env) -> None:
    kb = await env.create()
    doc = (await env.upload(kb["id"], "a.md", b"alpha")).json()["document"]
    storage = get_kb_storage()
    await storage.documents.update_one(
        {"_id": doc["id"]}, {"$set": {"status": "error_parsing", "error": {"stage": "parse", "message": "bad", "at": None}}}
    )
    listed = (await env.client.get(f"{BASE}/{kb['id']}/documents")).json()["items"][0]
    assert listed["error"] == {"stage": "parse", "message": "bad", "at": None}

    retried = await env.client.post(f"{BASE}/{kb['id']}/documents/retry", json={"doc_ids": []})
    assert retried.status_code == 200 and retried.json()["count"] == 1 and retried.json()["task_id"]
    assert (await env.client.post(f"{BASE}/{kb['id']}/documents/retry")).json() == {"task_id": None, "count": 0}

    await env.index_all()
    reindexed = await env.client.post(f"{BASE}/{kb['id']}/reindex")
    assert reindexed.status_code == 200 and reindexed.json()["count"] == 1

    task = storage.tasks.docs[-1]
    await storage.claim_task(task["_id"], "w", utc_now(), 60)
    busy = await env.client.post(f"{BASE}/{kb['id']}/reindex")
    assert busy.status_code == 409


async def test_search_contract(env: Env) -> None:
    kb = await env.create(name="检索库")
    await env.upload(kb["id"], "a.md", b"apple banana\ncherry")
    await env.index_all()
    resp = await env.client.post(
        f"{BASE}/{kb['id']}/search",
        json={"query": "apple", "top_k": 2, "mode": "hybrid", "use_rerank": None, "score_threshold": 0, "vector_weight": 0.7},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"results", "warnings", "took_ms"} and body["warnings"] == []
    hit = body["results"][0]
    assert set(hit) == {
        "chunk_id",
        "kb_id",
        "kb_name",
        "doc_id",
        "filename",
        "index",
        "text",
        "start_line",
        "end_line",
        "score",
        "rerank_score",
    }
    assert hit["filename"] == "a.md" and hit["kb_name"] == "检索库" and hit["text"] == "apple banana"

    env.fakes.model_name = "swapped"
    outdated = (await env.client.post(f"{BASE}/{kb['id']}/search", json={"query": "apple"})).json()
    assert outdated["warnings"] == ["index_outdated"]
    assert (await env.client.get(f"{BASE}/{kb['id']}")).json()["index_outdated"] is True


async def test_tasks_and_cancel_contract(env: Env) -> None:
    kb = await env.create()
    await env.upload(kb["id"])
    tasks = (await env.client.get(f"{BASE}/{kb['id']}/tasks", params={"active": "true"})).json()["items"]
    assert len(tasks) == 1
    task = tasks[0]
    assert set(task) == {
        "id",
        "kb_id",
        "type",
        "status",
        "progress",
        "attempts",
        "error",
        "created_at",
        "started_at",
        "finished_at",
    }
    assert set(task["progress"]) == {"total", "done", "failed", "current_doc_id"}
    assert task["type"] == "ingest" and task["status"] == "queued"

    cancelled = await env.client.post(f"{BASE}/{kb['id']}/tasks/{task['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await env.client.get(f"{BASE}/{kb['id']}/tasks", params={"active": "true"})).json()["items"] == []
    assert (await env.client.post(f"{BASE}/{kb['id']}/tasks/nope/cancel")).status_code == 404


async def test_unexpected_errors_become_generic_500(env: Env, monkeypatch) -> None:
    async def boom(*_a, **_k):
        raise RuntimeError("secret stack detail")

    monkeypatch.setattr(routes.service, "list_kbs", boom)
    resp = await env.client.get(BASE)
    assert resp.status_code == 500 and resp.json() == {"detail": routes.GENERIC_ERROR}


def _scope(path: str, content_type: str = "multipart/form-data; boundary=x") -> dict:
    return {"path": path, "headers": [(b"content-type", content_type.encode())]}


def test_upload_path_is_exempt_from_the_body_limit() -> None:
    assert _is_body_limit_exempt(_scope("/api/kb/abc123/documents/upload"))
    assert not _is_body_limit_exempt(_scope("/api/kb/abc123/documents/upload", "application/json"))
    assert not _is_body_limit_exempt(_scope("/api/kb/abc/123/documents/upload"))
    assert not _is_body_limit_exempt(_scope("/api/kb/abc123/documents/retry"))
    assert not _is_body_limit_exempt(_scope("/api/kb/abc123/documents/upload/extra"))


def test_router_registered_under_api_kb() -> None:
    from src.api.main import create_app

    paths = {getattr(r, "path", "") for r in create_app().routes}
    assert {"/api/kb/config", "/api/kb", "/api/kb/{kb_id}/documents/upload", "/api/kb/{kb_id}/reindex"} <= paths
