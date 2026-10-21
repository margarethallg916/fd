"""In-memory fakes for the knowledge base domain tests (Mongo, object storage, engine)."""

from __future__ import annotations

import copy
import hashlib
import math
import re
import zlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

# ── Mongo ────────────────────────────────────────────────────


def _get(doc: dict[str, Any], path: str) -> Any:
    cur: Any = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _is_operator_dict(cond: Any) -> bool:
    return isinstance(cond, dict) and bool(cond) and all(str(k).startswith("$") for k in cond)


def _eq(value: Any, cond: Any) -> bool:
    if isinstance(value, list) and not isinstance(cond, list):
        return cond in value
    return value == cond


def _op(value: Any, op: str, arg: Any, cond: dict[str, Any]) -> bool:
    if op == "$in":
        if isinstance(value, list):
            return any(v in arg for v in value)
        return value in arg
    if op == "$nin":
        return not _op(value, "$in", arg, cond)
    if op == "$ne":
        return not _eq(value, arg)
    if op == "$eq":
        return _eq(value, arg)
    if op in ("$gt", "$gte", "$lt", "$lte"):
        if value is None:
            return False
        return {
            "$gt": value > arg,
            "$gte": value >= arg,
            "$lt": value < arg,
            "$lte": value <= arg,
        }[op]
    if op == "$exists":
        return (value is not None) == bool(arg)
    if op == "$regex":
        flags = re.IGNORECASE if "i" in cond.get("$options", "") else 0
        return isinstance(value, str) and re.search(arg, value, flags) is not None
    if op == "$options":
        return True
    raise AssertionError(f"unsupported operator {op}")


def match(doc: dict[str, Any], flt: dict[str, Any] | None) -> bool:
    for key, cond in (flt or {}).items():
        if key == "$or":
            if not any(match(doc, sub) for sub in cond):
                return False
        elif key == "$and":
            if not all(match(doc, sub) for sub in cond):
                return False
        else:
            value = doc.get("_id") if key == "_id" else _get(doc, key)
            if _is_operator_dict(cond):
                if not all(_op(value, op, arg, cond) for op, arg in cond.items()):
                    return False
            elif not _eq(value, cond):
                return False
    return True


def _project(doc: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    if not projection:
        return copy.deepcopy(doc)
    keep = {k for k, v in projection.items() if v}
    return {k: copy.deepcopy(v) for k, v in doc.items() if k in keep or k == "_id"}


def _apply_update(doc: dict[str, Any], update: dict[str, Any], inserting: bool) -> None:
    for path, value in update.get("$set", {}).items():
        _set(doc, path, copy.deepcopy(value))
    for path in update.get("$unset", {}):
        doc.pop(path, None)
    for path, amount in update.get("$inc", {}).items():
        _set(doc, path, (_get(doc, path) or 0) + amount)
    for path, spec in update.get("$addToSet", {}).items():
        items = spec["$each"] if isinstance(spec, dict) and "$each" in spec else [spec]
        current = _get(doc, path)
        if current is None:
            current = []
            _set(doc, path, current)
        for item in items:
            if item not in current:
                current.append(copy.deepcopy(item))
    if inserting:
        for path, value in update.get("$setOnInsert", {}).items():
            _set(doc, path, copy.deepcopy(value))


def _sort_key(value: Any) -> tuple[int, Any]:
    return (0, 0) if value is None else (1, value)


class FakeCursor:
    def __init__(self, docs: list[dict[str, Any]], projection: Any = None) -> None:
        self._docs = docs
        self._projection = projection
        self._skip = 0
        self._limit = 0

    def sort(self, key: Any, direction: int | None = None) -> FakeCursor:
        keys = key if isinstance(key, list) else [(key, direction or 1)]
        for field_name, order in reversed(keys):
            self._docs.sort(key=lambda d: _sort_key(_get(d, field_name)), reverse=order == -1)
        return self

    def skip(self, n: int) -> FakeCursor:
        self._skip = n
        return self

    def limit(self, n: int) -> FakeCursor:
        self._limit = n
        return self

    def _window(self) -> list[dict[str, Any]]:
        docs = self._docs[self._skip :]
        docs = docs[: self._limit] if self._limit else docs
        return [_project(d, self._projection) for d in docs]

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        docs = self._window()
        return docs[:length] if length else docs

    def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        async def gen() -> AsyncIterator[dict[str, Any]]:
            for doc in self._window():
                yield doc

        return gen()


@dataclass
class _Result:
    matched_count: int = 0
    modified_count: int = 0
    upserted_id: Any = None
    deleted_count: int = 0
    inserted_id: Any = None


@dataclass
class _Index:
    keys: list[tuple[str, int]]
    unique: bool
    partial: dict[str, Any] | None
    name: str | None


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.indexes: list[_Index] = []

    # indexes ---------------------------------------------------
    async def create_index(self, keys: list[tuple[str, int]], **kwargs: Any) -> str:
        self.indexes.append(
            _Index(
                keys=list(keys),
                unique=bool(kwargs.get("unique")),
                partial=kwargs.get("partialFilterExpression"),
                name=kwargs.get("name"),
            )
        )
        return kwargs.get("name") or "_".join(f"{k}_{d}" for k, d in keys)

    def _check_unique(self, candidate: dict[str, Any], exclude: dict[str, Any] | None) -> None:
        for doc in self.docs:
            if exclude is not None and doc is exclude:
                continue
            if doc.get("_id") == candidate.get("_id"):
                raise DuplicateKeyError("E11000 duplicate key on _id")
        for index in self.indexes:
            if not index.unique:
                continue
            if index.partial and not match(candidate, index.partial):
                continue
            key = tuple(_get(candidate, f) for f, _ in index.keys)
            for doc in self.docs:
                if exclude is not None and doc is exclude:
                    continue
                if index.partial and not match(doc, index.partial):
                    continue
                if tuple(_get(doc, f) for f, _ in index.keys) == key:
                    raise DuplicateKeyError(f"E11000 duplicate key on {index.name or index.keys}")

    # writes ----------------------------------------------------
    async def insert_one(self, doc: dict[str, Any]) -> _Result:
        stored = copy.deepcopy(doc)
        self._check_unique(stored, None)
        self.docs.append(stored)
        return _Result(inserted_id=stored["_id"])

    async def insert_many(self, docs: list[dict[str, Any]]) -> None:
        for doc in docs:
            await self.insert_one(doc)

    def _first(self, flt: dict[str, Any], sort: Any = None) -> dict[str, Any] | None:
        found = [d for d in self.docs if match(d, flt)]
        if sort:
            found = FakeCursor(found).sort(sort)._docs  # noqa: SLF001
        return found[0] if found else None

    def _upsert_doc(self, flt: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = {}
        for key, cond in flt.items():
            if not key.startswith("$") and not _is_operator_dict(cond):
                _set(doc, key, copy.deepcopy(cond))
        _apply_update(doc, update, inserting=True)
        doc.setdefault("_id", f"auto-{len(self.docs)}")
        return doc

    def _update(self, doc: dict[str, Any], update: dict[str, Any]) -> bool:
        before = copy.deepcopy(doc)
        candidate = copy.deepcopy(doc)
        _apply_update(candidate, update, inserting=False)
        if candidate == before:
            return False
        self._check_unique(candidate, doc)
        doc.clear()
        doc.update(candidate)
        return True

    async def update_one(
        self, flt: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _Result:
        doc = self._first(flt)
        if doc is None:
            if not upsert:
                return _Result()
            new = self._upsert_doc(flt, update)
            self._check_unique(new, None)
            self.docs.append(new)
            return _Result(upserted_id=new["_id"])
        return _Result(matched_count=1, modified_count=int(self._update(doc, update)))

    async def update_many(self, flt: dict[str, Any], update: dict[str, Any]) -> _Result:
        matched = [d for d in self.docs if match(d, flt)]
        modified = sum(int(self._update(d, update)) for d in matched)
        return _Result(matched_count=len(matched), modified_count=modified)

    async def find_one_and_update(
        self,
        flt: dict[str, Any],
        update: dict[str, Any],
        projection: Any = None,
        upsert: bool = False,
        return_document: Any = ReturnDocument.BEFORE,
        sort: Any = None,
    ) -> dict[str, Any] | None:
        doc = self._first(flt, sort)
        if doc is None:
            if not upsert:
                return None
            new = self._upsert_doc(flt, update)
            self._check_unique(new, None)
            self.docs.append(new)
            return copy.deepcopy(new) if return_document == ReturnDocument.AFTER else None
        before = copy.deepcopy(doc)
        self._update(doc, update)
        return copy.deepcopy(doc) if return_document == ReturnDocument.AFTER else before

    async def delete_one(self, flt: dict[str, Any]) -> _Result:
        doc = self._first(flt)
        if doc is None:
            return _Result()
        self.docs.remove(doc)
        return _Result(deleted_count=1)

    async def delete_many(self, flt: dict[str, Any]) -> _Result:
        keep = [d for d in self.docs if not match(d, flt)]
        removed = len(self.docs) - len(keep)
        self.docs[:] = keep
        return _Result(deleted_count=removed)

    # reads -----------------------------------------------------
    def find(self, flt: dict[str, Any] | None = None, projection: Any = None) -> FakeCursor:
        return FakeCursor([d for d in self.docs if match(d, flt)], projection)

    async def find_one(
        self, flt: dict[str, Any] | None = None, projection: Any = None, sort: Any = None
    ) -> dict[str, Any] | None:
        doc = self._first(flt or {}, sort)
        return _project(doc, projection) if doc is not None else None

    async def count_documents(self, flt: dict[str, Any], limit: int = 0) -> int:
        count = sum(1 for d in self.docs if match(d, flt))
        return min(count, limit) if limit else count

    def aggregate(self, pipeline: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        rows: list[dict[str, Any]] = [copy.deepcopy(d) for d in self.docs]
        for stage in pipeline:
            if "$match" in stage:
                rows = [r for r in rows if match(r, stage["$match"])]
            elif "$group" in stage:
                rows = self._group(rows, stage["$group"])
            elif "$sort" in stage:
                for key, order in reversed(list(stage["$sort"].items())):
                    rows.sort(key=lambda r: _sort_key(_get(r, key)), reverse=order == -1)
            elif "$limit" in stage:
                rows = rows[: stage["$limit"]]
            else:
                raise AssertionError(f"unsupported stage {stage}")

        async def gen() -> AsyncIterator[dict[str, Any]]:
            for row in rows:
                yield row

        return gen()

    @staticmethod
    def _expr(row: dict[str, Any], expr: Any) -> Any:
        if isinstance(expr, str) and expr.startswith("$"):
            return _get(row, expr[1:])
        if isinstance(expr, dict):
            return {k: FakeCollection._expr(row, v) for k, v in expr.items()}
        return expr

    @staticmethod
    def _group(rows: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            gid = FakeCollection._expr(row, spec["_id"])
            bucket = groups.setdefault(repr(gid), {"_id": gid})
            for name, acc in spec.items():
                if name == "_id":
                    continue
                (op, arg), = acc.items()
                assert op == "$sum", op
                amount = 1 if arg == 1 else (FakeCollection._expr(row, arg) or 0)
                bucket[name] = bucket.get(name, 0) + amount
        return list(groups.values())


class FakeDatabase:
    def __init__(self) -> None:
        self._collections: dict[str, FakeCollection] = {}

    def __getitem__(self, name: str) -> FakeCollection:
        return self._collections.setdefault(name, FakeCollection())


class FakeMongoClient:
    def __init__(self) -> None:
        self._dbs: dict[str, FakeDatabase] = {}

    def __getitem__(self, name: str) -> FakeDatabase:
        return self._dbs.setdefault(name, FakeDatabase())


# ── object storage ───────────────────────────────────────────


class FakeObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_uploads = False

    async def upload_stream_to_key(self, file: Any, key: str, content_type=None, metadata=None, **_):
        if self.fail_uploads:
            raise RuntimeError("storage down")
        self.objects[key] = file.read()

    async def upload_to_key(self, data: bytes, key: str, content_type=None, metadata=None, **_):
        self.objects[key] = bytes(data)

    async def download_stream(self, key: str, chunk_size: int = 1024) -> AsyncIterator[bytes]:
        if key not in self.objects:
            raise FileNotFoundError(key)
        data = self.objects[key]
        for start in range(0, len(data), chunk_size):
            yield data[start : start + chunk_size]

    async def file_exists(self, key: str) -> bool:
        return key in self.objects

    async def delete_file(self, key: str) -> bool:
        return self.objects.pop(key, None) is not None

    async def list_files(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


# ── engine ───────────────────────────────────────────────────


def fake_vector(text: str, dim: int = 8) -> list[float]:
    vec = [0.0] * dim
    for ch in text.lower():
        vec[ord(ch) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class FakeVectorStore:
    """Dict-backed stand-in for ``KBVectorStore`` (cosine for dense, dot product for sparse)."""

    collections: dict[str, list[Any]] = field(default_factory=dict)
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    async def ensure_collection(self, dim: int, model_name: str) -> str:
        name = f"kb_{dim}_{hashlib.sha1(model_name.encode()).hexdigest()[:8]}"
        self.collections.setdefault(name, [])
        self.calls.append(("ensure_collection", (dim, model_name)))
        return name

    async def upsert_chunks(self, collection: str, points: list[Any]) -> None:
        store = self.collections.setdefault(collection, [])
        ids = {p.chunk_id for p in points}
        store[:] = [p for p in store if p.chunk_id not in ids] + list(points)
        self.calls.append(("upsert_chunks", (collection, len(points))))

    async def delete_by_doc(self, collection: str, doc_id: str) -> None:
        store = self.collections.get(collection, [])
        store[:] = [p for p in store if p.doc_id != doc_id]
        self.calls.append(("delete_by_doc", (collection, doc_id)))

    async def delete_by_kb(self, collection: str, kb_id: str) -> None:
        store = self.collections.get(collection, [])
        store[:] = [p for p in store if p.kb_id != kb_id]
        self.calls.append(("delete_by_kb", (collection, kb_id)))

    def _hit(self, point: Any, score: float):
        from src.infra.knowledge_base.vector_store import VectorHit

        return VectorHit(
            chunk_id=point.chunk_id,
            kb_id=point.kb_id,
            doc_id=point.doc_id,
            index=point.index,
            text=point.text,
            start_line=point.start_line,
            end_line=point.end_line,
            score=score,
        )

    async def search_dense(self, collection, kb_ids, vector, limit):
        self.calls.append(("search_dense", (collection, tuple(kb_ids), limit)))
        hits = [
            self._hit(p, sum(a * b for a, b in zip(p.dense, vector, strict=False)))
            for p in self.collections.get(collection, [])
            if p.kb_id in kb_ids
        ]
        return sorted(hits, key=lambda h: -h.score)[:limit]

    async def search_sparse(self, collection, kb_ids, vector, limit):
        self.calls.append(("search_sparse", (collection, tuple(kb_ids), limit)))
        if not vector.indices:
            return []
        query = dict(zip(vector.indices, vector.values, strict=True))
        hits = []
        for p in self.collections.get(collection, []):
            if p.kb_id not in kb_ids:
                continue
            doc = dict(zip(p.sparse.indices, p.sparse.values, strict=True))
            score = sum(v * doc.get(i, 0.0) for i, v in query.items())
            if score > 0:
                hits.append(self._hit(p, score))
        return sorted(hits, key=lambda h: -h.score)[:limit]

    async def close(self) -> None:
        return None


@dataclass
class EngineFakes:
    vectors: FakeVectorStore
    objects: FakeObjectStorage
    mongo: FakeMongoClient
    published: list[str]
    parse_error: Exception | None = None
    embed_error: Exception | None = None
    card_missing: bool = False
    rerank_scores: list[float] | None = None
    rerank_card_present: bool = False
    embed_calls: list[list[str]] = field(default_factory=list)
    model_name: str = "fake-embed"


def install_kb_fakes(monkeypatch: pytest.MonkeyPatch) -> EngineFakes:
    """Replace Mongo, object storage, vector store and engine functions with fakes."""
    from src.infra.knowledge_base import (
        chunking,
        embedding,
        fusion,
        objects,
        parsing,
        sparse,
        vector_store,
    )
    from src.infra.knowledge_base import events as kb_events
    from src.infra.knowledge_base import queue as kb_queue
    from src.infra.knowledge_base import storage as kb_storage
    from src.kernel.config import settings

    mongo = FakeMongoClient()
    monkeypatch.setattr(kb_storage, "get_mongo_client", lambda: mongo)
    monkeypatch.setattr(kb_storage, "_storage", None)

    object_store = FakeObjectStorage()

    async def _storage():
        return object_store

    monkeypatch.setattr(objects, "get_or_init_storage", _storage)

    fakes = EngineFakes(
        vectors=FakeVectorStore(), objects=object_store, mongo=mongo, published=[]
    )
    monkeypatch.setattr(vector_store, "get_vector_store", lambda: fakes.vectors)

    # settings
    monkeypatch.setattr(settings, "KB_ENABLED", True)
    monkeypatch.setattr(settings, "KB_MAX_FILE_MB", 1)
    monkeypatch.setattr(settings, "TASK_BACKEND", "local")
    monkeypatch.setattr(settings, "MONGODB_DB", "kb_test")

    # queue: record wake-ups instead of running anything
    async def _publish(task_id: str) -> None:
        fakes.published.append(task_id)

    kb_queue.set_task_publisher(_publish)
    monkeypatch.setattr(kb_queue, "_local_runner", None)

    async def _no_push(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(kb_events, "push_event", _no_push)

    # parsing
    supported = frozenset({".pdf", ".md", ".txt", ".csv", ".docx"})

    def _ext(name: str) -> str:
        return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""

    monkeypatch.setattr(parsing, "normalize_extension", _ext)
    monkeypatch.setattr(parsing, "is_supported_filename", lambda n: _ext(n) in supported)
    monkeypatch.setattr(parsing, "SUPPORTED_EXTENSIONS", supported)
    monkeypatch.setattr(parsing, "mineru_available", lambda: False)

    async def _parse(filename: str, data: bytes, content_type: str | None = None):
        if fakes.parse_error is not None:
            raise fakes.parse_error
        return parsing.ParsedDocument(markdown=data.decode("utf-8"), method="local")

    monkeypatch.setattr(parsing, "parse_document", _parse)

    # chunking: one chunk per non-empty line
    def _chunk(markdown: str, *, filename: str, preset=None, chunk_token_num=None, overlap_percent=None):
        out = []
        for number, line in enumerate(markdown.splitlines(), start=1):
            if line.strip():
                out.append(
                    chunking.TextChunk(
                        index=len(out),
                        text=line,
                        start_line=number,
                        end_line=number,
                        token_count=len(line.split()) or 1,
                    )
                )
        return out

    monkeypatch.setattr(chunking, "chunk_markdown", _chunk)

    # embedding
    async def _card(model_id: str | None = None):
        if fakes.card_missing:
            return None
        return embedding.ModelCard(
            model_id="card-1", model_name=fakes.model_name, api_base="http://x", api_key="k"
        )

    async def _rerank_card():
        if not fakes.rerank_card_present:
            return None
        return embedding.ModelCard(
            model_id="rr", model_name="fake-rerank", api_base="http://x", api_key="k"
        )

    async def _embed(texts: list[str], card):
        fakes.embed_calls.append(list(texts))
        if fakes.embed_error is not None:
            raise fakes.embed_error
        return [fake_vector(t) for t in texts]

    async def _rerank(query: str, documents: list[str], card):
        if fakes.rerank_scores is None:
            return None
        return list(fakes.rerank_scores[: len(documents)])

    monkeypatch.setattr(embedding, "resolve_embedding_card", _card)
    monkeypatch.setattr(embedding, "resolve_rerank_card", _rerank_card)
    monkeypatch.setattr(embedding, "embed_texts", _embed)
    monkeypatch.setattr(embedding, "rerank", _rerank)

    # sparse
    def _tokens(text: str) -> list[str]:
        return [t for t in re.split(r"\W+", text.lower()) if t]

    def _encode_doc(text: str):
        counts: dict[int, float] = {}
        for tok in _tokens(text):
            idx = zlib.crc32(tok.encode()) & 0x7FFFFFFF
            counts[idx] = counts.get(idx, 0.0) + 1.0
        return sparse.SparseVec(indices=list(counts), values=list(counts.values()))

    def _encode_query(text: str):
        idx = sorted({zlib.crc32(t.encode()) & 0x7FFFFFFF for t in _tokens(text)})
        return sparse.SparseVec(indices=idx, values=[1.0] * len(idx))

    monkeypatch.setattr(sparse, "encode_document", _encode_doc)
    monkeypatch.setattr(sparse, "encode_query", _encode_query)

    # fusion: simple, deterministic stand-ins mirroring the contract
    def _fusion(dense, sparse_hits, *, vector_weight: float = 0.7):
        from dataclasses import replace

        sp = {h.chunk_id: h.score for h in sparse_hits}
        top = max(sp.values(), default=0.0)
        merged = {}
        for h in dense:
            merged[h.chunk_id] = (h, h.score, 0.0)
        for h in sparse_hits:
            norm = (sp[h.chunk_id] / top) if top else 0.0
            base = merged.get(h.chunk_id, (h, 0.0, 0.0))
            merged[h.chunk_id] = (base[0], base[1], norm)
        hits = [
            replace(h, score=vector_weight * d + (1 - vector_weight) * s)
            for h, d, s in merged.values()
        ]
        return sorted(hits, key=lambda h: (-h.score, h.chunk_id))

    monkeypatch.setattr(fusion, "weighted_fusion", _fusion)
    monkeypatch.setattr(
        fusion, "apply_threshold", lambda hits, t: [h for h in hits if h.score >= t]
    )

    def _apply_rerank(hits, scores):
        from dataclasses import replace

        if len(scores) != len(hits):
            return list(hits)
        return sorted(
            (replace(h, score=s) for h, s in zip(hits, scores, strict=True)), key=lambda h: -h.score
        )

    monkeypatch.setattr(fusion, "apply_rerank_scores", _apply_rerank)
    return fakes


async def wait_until(predicate, timeout: float = 2.0) -> None:
    import asyncio

    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def make_user(sub: str = "u1", username: str | None = None):
    from src.kernel.schemas.user import TokenPayload

    return TokenPayload(sub=sub, username=username or sub, roles=["user"], permissions=[])


def kb_env_gen(monkeypatch: pytest.MonkeyPatch):
    """Generator body shared by the ``kb_env`` fixtures (domain tests and route tests)."""
    from src.infra.knowledge_base import queue as kb_queue

    fakes = install_kb_fakes(monkeypatch)
    yield fakes
    kb_queue.set_task_publisher(None)
