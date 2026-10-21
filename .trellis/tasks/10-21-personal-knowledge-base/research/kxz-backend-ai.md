# 昆小智 backend — existing AI-side infrastructure (reuse guide for the personal KB)

Scope: read-only research of `/tmp/hoplite/workspace` (HEAD `1498b534`). All paths are repo-relative
(`/tmp/hoplite/workspace/<path>`). Line numbers are from the working tree at research time.
Anything I could not confirm from code is marked **UNCONFIRMED**.

## 0. TL;DR — the 12 things an implementer must know

1. **MinerU** = `MinerUClient.parse_bytes(filename, content, content_type) -> str` (async, bytes in → markdown out,
   300 s timeout × 3 retries). Config is **admin settings** (`ENABLE_DOCUMENT_PARSE`, `MINERU_API_BASE_URL`,
   `MINERU_API_KEY`); `MINERU_BACKEND/PARSE_EFFORT/IMAGE_ANALYSIS` are env-only. `MINERU_API_BASE_URL` defaults to
   `http://localhost:8000` so "not configured" is **never empty** — gate on `ENABLE_DOCUMENT_PARSE`.
2. Only `charset-normalizer` (+ `jieba`, `tiktoken`, `langchain-text-splitters`, `markdown-it-py`, `numpy`) are installed. **No**
   openpyxl / pandas / xlrd / pypdf / bs4 / lxml / markdownify / python-docx / pymupdf anywhere in `pyproject.toml`,
   `uv.lock` or `.venv`. xlsx/xls/pdf-text-layer need **new deps**; csv/html/json can use stdlib.
3. **Embedding / rerank** = admin "model cards" (collection `model_configs`, `kind` ∈ chat/embedding/rerank/transcribe).
   Resolve with `LLMClient.get_card_config(model_id, kind="embedding"|"rerank")` → `{api_base, api_key, model}`; then plain
   `httpx` to `{api_base}/v1/embeddings` / `/v1/rerank` (OpenAI/Cohere/Jina style). There is **no batching, no dimension
   discovery, no rate limiting, and no shared embedding helper** — write one. `api_base` `/v1` double-prefix ambiguity (see §2.4).
4. Native memory `$vectorSearch` is wrapped in `try/except: pass` and falls back to Python cosine over the **latest ≤100**
   docs (`scan_limit = min(limit*3, 100)`) — a pattern *not* to copy for the KB (it silently truncates the corpus).
5. Background LLM calls = `LLMClient.get_model(model_id=<setting> or None, temperature=…, max_tokens=…)` then
   `await model.ainvoke([...])` + hand-rolled JSON parsing. **No** `with_structured_output` anywhere in `src/`.
   `model_id=None` → `DEFAULT_MODEL_ID` else *first enabled card of any kind* (no kind filter!).
6. **arq**: one embedded `Worker` (`EmbeddedArqRuntime`) in the FastAPI process, queue `kunxiaozhi:arq`, `max_jobs=64`,
   `job_timeout=86400`, function list `[run_agent_task]`. A second, separate `Worker` for queue `kb` is non-conflicting
   (separate queue name ⇒ separate zset + health-check key) but must be wired into `start_runtime_services()` yourself.
   Pitfalls: arq `in_progress_timeout = job_timeout + 10` (crash ⇒ job-id blocked), `_job_id` dedupe vs `keep_result=3600`, `max_jobs` not hot-reloadable.
7. **WebSocket push**: `await get_connection_manager().send_to_user_with_broadcast(user_id, {"type": "...", "data": {...}})`
   (Redis-routed, multi-replica safe, 256 KiB cap). Frontend `useWebSocket.ts` only dispatches a **closed set** of types
   (`task:complete`, `notification:feedback`, `scheduled_task:run_finished`) — a new `kb:*` type needs a new branch.
8. **There is NO per-user persisted 站内通知/inbox.** The `notifications` collection is admin broadcast announcements only.
   "Notify on share" must be built (persisted record + WS push, or an "unseen shares" flag on the share record).
9. **Tools** = `@tool async def f(..., runtime: Annotated[ToolRuntime, InjectedToolArg] = None) -> str` returning a **JSON string**.
   User id: `get_user_id_from_runtime(runtime)` (`runtime.config["configurable"]["context"].user_id`). Per-message options:
   `runtime.config["configurable"]["agent_options"]`. Register in `build_internal_tools()` (reaches Fast/Search/Team agents
   automatically). **Write the Chinese docstring + `Annotated[...]` arg descriptions in the tool source itself**: runtime-injected tools are *not* localised by `harness_prompt_overrides` (verified: `localize_tool_for_model` returns them unchanged, §6.7).
10. **Frontend only receives a structured tool result if the JSON string ≤ 8000 chars** (`_TOOL_RESULT_DISPLAY_MAX_CHARS`);
    otherwise it is clipped to text and citation cards cannot parse it. Also `{"error": ...}` dicts and strings starting with
    `error:/failed/exception/traceback` are flagged as tool errors.
11. **Per-message KB ids**: `AgentRequest.agent_options` (free-form `dict`, **no key sanitisation**) → `task_context["agent_options"]`
    → `configurable.agent_options`. A client can forge any key ⇒ tools MUST re-check KB access server-side. Frontend type is
    `Record<string, boolean | string | number>` (no arrays!) and the `@` trigger is already used for persona/team mentions.
12. **Confirm-before-write** has two existing patterns: `ask_human` (blocking approval form over SSE, 300 s) and the
    scheduled-task **draft→card→REST confirm** pattern (preferred; non-blocking, TTL'd, frontend card). Both must be excluded from
    unattended scheduled runs and WeCom (see §6.8).

---

## 1. MinerU / document parsing

### 1.1 Where MinerU is called

| Item | Location |
|---|---|
| HTTP adapter | `src/infra/tool/mineru_client.py` — `class MinerUClient` (L32), `class MinerUError(Exception)` (L28), `strip_server_local_image_links()` (L133) |
| Only caller | `src/infra/tool/read_document_tool.py` — `@tool async def read_document(url, runtime)` (L335-475) |
| Registration | `src/infra/tool/internal_registry.py:48-49` — `if settings.ENABLE_DOCUMENT_PARSE: tools.append(get_read_document_tool())` |
| Spec | `.trellis/spec/backend/read-document-dispatch.md` (matches code) |

### 1.2 The conversion function (async, bytes → markdown)

```python
# src/infra/tool/mineru_client.py:40-75
class MinerUClient:
    def __init__(self, base_url: str, api_key: str | None = None, timeout: int = 300,
                 max_retries: int = 3, retry_delay: int = 2, backend: str | None = None,
                 effort: str | None = None, image_analysis: bool | None = None) -> None: ...
    async def parse_bytes(self, filename: str, content: bytes,
                          content_type: str = "application/pdf") -> str:
        """POST bytes to MinerU and return the parsed Markdown.
        Raises MinerUError on network failure, non-2xx, or empty md_content after exhausting retries."""
```

* Wire contract: `POST {base_url}/file_parse`, multipart field `files=(filename, bytes, content_type)`, form data
  `return_md=true, return_content_list=false, backend, effort, image_analysis`; optional `Authorization: Bearer <key>`;
  response `{"results": {<key>: {"md_content": "..."}}}`; only the **first** result's `md_content` is read (L118-124).
* **Always async** (`httpx.AsyncClient` created per attempt, L92). Takes **bytes** only — no path/URL variant. Downloading is the
  caller's job (`_download_to_bytes`, `read_document_tool.py:183-238`, size-capped spooled temp file).
* **Timeouts/retries**: per attempt `timeout=300` s; `max_retries=3`; sleep `retry_delay*attempt` (2 s, 4 s) → worst case ≈ 15 min/file.
  `MinerUError("Empty content from MinerU")` is **not** retried (re-raised at L102-103). Pass a larger `timeout=` to the constructor for big PDFs.
* **Images/figures**: no image bytes are returned. With `image_analysis=true` + `effort="high"` MinerU embeds VLM descriptions inline
  as `<details><summary>image content</summary>…`; host-local `![](images/x.png)` links are stripped (L127-137). Hybrid `effort="medium"`
  silently disables figure analysis — the client always sends `effort` explicitly. So **markdown is text-only; there are no image assets to store**.
* Image inputs (png/jpg/jpeg/webp/gif/bmp/tiff) go through the same endpoint with an image MIME (`_IMAGE_EXTENSIONS`, L72-80).

Pattern to copy (from `read_document_tool.py:430-453`):

```python
client = MinerUClient(base_url=base_url, api_key=getattr(settings, "MINERU_API_KEY", "") or None)
try:
    md_content = await client.parse_bytes(filename, content, content_type)
except MinerUError as exc:  # network/non-2xx/empty after retries
    ...
```

### 1.3 Configuration (admin settings vs env) and "not configured"

* `src/kernel/config/base.py:449-459`: `ENABLE_DOCUMENT_PARSE=False`, `MINERU_API_BASE_URL="http://localhost:8000"`, `MINERU_API_KEY=""`,
  `MINERU_BACKEND="hybrid-engine"`, `MINERU_PARSE_EFFORT="high"`, `MINERU_IMAGE_ANALYSIS=True`,
  `DOCUMENT_PARSE_MAX_BYTES=52428800` (50 MiB), `DOCUMENT_PARSE_MAX_OUTPUT_CHARS=50000`.
* Admin-editable (DB-backed `settings.X`, hot-updated via settings pubsub): `ENABLE_DOCUMENT_PARSE`, `MINERU_API_BASE_URL`,
  `MINERU_API_KEY` (`is_sensitive`), `DOCUMENT_PARSE_MAX_BYTES`, `DOCUMENT_PARSE_MAX_OUTPUT_CHARS` — `src/kernel/config/definitions.py:~799-840`
  (category `SettingCategory.DOCUMENT_PARSE`). `MINERU_BACKEND/PARSE_EFFORT/IMAGE_ANALYSIS` have **no** definition entry ⇒ env-only
  (`grep` confirms; they are read with `getattr(settings, ..., default)` in `MinerUClient.__init__`, L56-62).
* **Not configured behaviour**: `read_document` checks only `if not base_url` (L401-405) → returns `{"error": "MINERU_API_BASE_URL is not configured"}`
  *before* downloading. Because the default is non-empty, that branch is effectively dead; the real switch is `ENABLE_DOCUMENT_PARSE`
  (tool simply isn't registered). **KB recommendation**: treat MinerU as available iff `ENABLE_DOCUMENT_PARSE` and base URL non-empty;
  on `MinerUError`/unavailable, fall back (pdf → text layer) or mark the file `failed` with a user-presentable reason.

### 1.4 Local parsing helpers & dependencies

* Plain text decode: `_decode_text(content: bytes) -> str` (`read_document_tool.py:241-246`):
  ```python
  match = from_bytes(content).best()   # charset_normalizer
  return "" if match is None else str(match)
  ```
  (spec: decode with `str(match)`; short CJK samples can be misdetected, e.g. GBK as cp949; empty bytes ⇒ `"Empty content"`).
  It is called through `run_blocking_io` (CPU-ish, off the loop).
* Existing extension tables (reusable constants): `_MINERU_EXTENSIONS` (pdf/docx/pptx, L56-60), `_PLAIN_TEXT_EXTENSIONS`
  (`.txt .md .log .json .py`, L63-65), `_DATA_EXTENSIONS` (`.xlsx .csv`, L68 — **not parsed**, returns sandbox guidance),
  `_IMAGE_EXTENSIONS` (L72-80), `_classify_file()` (L145), `_content_type_for()` (L158), `_EXTENSION_BY_CONTENT_TYPE` (L89-98).
  xlsx/csv are explicitly *not* parsed today (`_handle_data_file`, L291-332) — the KB needs its own xlsx/csv→markdown.
* `read_document` is **not reusable from a background job**: it needs a `ToolRuntime`, downloads its own server over unauthenticated
  HTTP (`/api/storage/files/{id}/content`) and clips output at 50 000 chars. Use `MinerUClient` + `_decode_text`-style helpers directly on bytes
  read from storage (see §1.6).

**Dependencies already present** (`pyproject.toml` + `uv.lock` + `.venv/lib/python3.12/site-packages`):

| Lib | direct dep? | installed in .venv | notes |
|---|---|---|---|
| `charset-normalizer>=3.4.0` | yes (pyproject L~43) | 3.4.7 | decoding |
| `jieba>=0.42.1` | yes | 0.42.1 | used only for memory tags (`memory/client/native/classification.py:11`, `summaries.py:12`) |
| `tiktoken>=0.9.0` | yes | 0.13.0 | token counting for chunking |
| `langchain-text-splitters` | transitive (langchain) | 1.1.2 | `RecursiveCharacterTextSplitter` etc. available |
| `markdown-it-py` | transitive | 4.2.0 | markdown parsing for header-aware chunking |
| `numpy` | transitive (langchain-mongodb, qdrant-client) | 2.4.6 | |
| `qdrant-client>=1.15.0` | **added in the *uncommitted* working-tree diff** (`pyproject.toml:65`, `uv.lock`); not used anywhere in `src/` yet | 1.19.1 | presumably added by the parent for this feature |
| `httpx`, `aiofiles`, `boto3`, `minio`, `oss2`, `motor`, `pymongo`, `redis`, `arq`, `apscheduler` | yes | yes | |
| **openpyxl, pandas, xlrd, pypdf, pymupdf, pdfplumber, python-docx, python-pptx, bs4, lxml, markdownify, html2text** | **no** | **no** | none in `uv.lock` at all — add explicitly (e.g. `openpyxl` for xlsx, `xlrd` for xls, `pypdf` for pdf text-layer fallback). csv/json/html can use stdlib `csv`, `json`, `html.parser`. |

(`grep -rn "import openpyxl|pandas|bs4|docx|pypdf|fitz|markdownify|lxml|xlrd" src` → no hits.)

### 1.5 Async/blocking convention

Any sync CPU/IO work must go through `src/infra/async_utils/blocking.py::run_blocking_io(func, *args, timeout=None, **kwargs)`
(bounded `ThreadPoolExecutor`, `BLOCKING_IO_MAX_WORKERS=8`, pending cap 16 via `BLOCKING_IO_MAX_PENDING`). All repo code wraps `json.dumps/loads`, file IO and
parsers with it. For KB parsing (openpyxl/pypdf/chunking) use it; note the pool is **shared with the whole app** (8 threads), so heavy CPU parsing
at KB concurrency 4 can starve other blocking calls — consider a dedicated executor or `asyncio.to_thread` with a semaphore. **UNCONFIRMED**: whether
that contention is observable in practice.

### 1.6 Where to keep the original bytes (needed because the job runs without a request)

`src/infra/storage/s3/service.py::S3StorageService` (obtain via `await get_or_init_storage()`, L542) works for **both** S3/MinIO/OSS and a local-disk backend
(`S3_ENABLED=False` → `S3Provider.LOCAL`, `LOCAL_STORAGE_PATH`, default `./uploads`; k8s manifest runs `S3_ENABLED=false`).
Signatures: `upload_bytes(data, folder, filename, content_type=None, metadata=None, *, skip_size_limit=False) -> UploadResult` (L198),
`upload_to_key(data, key, content_type=None, metadata=None, *, skip_size_limit=False)` (L229), `download_file(key) -> bytes` (L358),
`download_to_file`, `download_stream`, `delete_file(key)`, `file_exists`. `download_file` enforces `internal_max_upload_size`.

---

## 2. Embedding and rerank "model cards"

### 2.1 Where admins configure models

* Collection: Mongo `model_configs` (`src/infra/agent/model_storage.py:19` `_COLL_MODELS`), `class ModelStorage` (L34); singleton `get_model_storage()`.
  `api_key` stored **encrypted**, decrypted on read (`_decrypt_doc`). `list_models(include_disabled=False)` (L153, limit 500) — **no `kind` filter**.
* Schema: `src/kernel/schemas/model.py` — `ModelKind = Literal["chat","embedding","rerank","transcribe"]` (L12);
  `ModelConfig` fields `id, value (model name), provider, kind="chat", label, api_key, api_base, temperature, max_tokens, profile, fallback_model, enabled, order` (L28-61).
  Legacy cards without `kind` default to `chat`.
* Admin UI: `frontend/src/components/panels/ModelPanel/tabs/ModelFormModal.tsx` (kind selector). The settings panel renders a card picker filtered by kind via
  `frontend/src/components/panels/SettingsPanel.constants.ts` → `MODEL_CONFIG_SETTING_KEYS` (L~46-55) and `MODEL_CARD_KIND_FILTER` (L57-63).
* Settings that point at cards (all empty-string-allowed in `src/kernel/config/service.py:20-31 _ALLOW_EMPTY_STRING_SETTINGS`):
  `NATIVE_MEMORY_EMBEDDING_MODEL_ID` (base.py:416; def `_definitions_extra.py:777`, category `MEMORY_EMBEDDING`),
  `NATIVE_MEMORY_RERANK_MODEL_ID` (base.py:423; def `_definitions_extra.py:806`), `DIFY_KB_RERANK_MODEL_ID` (base.py:472; def `_definitions_extra.py:1018`),
  `SESSION_TITLE_MODEL_ID` (base.py:156), `NATIVE_MEMORY_MODEL_ID`, `NATIVE_MEMORY_COMPACTION_MODEL_ID`, `AUDIO_TRANSCRIPTION_MODEL_ID`, `DEFAULT_MODEL_ID` (base.py:354).
* **To add `KB_EMBEDDING_MODEL_ID` / `KB_RERANK_MODEL_ID` / `KB_LLM_MODEL_ID` you touch**: `src/kernel/config/base.py` (attr),
  `_definitions_extra.py` or `definitions.py` (entry; `"type": SettingType.STRING`, `"category"`, `"subcategory"`, `"description": "settingDesc.<KEY>"`, `"default": ""`, `"frontend_visible": True`),
  `service.py::_ALLOW_EMPTY_STRING_SETTINGS`, `SettingCategory` enum (`src/kernel/schemas/setting.py:29-57`, only if a new category), and on the frontend
  `SettingsPanel.constants.ts` (`CATEGORY_ORDER`, `MODEL_CONFIG_SETTING_KEYS`, `MODEL_CARD_KIND_FILTER`), `SettingsPanel.tsx` category label map (L70-95),
  `frontend/src/types/settings.ts` (category union L38-39), plus i18n `settingDesc.*` / `categories.*`.

### 2.2 Resolving the active card

```python
# src/infra/llm/client.py:589-631  (static, async)
card = await LLMClient.get_card_config(settings.NATIVE_MEMORY_EMBEDDING_MODEL_ID or None, kind="embedding")
# -> {"api_base": str|None, "api_key": str|None, "model": <card.value>}  or None
#    None when: empty id, card missing, kind mismatch (warning logged), storage error
```

It deliberately does **not** build a chat model. Tests: `tests/infra/llm/test_card_config.py`.
Callers must check `card and card.get("api_base") and card.get("api_key")` (memory does at `backend.py:534`; rerank also requires `model`, `search.py:369`).

### 2.3 Batch embedding — what exists

Only a single-text closure (`src/infra/memory/client/native/backend.py:523-566`):

```python
client = httpx.AsyncClient(base_url=api_base.rstrip("/"),
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    timeout=httpx.Timeout(30.0))
async def embed_fn(text: str) -> list[float]:
    resp = await client.post("/v1/embeddings", json={"input": text, "model": model})
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
```

* Created **once** and cached on the backend; `_maybe_embed` (L461-471) wraps it in `run_blocking_io(self._embedding_fn, text)` and awaits a coroutine result
  (works because an un-awaited coroutine object is returned from the thread — odd but harmless; do not copy).
  Failure is swallowed (`return None` → text-only mode).
* **No batching** (`"input"` is a str), **no retries**, **no concurrency/rate limiting**, **no dimension discovery/validation**, `text-embedding-3-small` default model name if card `value` empty.
* The endpoint is OpenAI-compatible: `POST /v1/embeddings {"input": str | list[str], "model": ...}` → `{"data":[{"index":i,"embedding":[...]}, ...]}`.
  For the KB send `input` as a list (sort by `index`), cap batch size/tokens (provider-dependent; **UNCONFIRMED** what the admin's provider allows — make it a setting, e.g. 16-64 items),
  retry 429/5xx with backoff, and bound global concurrency (Redis ZSET slots, see §4.6).
* **Dimension discovery**: none in the repo. Probe once (`len(embed(["dimension probe"])[0])`), store `{embedding_model_id, model, dim}` on the KB doc, and refuse to mix
  models within a collection (re-embed/rebuild when the admin changes the card).
* **`api_base` convention is ambiguous (UNCONFIRMED which one admins use)**: code does `base_url=api_base` + `post("/v1/embeddings")`; httpx merges to `…/v1/v1/embeddings` if the
  card already ends in `/v1` (and the existing unit test fixture literally uses `"https://api.example.com/v1"`, `tests/infra/memory/native/test_model_card_consumers.py:70-117`).
  The UI placeholder is a bare host (`"e.g., https://api.anthropic.com"`, `en.json:169`). Normalise in the new helper: strip a trailing `/v1` (or `/v1/embeddings`) and always post to `/v1/embeddings`.

### 2.4 Rerank call + response shape

```python
# src/infra/memory/client/native/search.py:361-430 (same shape in dify_kb_tool.py:241-292)
card = await LLMClient.get_card_config(settings.NATIVE_MEMORY_RERANK_MODEL_ID or None, kind="rerank")
async with httpx.AsyncClient(base_url=card["api_base"].rstrip("/"),
        headers={"Authorization": f"Bearer {card['api_key']}", "Content-Type": "application/json"},
        timeout=httpx.Timeout(15.0)) as client:
    r = await client.post("/v1/rerank", json={"model": card["model"], "query": query,
                                             "documents": [str, ...], "top_n": max_results})
    r.raise_for_status()
results = r.json().get("results")   # [{"index": int, "relevance_score": float}, ...]  (already sorted desc)
```

Defensive parsing to copy: skip non-dict items, `idx` must be int in range and unique, copy candidate dict and overwrite `score` with `relevance_score`; any failure/empty ⇒ **degrade to
the original order** (`local_rerank` heuristic in memory, `segments[:top_n]` in Dify). Rerank is skipped when `len(candidates) <= top_n` (Dify) / `<=1` (memory). Timeout 15 s (memory) / 20 s (Dify).

### 2.5 The existing native memory vector search ($vectorSearch + Python cosine fallback)

`src/infra/memory/client/native/search.py:252-313` (`vector_search`):

```python
query_vec = await backend._maybe_embed(query)
base = {"user_id": user_id, "source": {"$ne": "session_summary"}, "embedding": {"$exists": True, "$ne": None}}
try:
    pipeline = [{"$vectorSearch": {"index": "native_mem_vector_idx", "path": "embedding",
                                   "queryVector": query_vec, "numCandidates": limit * 5, "limit": limit}},
                {"$match": base}]                       # NB: $match AFTER $vectorSearch (post-filter)
    docs = await backend._collection.aggregate(pipeline).to_list(length=limit)
    return [format_memory(doc, doc.get("score", 1.0)) for doc in docs]
except Exception:
    pass                                                # community Mongo has no $vectorSearch → always lands here
scan_limit = min(limit * 3, 100)                        # <-- only the 100 most-recently-updated docs are scored
docs = await backend._collection.find(base, projection).sort("updated_at", -1).limit(scan_limit).to_list(length=scan_limit)
scored = [(cosine_similarity(query_vec, d["embedding"]), d) for d in docs if d.get("embedding")]
scored.sort(key=lambda x: x[0], reverse=True)
```

* `cosine_similarity` = pure-Python (`memory/client/native/models.py:38-44`, no numpy).
* The `native_mem_vector_idx` Atlas index is **never created** by `_create_indexes_sync` (backend.py:486-521) — only B-tree + `$text` (weights content 10 / summary 5 / tags 2) indexes. So on the
  deployed community Mongo this path is always the fallback.
* Hybrid composition (`recall_memories`, L452-504): `text_search` (Mongo `$text`, falls back to `keyword_fallback` regex clauses via `build_keyword_clauses`) ‖ `vector_search` → `rrf_merge(k=60)` →
  optional `rerank_candidates` → min-score filter (`NATIVE_MEMORY_RECALL_MIN_SCORE`, default 0.3). `rrf_merge` (L433-450) is a clean ~15-line reusable RRF.
* Mongo `$text` does **not** segment Chinese (whitespace/punctuation tokenisation only). Existing code works around with `char_ngrams`/`has_cjk` (`models.py:25-36`) and regex keyword fallback.
  For KB keyword leg: pre-segment with `jieba` into a space-joined `tokens` field and put the `$text` index on that field (`default_language="none"`). *(Recommendation, not existing code.)*

### 2.6 Vector store availability

No vector DB client exists in `src/` (`grep -rn qdrant src tests` → nothing). Qdrant was only just added to `pyproject.toml`/`uv.lock` (uncommitted). `numpy` is available.

---

## 3. LLM access for background jobs

### 3.1 The pattern (no request/agent context needed)

```python
# src/infra/memory/client/native/backend.py:132-147
from src.infra.llm.client import LLMClient
model_id = getattr(settings, "NATIVE_MEMORY_MODEL_ID", "") or None
return await LLMClient.get_model(model_id=model_id, temperature=0.1, max_tokens=max_tokens)
```

* `LLMClient.get_model(model=None, model_id=None, temperature=0.7, max_tokens=None, api_key=None, api_base=None, thinking=None, profile=None, model_config=None, use_model_config=True, **kwargs) -> BaseChatModel`
  (`src/infra/llm/client.py:364`); LRU-cached instances (`LLM_MODEL_CACHE_SIZE`, default 50). `model_id` = **card UUID**; unknown/disabled → `AuthorizationError("model_not_found"/"model_disabled")`;
  missing key → `AuthorizationError("model_api_key_missing")`. Extra kwargs (e.g. `max_retries=settings.LLM_MAX_RETRIES`) pass through to the provider model (used in `recommendations.py:490`, `routes/session.py:744-751`).
* **Default-model fallback** (`model_id=None`, `model=None`): `models_service.get_default_model()` → `select_default_model(models)` → `DEFAULT_MODEL_ID` if it is among enabled cards, else the **first enabled card of any kind**
  (`src/infra/llm/models_service.py:72-100`; no `kind` filter in `list_models`/`_fetch_from_db`). ⇒ always give the KB graph/summary jobs an **explicit chat-kind card setting**, or filter by `kind=="chat"` yourself.
* Model-card consumers in background: title (`SESSION_TITLE_MODEL_ID`, `routes/session.py:744`), follow-up questions (`agents/core/recommendations.py:490`), memory enrichment/extraction (`NATIVE_MEMORY_MODEL_ID`),
  memory compaction (`NATIVE_MEMORY_COMPACTION_MODEL_ID`, `memory/compaction_agent.py:527`).

### 3.2 Calling it / JSON output

No structured-output API is used (`grep with_structured_output src` → nothing). Pattern is plain `ainvoke` + tolerant parse:

```python
# src/infra/memory/client/native/summaries.py:67-100 (abridged)
response = await model.ainvoke([SystemMessage(content=_ENRICH_SYSTEM), HumanMessage(content=...)])
text = response.content
if isinstance(text, list):                       # Anthropic-style blocks
    text = next((i["text"] for i in text if isinstance(i, dict) and i.get("type") == "text"), "")
text = str(text).strip().strip("```json").strip("```").strip()   # char-strip hack
data = await run_blocking_io(json.loads, text)
```

Better tolerant parsers already exist: `agents/core/recommendations.py::_extract_text` (L404) and `_parse_questions` (L413-448: fence strip, `json.loads`, then bracket-slice `find/rfind` retry).
Retry helper: `_ainvoke_with_retry(model, prompt, max_retries=None)` exists **twice, privately** (`recommendations.py:451`, `routes/session.py:75`; exponential `settings.LLM_RETRY_DELAY * 2**attempt`, `LLM_MAX_RETRIES`=3, `LLM_RETRY_DELAY`=1.0 at base.py:124-125). Copy, don't import privates.
For graph triple extraction you can try `model.with_structured_output(PydanticModel)` (langchain-openai/anthropic support it) but **UNCONFIRMED** against the admin's providers (esp. OpenAI-compatible third parties); keep the JSON-parse fallback.

---

## 4. arq / background jobs

### 4.1 Worker and queue configuration (existing)

| Item | Value / location |
|---|---|
| Settings | `src/kernel/config/base.py:174-178`: `TASK_BACKEND="arq"` (`local\|arq`), `ARQ_EMBEDDED_WORKER=True`, `ARQ_QUEUE_NAME="kunxiaozhi:arq"`, `ARQ_WORKER_MAX_JOBS=64`, `ARQ_JOB_TIMEOUT_SECONDS=86400` |
| Worker lifecycle | `src/infra/task/arq_runtime.py` — `EmbeddedArqRuntime` (L16-85), `get_arq_runtime()`, `start_arq_runtime()` (L87), `stop_arq_runtime()` |
| Job function | `src/infra/task/arq_worker.py::run_agent_task(ctx, run_id)` (L67); `WorkerSettings.functions = [run_agent_task]` (L148, **no** `redis_settings`/`queue_name` ⇒ not runnable via `arq` CLI; **no standalone worker mode exists today**) |
| Redis settings | `src/infra/task/arq_settings.py::build_arq_redis_settings(settings) -> arq.RedisSettings` (L33-61; Sentinel aware) |
| Enqueue | `src/infra/task/manager.py:90-96` `_get_arq_pool()` → `create_pool(build_arq_redis_settings(settings), default_queue_name=settings.ARQ_QUEUE_NAME)`; enqueue at L458: `await arq_pool.enqueue_job("run_agent_task", run_id, _job_id=run_id)` |
| Payload store | jobs carry only a `run_id`; real payload lives in Redis `task:arq:payload:{run_id}` (24 h TTL, 2 MiB max) — `arq_payloads.py` |

```python
# src/infra/task/arq_runtime.py:28-50  — the embedded worker (copy this shape for the KB worker)
self._worker = self._worker_factory([run_agent_task], queue_name=settings.ARQ_QUEUE_NAME,
    redis_settings=build_arq_redis_settings(settings), handle_signals=False,
    max_jobs=settings.ARQ_WORKER_MAX_JOBS, job_timeout=settings.ARQ_JOB_TIMEOUT_SECONDS,
    ctx={"payload_store": TaskArqPayloadStore()}, allow_abort_jobs=True)
self._task = asyncio.create_task(self._worker.async_run())
```
`start()` early-returns unless `TASK_BACKEND == "arq"` **and** `ARQ_EMBEDDED_WORKER` (L33-36). `stop()` calls `worker.close()` (guarding the Windows `SIGUSR1` AttributeError) then cancels the task.

### 4.2 In-process or separate? — in-process

Single uvicorn process (`main.py`: `uvicorn.run("src.api.main:app", …)`, no `workers=`), k8s `replicas: 1` for the app, env `TASK_BACKEND=arq`, `ARQ_EMBEDDED_WORKER=true`.
Lifespan (`src/api/main.py:455`) → `await start_runtime_services()` (main.py:519) → `src/infra/runtime_services.py:85-113` which calls `await start_arq_runtime()` (L88), starts pubsub listeners,
`get_runtime_scheduler().start()` (L113). `stop_runtime_services()` mirrors it. **Add the KB worker next to `start_arq_runtime()`/`stop_arq_runtime()` there.**

### 4.3 arq 0.28.0 semantics that matter (verified in `.venv/.../arq/worker.py`, `connections.py`)

* `Worker(functions, queue_name=…, redis_settings=…, max_jobs=10, job_timeout=300, keep_result=3600, max_tries=5, poll_delay=0.5, health_check_key=None (=queue_name+":health-check"), allow_abort_jobs=False, retry_jobs=True, handle_signals=True)`.
  Separate `queue_name` ⇒ separate ZSET + health key ⇒ **no cross-talk with `run_agent_task`**. Never enqueue KB jobs on the default queue (the agent worker would fail "function not found").
* `ArqRedis.enqueue_job(function: str, *args, _job_id=None, _queue_name=None, _defer_until=None, _defer_by=None, _expires=None, _job_try=None, **kwargs) -> Job | None`
  (`connections.py:119-144`). Returns **`None` if the job id already exists** (queued, running, or result still kept). Use `_queue_name="kb"`-style routing or a dedicated pool created with `default_queue_name=`.
* **Crash/durability**: `in_progress_timeout_s = max(job timeouts) + 10` (`worker.py:277`) and the in-progress key is set with that TTL (L465). The existing worker has `job_timeout=86400` ⇒ after a hard crash the same `_job_id` is blocked for ~24 h.
  For `kb`, use a much smaller `job_timeout` (per-file budget, e.g. 1800-3600 s) and **do not use arq as the source of truth** — keep the Mongo task table with lease + heartbeat + reconcile (your plan), and use fresh `_job_id`s (or `keep_result=0`) when re-enqueueing.
* `max_jobs` is fixed at construction (**no hot reload**). For an admin-configurable concurrency either restart-required, or construct with a larger `max_jobs` and gate inside the job with a Redis ZSET slot (below) re-read per job.
* Graceful shutdown cancels running jobs → arq re-queues cancelled jobs (`retry_jobs=True`, `CancelledError` ⇒ retry) — idempotent job bodies required.

### 4.4 Redis client factory (incl. Sentinel)

`src/infra/storage/redis.py`: `get_redis_client() -> redis.asyncio.Redis` (L197, shared pool, `decode_responses=True`, `max_connections=50`, `socket_timeout=10`),
`create_redis_client(*, isolated_pool=False, socket_timeout=…)` (L182), `sentinel_enabled()/parse_sentinel_hosts()/redis_db_index()/redis_node_password()`.
Spec (`redis-sentinel.md`): **all** clients via these factories; arq via `build_arq_redis_settings`. Results are `str` (decoded), not bytes (the payload store defensively handles bytes).

### 4.5 Locks, leases, run slots already implemented (copy, don't reinvent)

* `src/infra/scheduler/locks.py` (spec `scheduled-tasks.md`): token-checked Lua release `_RELEASE_LOCK_LUA`; `claim_schedule_slot(task_id, slot_id, ttl)` (`SET NX EX`, never released);
  `acquire_task_lock(task_id, run_id, ttl) -> token|None` / `release_task_lock(task_id, token)`; **global concurrency ZSET** `acquire_run_slot(member, limit, ttl) -> bool` / `release_run_slot(member)`
  (Lua: purge expired by score, idempotent re-acquire, `ZCARD < limit`) — key hard-coded `RUN_SLOTS_KEY="scheduler:running"`; copy with a `kb:running` key for "max 4 concurrent KB tasks cluster-wide".
  Runner usage: `scheduler/runner.py:196-225` (slot claim → task lock → record) and `_wait_for_run_slot` (L~316-340; limit re-read each loop; Redis failure **fails open**).
* `src/infra/task/startup_cleanup.py`: Redis lease with renewal (`STARTUP_CLEANUP_LEASE_KEY`, `_acquire_startup_cleanup_lease` L197, `_renew…` L226 Lua `expire`, `_release…` L258) — template for a KB reconcile leader lease.
* `src/infra/memory/distributed.py`: simple `SET NX EX` locks (`acquire_consolidation_lock`, `acquire_auto_capture_lock`) + Redis pubsub invalidation via `get_pubsub_hub()`.
* Mongo atomic claim pattern: `scheduler/drafts.py::ScheduledTaskDraftStorage.transition()` (L119-136) — `find_one_and_update({_id, status: from}, {$set: {status: to}}, return_document=AFTER)`; TTL cleanup via `create_index("purge_at", expireAfterSeconds=0)` (L109-111).

### 4.6 Periodic jobs (reconcile / stuck-task sweep)

`src/infra/scheduler/runtime.py` — `RuntimeScheduler` (APScheduler facade) via `get_runtime_scheduler()`; `ScheduledJob(id, handler, interval_seconds | trigger, enabled, name, max_instances=1, misfire_grace_time, coalesce=True, run_on_start=False)` (L26-44).
Usage template: `src/infra/memory/tools.py:409-417` — `get_runtime_scheduler().register_interval_job(ScheduledJob(id="memory.compaction", interval_seconds=<callable>, enabled=<lambda>, handler=<async fn>))`.
Runs on **every replica** → guard handler with a Redis lease/lock. `interval_seconds`/`enabled` may be callables (re-evaluated ⇒ hot-reloadable).

### 4.7 Startup index/collection initialisation

`src/api/main.py::_startup_index_initializers()` (L346-426) returns `[(name, async init fn)]`, run with `asyncio.gather` before serving (L438-451). Add a `("kb_storage", _init_kb_storage)` entry; storage classes use a lazy collection
property `get_mongo_client()[settings.MONGODB_DB][NAME]` (`get_mongo_client()` = cached Motor `AsyncIOMotorClient`, `src/infra/storage/mongodb.py:31`) + an `ensure_indexes()` coroutine.
Routers are registered in `create_app()` (`main.py:808-876`, e.g. `app.include_router(scheduled_task.router, prefix="/api/scheduled-tasks", …)`).

### 4.8 Conflicts / things to decide for "Mongo task table + dedicated arq queue `kb`, in-process, concurrency 4"

* No hard conflict. Needs: new settings (`KB_ARQ_QUEUE_NAME`, `KB_WORKER_MAX_JOBS`, `KB_JOB_TIMEOUT_SECONDS`), a `KbArqRuntime` (or extend `EmbeddedArqRuntime` to a list of workers), hook into `runtime_services.py`.
* `EmbeddedArqRuntime.start()` is gated on `TASK_BACKEND == "arq"`; the KB runtime should **not** depend on `TASK_BACKEND` (agent chat can be `local` while the KB still needs its queue) but does need Redis (same as the whole app).
* Don't reuse `run_agent_task`'s payload store; keep payloads in Mongo (task docs) and enqueue only `task_id`.
* The shared 8-thread `run_blocking_io` pool (§1.5) and the single event loop are shared with chat; heavy per-file CPU work (chunking, xlsx parse) should be offloaded and bounded.
* `uvicorn` runs 1 process; if ever scaled to N processes/replicas each embeds a worker ⇒ concurrency is `N×4` unless you add the cluster-wide Redis slot above.

---

## 5. WebSocket push and "站内通知"

### 5.1 Server-side push API

```python
# src/infra/websocket.py  — ConnectionManager (L95), singleton get_connection_manager() (L428)
from src.infra.websocket import get_connection_manager
delivered = await get_connection_manager().send_to_user_with_broadcast(
    user_id,                       # internal user id (UserInDB.id / TokenPayload.sub), NOT username
    {"type": "kb:file_status", "data": {...}},   # envelope: {"type": str, "data": dict}
)  # -> int: number of instance channels published to (falls back to local send if Redis route unknown)
```

* Mechanism (`send_to_user_with_broadcast`, L308-349): scans Redis `ws:route:{user_id}:*` (60 s TTL, refreshed every 20 s per connected instance), publishes JSON to `ws:deliver:{instance_id}` channels;
  if nothing published (race/Redis hiccup) falls back to `_send_to_user_local`. Safe to call from the arq worker (same process) or any replica. Messages > `WS_MESSAGE_MAX_BYTES = 256 KiB` are dropped with a warning (L28, L47-71).
  Returns 0 when the user is offline — **fire-and-forget, nothing persisted**.
* To push to "all sessions of a user" — that is the default (all sockets of `user_id`). To push to many users, loop (no multi-user API). Errors should be swallowed (`try/except … logger.warning`) like all existing call sites:
  `scheduler/runner.py:515-541` (`WS_EVENT_TYPE = "scheduled_task:run_finished"`, L46), `task/executor.py:471` (`task:complete`, includes `unread_count` etc.), `notification/feedback_notifier.py:128-150` (`notification:feedback`, resolves **username → user** via `UserStorage().get_by_username(username)` then pushes to `user.id`).
* Endpoint: `src/api/routes/websocket.py` `@router.websocket("/ws")` — auth via `?token=`/first message `{"type":"auth","token":…}`, server replies `{"type":"auth:ok"}`; 60 s idle receive loop re-checks session & credential version; per-IP failure rate limiter. Client messages are ignored (heartbeat only).

### 5.2 Frontend subscription (closed set — must be extended)

`frontend/src/hooks/useWebSocket.ts`: single shared socket; `ws.onmessage` (L~165-195) dispatches by `message.type` to option callbacks:

```ts
if (message.type === "task:complete" && onTaskCompleteRef.current) ...
if (message.type === "notification:feedback" && onFeedbackNotificationRef.current) ...
if (message.type === "scheduled_task:run_finished" && onScheduledTaskFinishedRef.current) ...
```
Options type `UseWebSocketOptions` (L38-45) lists exactly those three callbacks. Wiring: `frontend/src/components/layout/AppContent/useWebSocketNotifications.tsx:44` (`useWebSocket({ enabled, onScheduledTaskFinished, onFeedbackNotification, onTaskComplete })`).
Decoupling pattern for list refresh: `frontend/src/utils/scheduledTaskEvents.ts` (`dispatchScheduledTaskRefresh()` → `window` `Event("scheduled-task:refresh")`, `subscribeScheduledTaskRefresh(handler)` in `ScheduledTaskPanel/index.tsx:68`). **For KB**: add `onKbEvent?: (msg) => void` + `message.type.startsWith("kb:")` branch, and a `kbEvents.ts` custom-event bus the KB pages subscribe to (poll as the fallback; reconnect backoff 1 s→30 s is built in).
UI toast/OS notification side-effects go through `appNotificationService.notify({type, title, body, route, dedupeKey})` + `react-hot-toast`.

### 5.3 站内通知 — there is no per-user inbox

* `src/infra/notification/{storage,manager}.py` + `src/api/routes/notification.py` (`/api/notifications`: `GET /active`, admin CRUD, `POST /{id}/dismiss`) = **admin-authored global announcements**
  (collection `notifications` + `notification_dismissals`; schema `src/kernel/schemas/notification.py`: `NotificationCreate{title_i18n, content_i18n (en/zh/ja/ko/ru), type: info|success|warning|maintenance, start_time, end_time, is_active, popup}`).
  `NotificationManager.create(data: NotificationCreate, user_id: str)` records `created_by`; there is **no recipient/target field**, so it cannot address one user. (Spec: `announcement-login-popup.md`.)
* Real per-user delivery today = WS push (above) and WeCom push; both non-persistent.
* **For share-to-工号**: persist the share row (`kb_shares{kb_id, grantee_user_id, role, shared_by, created_at, seen_at}`), surface it in the grantee's "共享给我" list with an unseen badge (poll-safe), and *additionally* best-effort WS push `{"type":"kb:shared","data":{kb_id,name,from,role}}`. Extending the `notifications` collection with `target_user_ids` is possible but would touch the active/popup aggregation pipelines (`storage.py:254-300`) — not recommended.
* 工号 = `username`: `UserStorage().get_by_username(username) -> UserInDB | None` (`src/infra/user/storage.py:256`; exact, case-sensitive match; does **not** check `is_active`). `TokenPayload{sub (user_id), username, roles, permissions, sid, credential_version}` (`src/kernel/schemas/user.py:~107`).
  There is no user-search endpoint for ordinary users (confirmed by listing `src/api/routes`; `user.py` is admin-oriented — **UNCONFIRMED** at endpoint level, I did not read every route).

---

## 6. Agent tools

### 6.1 Defining tools

Dominant style: LangChain `@tool` on an `async def` returning **`str` (JSON)**, with injected `runtime`:

```python
# src/infra/tool/dify_kb_tool.py:374-387, 488-495  (same in read_document_tool.py:335-344, scheduled_task_tool.py:218-221, memory/tools.py:208-216)
from langchain.tools import tool, ToolRuntime
from langchain_core.tools import BaseTool, InjectedToolArg

@tool
async def query_dify_knowledge_base(
    query: Annotated[str, "The user's original query (no modification applied)."],
    dataset_id: Annotated[str, "The ID of the knowledge base to query."],
    top_k: Annotated[int | None, "..."] = None,
    runtime: Annotated[ToolRuntime, InjectedToolArg] = None,  # type: ignore[assignment]
) -> str:
    """docstring = model-visible description (overridden by the Chinese catalog, §6.7)"""
def get_query_dify_knowledge_base_tool() -> BaseTool: return query_dify_knowledge_base
```
`BaseTool` subclasses are used only for `AskHumanTool` (`human_tool/tool.py:24`, class attrs + `args_schema`) and the `MCPToolWithRetry` wrapper. Use `@tool`.
Convention in every tool: wrap the body in `try/except Exception` and **return** `{"error": ...}`/`{"success": False, ...}` JSON — never raise (`MCPToolWithRetry` retries raises). JSON dumped via `await run_blocking_io(json.dumps, data, ensure_ascii=False)`.

### 6.2 Where tool lists are assembled (per agent type)

* `src/infra/tool/internal_registry.py::build_internal_tools(*, include_sandbox_tools=None) -> list[BaseTool]` (L36-80) — feature-flag gated: `ENABLE_IMAGE_GENERATION`, `ENABLE_AUDIO_TRANSCRIPTION`, `ENABLE_DOCUMENT_PARSE`,
  Dify (`DIFY_KB_ENABLED and DIFY_KB_BASE_URL and DIFY_KB_API_KEY and DIFY_KB_RERANK_MODEL_ID`, L54-62), sandbox-only tools, persona/team tools, scheduled tasks (`SCHEDULED_TASK_ENABLED and SCHEDULED_TASK_CHAT_TOOL_ENABLED`).
  Virtual MCP server name `kunxiaozhi_internal` (`INTERNAL_MCP_SERVER_NAME`, L33).
* `get_internal_tools_for_user(*, user_id, user_roles, is_admin, include_sandbox_tools=None)` (L194-228): loads admin **tool policies** from `MCPStorage().list_tool_policies("kunxiaozhi_internal")`, drops tools the role isn't allowed to use
  (`_is_tool_allowed`: admins always pass; non-admins blocked by `disabled` or `allowed_roles` mismatch), wraps each in `MCPToolWithRetry(tool, user_id=…, server_name=…, user_roles=…, is_admin=…, role_quotas=policy.role_quotas, quota_tool_name=tool.name)`.
  ⇒ **admin UI (/mcp → internal server) automatically lists/gates/quotas any tool you add to `build_internal_tools()`** via `get_internal_tool_infos()` (L231-260; params extracted from the schema, `runtime` omitted).
* Callers: `FastAgentContext.setup()` (`src/agents/fast_agent/context.py:188`), `SearchAgentContext.setup()` (`src/agents/search_agent/context.py:205`). `TeamAgentContext(FastAgentContext)` (`team_agent/context.py:26`) inherits Fast's tools then prunes
  `TEAM_ROUTER_EXCLUDED_TOOLS` (L11-24); team role subagents get explicit `role_tools` derived from the filtered list (`team_agent/nodes.py:~418-523`). Memory tools are appended **separately** per context
  (`fast_agent/context.py:204`, `search_agent/context.py:221`: `if settings.ENABLE_MEMORY: get_all_memory_tools()`), not via the internal registry — a KB-tools block could follow either style; the internal registry gives you admin policy/quota UI for free.
* Final per-request filtering: `ctx.filter_tools()` → `filter_disabled_tools(tools, disabled_tools, disabled_mcp_tools)` (`src/agents/core/tool_filter.py:23`; `BUILTIN_TOOLS = {ask_human, reveal_file, reveal_project, transfer_file}` never filtered). The user's per-conversation tool toggles arrive as `disabled_tools`.
* Agent graph: `create_deep_agent(model, system_prompt, backend, tools=filtered_tools, middleware=user_middleware, subagents=…)` (`fast_agent/nodes.py:258-267`); `search_agent/nodes.py:332`; `team_agent/nodes.py:630`.

### 6.3 How a tool reads user / session / options

```python
# src/infra/tool/backend_utils.py:17-32  (identical copy: memory/client/base.py:198-212)
def get_user_id_from_runtime(runtime) -> Optional[str]:
    return runtime.config["configurable"]["context"].user_id      # context = FastAgentContext(session_id, agent_id, user_id, …)
# session id: scheduled_task_tool.py:97-102 → getattr(runtime.config["configurable"]["context"], "session_id", None)
# per-message options: dify_kb_tool.py:87-109 → runtime.config["configurable"]["agent_options"]  (dict | None)
# base url: backend_utils.get_base_url_from_runtime(runtime) → configurable["base_url"]
# sandbox backend: backend_utils.get_backend_from_runtime(runtime)
```
`runtime.config["configurable"]` is built in `fast_agent/nodes.py:272-285` (`thread_id, backend, context, disabled_skills, enabled_skills, base_url, agent_options, presenter`); same shape in search/team nodes.
Context-var alternative (no runtime needed, e.g. inside helper code): `TraceContext.get_request_context()` → `RequestContext{request_id, session_id, run_id, user_id, trace_id, unattended}` (`src/infra/logging/context.py:42-52, 190+`; set by `task/executor.py:125` and `agents/core/base.py:359`).
Test pattern: `types.SimpleNamespace(config={"configurable": {"agent_options": {...}, "context": SimpleNamespace(user_id="u1", session_id="s1")}})` (`tests/infra/tool/test_dify_kb_new_tools.py:20-24`).
**UNCONFIRMED**: that subagents spawned via deepagents' `task` tool still see `agent_options`/`context` in `runtime.config` (LangGraph config inheritance suggests yes; not verified).

### 6.4 Tool output structure

* Always a **string** (JSON) — `MCPToolWithRetry` returns `Any` but all internal tools return str; no `content_and_artifact`/`ToolMessage.artifact` usage anywhere (`artifact` is only read as a fallback in `agent/events/tool_outputs.py:84-86`).
* Event path (`src/infra/agent/events/tool_events.py::_handle_tool_end` L114-160): raw string → if it starts with `{`/`[` **and `len ≤ 8000`** (`_TOOL_RESULT_DISPLAY_MAX_CHARS`, L20-21) it is JSON-parsed and emitted to the frontend as a structured `dict`; otherwise clipped to 8000 chars + `[truncated from N chars for display]` and sent as text.
  The model always receives the full `ToolMessage.content`. Compaction in `tool_outputs.py` (strings > 2000 chars, > 100 list items, depth 8) applies to non-str content only.
* Error detection (`tool_outputs.py::detect_tool_error`): dict with truthy `"error"` or `status=="error"`, or text whose first 200 chars start (case-insens.) with `error:`, `validationerror`, `[mcp tool error]`, `failed`, `command failed`, `exception`, `traceback` (`events/types.py:11-21`) ⇒ tool marked failed in the UI. Don't start a normal success text with "failed…".
* ⇒ **Design the `query_kb` JSON to stay ≤ 8000 chars** (cap `top_k`, truncate snippets e.g. 300-400 chars each; keep full chunk text retrievable via `open_kb_document`/line ranges) so the frontend can render citation cards from the structured result. Pattern for "frontend-recognisable result": include `"entity_type": "kb_search"` (see `scheduled_task_draft`/`persona_preset` below).
* Existing Dify tools output: `list_dify_knowledge_bases` → `{"success": true, "knowledge_bases": [{id, name, description}]}`; `query_dify_knowledge_base` → `{"success": true, "query", "dataset_id", "records": [{dataset_id, segment_id, document_id, document_name, content, position, score}]}` (dify_kb_tool.py:299-485).

### 6.5 How the existing Dify KB tool works end-to-end

1. Admin settings `DIFY_KB_*` (base.py:~461-480; defs `_definitions_extra.py:~990-1100`) incl. `DIFY_KB_DEFAULT_DATASET_IDS`, `DIFY_KB_RERANK_MODEL_ID`, `DIFY_KB_TOP_K`, `DIFY_KB_RERANK_TOP_K`, `DIFY_KB_SCORE_THRESHOLD`.
2. Persona presets store `dify_kb_dataset_ids` (`kernel/schemas/persona_preset.py:105,145,181,208`).
3. Chat submit (`src/api/routes/chat.py:415-424`) calls `apply_dify_kb_dataset_ids_to_agent_options(request.agent_options, persona_snapshot=…)` (`src/infra/persona_preset/dify_kb_agent_options.py:32-52`): persona ids else `DIFY_KB_DEFAULT_DATASET_IDS`; sets `agent_options["dify_kb_dataset_ids"]` only when non-empty. Same helper is called by WeCom (`infra/agent/wecom/handler.py:885-888`) and the scheduler (`infra/scheduler/runner.py:358-389`) — "every channel must call it" (spec `persona-runtime-and-dify-kb.md`).
4. `agent_options` rides in `task_context` → arq payload → `run_task(agent_options=…)` → agent `kwargs["agent_options"]` → `config["configurable"]["agent_options"]` (graph.py:151,175 → nodes.py:279-281).
5. Tools read ids from `runtime.config["configurable"]["agent_options"]["dify_kb_dataset_ids"]` and enforce scope: empty scope ⇒ `{"success": True, "knowledge_bases": [], "reason": "..."}`; unknown `dataset_id` ⇒ `{"success": False, "error": "Invalid dataset_id …", "records": []}` without calling Dify.
6. Retrieval: per-dataset call to Dify `/retrieve` (query clipped to 250 chars, no rewriting), then external `/v1/rerank` via card, degrade to `pool[:top_n]` on any failure; result returned as JSON string.
7. **Citations/sources are NOT surfaced specially**: `grep -ril "dify\|knowledge_base\|citation" frontend/src/components/chat` → only `ChatMessage/executionProcess.ts` (generic tool-kind icon matching). Results render in the generic `ToolCallItem`. No citation-card machinery exists — build it
   (pattern: `MessagePartRenderer.tsx:225-234` switches on `isScheduledTaskDraftToolName(part.name)` + `parseScheduledTaskDraftResult(part.result)` → `<ScheduledTaskDraftCard/>`; tool names may be prefixed `server:tool` → match with `name === X || name.endsWith(":"+X)`, see `scheduledTaskDraftResult.ts:24-27`).

### 6.6 Confirm-before-execute patterns

**A. `ask_human` (blocking approval)** — `src/infra/tool/human_tool/tool.py`: `AskHumanTool._arun(message, fields, timeout=300, allow_other)` (L110) → `create_approval(...)` (L163, `src/api/routes/human.py`) → writes an `approval_required` event to the session stream via `get_dual_writer().write_event(...)` → blocks on `wait_for_response(approval.id, timeout)` (L177) → returns `{"status": "success"|"timeout"|"rejected", "values": {...}}`.
The model must *choose* to call it (it is always available; `ask_human` is in `BUILTIN_TOOLS`), the agent run blocks for up to `timeout` (10–3600 s). Excluded from unattended runs and team routers.

**B. Draft → card → REST confirm (recommended for "存入知识库")** — `src/infra/tool/scheduled_task_tool.py` + `src/infra/scheduler/drafts.py`:
* Tool only **creates a draft** (Mongo doc, `DRAFT_TTL = 30 min`, `purge_at` TTL index) and returns `{"entity_type": "scheduled_task_draft", "draft_id", "action", "summary", "before", "changes", "expires_at", "message": DRAFT_MESSAGE}` (`_draft_result`, L360-373).
* The frontend renders a confirm/cancel card (`ScheduledTaskDraftCard`) which calls `POST /api/scheduled-tasks/drafts/{id}/confirm` (`routes/scheduled_task.py:76-82`) — "Only this endpoint (a human click) creates / modifies the task"; confirm atomically claims an unexpired draft (`drafts.confirm_draft`, L294; `transition(from pending → applying)`).
* Permission gate inside the tool: `_authorize(runtime)` (L127-143): unattended? → refuse; `get_user_id_from_runtime`; `runner.resolve_active_user(user_id)`; required permissions; feature switch.
For the KB write tool, mirror this: `draft_save_to_kb(...)` → `{"entity_type":"kb_save_draft","draft_id",...}` + `POST /api/knowledge-bases/drafts/{id}/confirm`. Needs a new frontend card + renderer branch.

### 6.7 Tool names/descriptions as the model sees them (agent-harness spec)

* `src/agents/core/harness_prompt_overrides.py`: `_DEFAULT_TOOLS: dict[str, str]` (L53-88: tool name → concise Chinese description; includes `memory_*`, `read_document`, `list_dify_knowledge_bases`, `query_dify_knowledge_base`, `list_scheduled_tasks`, `draft_scheduled_task`…) and `_DEFAULT_FIELDS: dict[str, dict[str, str]]` (L90-~175: tool → arg → Chinese description; `{}` for arg-less tools).
  `localize_tool_for_model(tool, catalog)` (L193-215) copies only tools whose name is in `catalog.schema_fields`, replacing `description` and compacting `args_schema` with the field descriptions; **unknown tools pass through unchanged** (so unlisted KB tools still work with their docstring descriptions — but will not be harness-localised).
  Names/schemas are never altered. Spec: `agent-harness.md` ("preserve tool names and schemas, changing only descriptions and schema title/description annotations").
* **CONFIRMED by running it (read-only check)**: every tool that has a `runtime: ToolRuntime` parameter is returned **unchanged** (`localize_tool_for_model(t, DEFAULT_HARNESS_CATALOG) is t`) even when its name *is* in the catalog
  (`query_dify_knowledge_base`, `read_document`, `memory_recall`, `draft_scheduled_task` all are): `tool.get_input_schema().model_json_schema()` raises `PydanticInvalidForJsonSchema` (the `ToolRuntime` field) and the bare `except Exception: return tool` (L202-205) passes the tool through.
  Consequence: for KB tools that take `runtime`, the catalog entries in `_DEFAULT_TOOLS/_DEFAULT_FIELDS` are effectively **dead** — the model sees the tool's own docstring + `Annotated[...]` strings. **Write the model-facing description (docstring) and every `Annotated[str, "…"]` arg description directly in concise Chinese in the tool source** (as `scheduled_task_tool.py:603-617` does; test `test_scheduled_task_registration.py:90-140` asserts Chinese property descriptions). Still add catalog entries if you also want a runtime-free tool variant localized.
* System prompt mentions: only memory (`fast_agent/prompt.py:11`, `harness_prompt_overrides.py:155` memory guide). Dify/read_document are **not** mentioned in any system prompt — descriptions alone drive usage. To tell the model which KBs exist per request, inject a section via middleware like `MemoryIndexMiddleware` (`src/infra/agent/middleware/prompt_injection.py:57-84`, `awrap_model_call` → `_append_system_text_block`) or `SectionPromptMiddleware(sections=[...])` (same file L28-54, used with persona sections at `fast_agent/nodes.py:230-232`); register in the `user_middleware` list *before* `PromptCachingMiddleware` (nodes.py:~255) so the KV-cache marker lands after dynamic content.

### 6.8 Gating tools: settings / role / persona / channel / unattended

* **Settings**: flag checks in `build_internal_tools()` (§6.2). Add `ENABLE_KB`-style flag + "model cards configured" check (Dify pattern requires its rerank card, L54-59).
* **Role**: admin tool-policy docs per tool (`MCPStorage.list_tool_policies`: `disabled`, `allowed_roles`, `role_quotas`) applied by `get_internal_tools_for_user`; admins bypass. Plus in-tool permission checks via `TokenPayload.permissions` (`scheduled_task_tool._authorize`).
  New permission strings are registered in 4 places: `src/kernel/types.py:~113` (`Permission` enum, e.g. `SCHEDULED_TASK_USE = "scheduled_task:use"`), `src/kernel/schemas/permission.py:~251` (metadata) and `~416` (group), `src/infra/auth/rbac.py:~174` (default role grants); routes use `Depends(require_permissions("scheduled_task:use", "chat:write"))`.
  Role limits: `src/kernel/schemas/role.py::RoleLimits` (L12-49; `model_config = ConfigDict(extra="allow")`, so KB limits `max_kbs`, `max_docs_per_kb`, `kb_storage_quota_mb`, `max_kb_file_size_mb` can be added as optional fields without migration).
* **Persona**: personas do **not** gate tools, only inject Dify ids/skill hints (`apply_persona_skill_hints_to_agent_options`, chat.py:283).
* **Per-conversation user toggles**: `disabled_tools`/`disabled_mcp_tools` in request → `filter_disabled_tools`.
* **Unattended scheduled runs**: `src/agents/core/scheduled_run.py:30-34` `SCHEDULED_RUN_EXCLUDED_TOOLS = {ask_human, update_sop} | SCHEDULED_TASK_CHAT_TOOL_NAMES`, enforced by `scheduled_run_middleware()` (filters tools before the cache marker) and the prompt "不执行不可逆或对外有副作用的操作" (L36-41). **Add the KB write/draft tool here.** Read-only KB tools are fine.
* **WeCom channel**: `infra/agent/wecom/handler.py` submits with `disabled_tools=[DRAFT_TOOL_NAME]` (cards can't render in WeCom); `tests/infra/tool/test_scheduled_task_registration.py::test_wecom_submit_disables_draft_tool` AST-asserts the list is exactly `[DRAFT_TOOL_NAME]` → if you add a KB draft tool there, **update that test**. WeCom/scheduler paths never get the web `@知识库` ids — they must resolve KBs server-side from "对话中启用" flags.
* **Team router**: add write/management tools to `TEAM_ROUTER_EXCLUDED_TOOLS` (`team_agent/context.py:11`); a test asserts membership for the scheduled tools.
* Tool-call retry hazard: `MCPToolWithRetry` retries up to 3× on errors matching `429/503/502/timeout/connection/network/reset/refused/unavailable` or empty message, with `MCP_TOOL_TIMEOUT = 300 s` per call (`mcp_client.py:27-29, 113-155, 157-`). A write tool must be idempotent or never raise; `query_kb` must finish < 300 s.

---

## 7. Chat request options (how to add "@知识库")

### 7.1 Backend request schema & flow

* `AgentRequest` (`src/kernel/schemas/agent.py:47-83`): `message, session_id, workspace_dir, max_steps, disabled_tools, agent_options: Optional[dict[str, Any]] (L54, "Agent options (e.g., enable_thinking)"), disabled_skills, enabled_skills, persona_preset_id, persona_snapshot, persona_system_prompt, disabled_mcp_tools, user_timezone, attachments, context, project_id, team_id, goal`.
  **No dedicated field for KBs**; `agent_options` is an untyped dict.
* Submit route `src/api/routes/chat.py` (`POST /api/chat/...`, submit function ~L370-600): `resolve_persona_request` → `validate_agent_model_access(request.agent_options, user)` (L141; attaches `model_id/model/_resolved_model_config/_resolved_supports_vision/_resolved_fallback_model/_resolved_model_profile`) → `apply_dify_kb_dataset_ids_to_agent_options(...)` (L415-424) → …
  → `task_context = {..., "agent_options": request.agent_options, ...}` (L484-500) → `TaskArqPayloadStore.save` → arq `run_agent_task` → `task_executor.run_task(agent_options=payload["agent_options"])` (`arq_worker.py:114`) → `agent.stream(**kwargs)` → `config["configurable"]["agent_options"]` (`fast_agent/graph.py:151,175`, `nodes.py:83,279`; same in search/team).
* Session metadata also stores `agent_options` (`build_conversation_config`, chat.py:228-260 → `conversation_config["agent_options"]`), restored by the UI on session load (`useSessionConfig.ts:286`, `ChatAppContent.tsx:713`). A per-message-only `kb_ids` should therefore be **stripped from the persisted session config** (or you will re-apply last message's @KBs on re-open) — **UNCONFIRMED** whether you want persistence.
* **No sanitisation of `agent_options` keys** (grep of chat.py/chat_validation.py/session.py): a client may send `dify_kb_dataset_ids`, `kb_ids`, or any `_resolved_*` key. Dify's own tool defends by re-validating ids against the persona/default scope. The KB tool must: (a) take the ids from `agent_options` as *hints*, (b) re-authorise every id (owner / shared-to-me / subscribed+public, `enabled_in_chat`, feature flag, role quota) per call, (c) ideally `pop` client-supplied `kb_*` keys in `chat.py` before applying server-resolved ones.
* Recommended server helper (mirrors `apply_dify_kb_dataset_ids_to_agent_options`): `apply_kb_scope_to_agent_options(agent_options, user_id, mentioned_ids)` called from chat.py next to L420 **and** from WeCom/scheduler (with `mentioned_ids=None`), writing e.g. `agent_options["kb_scope"] = {"mentioned": [...], "allowed": [...]}`.
* Payload limits: arq payload ≤ 2 MiB (`TASK_ARQ_PAYLOAD_MAX_BYTES`), so keep ids as ids (not content).

### 7.2 Frontend plumbing

* `frontend/src/components/chat/chatInputTypes.ts:15-21`: `ChatInputProps.onSend(message, options?: Record<string, boolean|string|number>, attachments?, onAccepted?)`; `ChatInput.tsx:536-548` calls `onSend(buildEmphasizedUserMessage(trimmed, emphasizedSkillNames, …), agentOptionValues, draftAttachments, handleAccepted)`.
  `agentOptionValues` is a prop (state owned above, default `{}`; used for `enable_thinking`, model selection …).
* `frontend/src/hooks/useAgent.ts:596-600` `sendMessage(content, agentOptions?: Record<string, boolean|string|number>, attachments?, onAccepted?)`; merged at L660-663:
  `const fullAgentOptions = { ...options?.getAgentOptions?.(), ...agentOptions }` → `sessionApi.submitChat(currentAgent, content, sessionId, fullAgentOptions, attachments, projectId, disabledSkills, disabledMcpTools, personaPresetId, enabledSkills, teamId, goal)` (L665-678)
  → `buildSubmitChatBody({message, sessionId, agentOptions, attachments, projectId, disabledSkills, enabledSkills, personaPresetId, disabledMcpTools, userTimezone, teamId, goal})` (`frontend/src/services/api/session.ts:162-205`) → body key `agent_options`.
  **Type blocker**: `agent_options?: Record<string, boolean | string | number>` appears in `session.ts:184`, `useAgent.ts:596`, `useAgent/types.ts:317`, `ChatAppContent.tsx:686`, `chatInputTypes.ts` — arrays are not allowed. Either widen the type to include `string[]`/`unknown`, or send a comma/JSON-string (`kb_ids: "id1,id2"`) and split on the server, or add a dedicated `kb_ids?: string[]` argument/body field + `AgentRequest.kb_ids` (cleaner, typed, no persistence in `agent_options`).
* `@` mention is **already taken**: `ChatInput.tsx:193-196` `mentionMode = currentAgent === "team" ? "team" : "persona"`; `useMentionState` (`hooks/useMentionState.ts:findMentionMatch` — `@` at word start up to the cursor), `useMentionSearch` (persona presets), `useTeamMentionSearch`, `MentionPopup.tsx`, `TeamMentionPopup.tsx`; selecting a persona replaces the `@query` text and sets `selectedPersonaPresetId`.
  "@知识库" needs a third mention source (or a combined popup with sections) — e.g. a `kbMention` mode that opens when the persona list lacks a match or via a different trigger (`#`/`@知识库`), producing chips (like `emphasizedSkillNames`, ChatInput L812-830) that are submitted as `kb_ids`. Skill-emphasis precedent: the message text is rewritten with "请必须使用{{names}}技能。" (`buildEmphasizedUserMessage`) — an extra hint line for mentioned KBs is possible but ids must still be sent structurally.
* Frontend citation rendering: new branch in `MessagePartRenderer.tsx` (L225-234 pattern) keyed on tool name (`query_kb` etc.) / `entity_type`; part type has `part.name, part.args, part.result (string | Record), part.success, part.isPending`.

---

## 8. Reuse checklist for the implementer (ordered)

1. Settings: `KB_ENABLED`, `KB_EMBEDDING_MODEL_ID`, `KB_RERANK_MODEL_ID`, `KB_LLM_MODEL_ID` (graph), `KB_ARQ_QUEUE_NAME`, `KB_WORKER_MAX_JOBS`(4), `KB_JOB_TIMEOUT_SECONDS`, batch/chunk limits → follow the `DIFY_KB_*` / `NATIVE_MEMORY_*` definition pattern (§2.1); reuse `ENABLE_DOCUMENT_PARSE`/`MINERU_*` (§1.3).
2. Small shared helpers (new): `src/infra/kb/embedding.py` (card resolve + `/v1` normalisation + batched, retried, concurrency-limited embed + dim probe), `rerank()` (copy §2.4), `parse_to_markdown(filename, bytes)` (MinerU / `_decode_text` / csv / xlsx / html), JSON-LLM helper (copy §3.2 with fence-strip + bracket-slice).
3. Queue: Mongo `kb_tasks` table (lease+heartbeat+attempts), `KbArqRuntime` Worker on its own queue wired in `runtime_services.py`, Redis ZSET slot (copy `scheduler/locks.py` with new key), reconcile via `ScheduledJob` + Redis lease.
4. Progress: Mongo status is truth; `send_to_user_with_broadcast(owner_id, {"type":"kb:file_status",...})` as accelerator; frontend `kbEvents.ts` bus + 5-30 s polling fallback.
5. Tools: add to `build_internal_tools()` behind `KB_ENABLED` + cards configured; names unique (avoid `list_dify_knowledge_bases` clash); JSON ≤ 8000 chars; Chinese docstring/`Annotated` descriptions in source (catalog is bypassed for `runtime` tools, §6.7); never raise; re-authorise ids from `agent_options`; draft→confirm for the write tool + add to `SCHEDULED_RUN_EXCLUDED_TOOLS`, `TEAM_ROUTER_EXCLUDED_TOOLS`, and WeCom `disabled_tools` (+ update the AST test).
6. Chat: server `apply_kb_scope_to_agent_options` helper called from chat.py/WeCom/scheduler; widen frontend option typing; add `@知识库` mention source; frontend citation card renderer.
7. Share notifications: persist in KB share docs + WS push; no `notifications` collection reuse.

## 9. Not found / unverified

* Any existing batch-embedding, vector-dimension, or embedding rate-limit code — none exists (searched `src/` for `embedding`, `rerank`, `/v1/embeddings`; only memory + Dify consumers).
* Which `api_base` convention admins use for embedding/rerank cards (`…` vs `…/v1`) — **UNCONFIRMED**; the code + test fixtures disagree with the UI placeholder.
* Per-user "站内通知" inbox — does not exist (searched `src/infra/notification`, `src/api/routes/notification.py`, schemas).
* Whether deepagents subagents inherit `configurable.agent_options`/`context` — not verified at runtime.
* Whether MinerU is actually deployed/enabled in the target environment (`ENABLE_DOCUMENT_PARSE` default False) — not checkable from the repo.
* I did not run any tests or start services (read-only research); no repository files other than this note were modified. `pyproject.toml`/`uv.lock` show uncommitted `qdrant-client` changes that I did **not** make.
