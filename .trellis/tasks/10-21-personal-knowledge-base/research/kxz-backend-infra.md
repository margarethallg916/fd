# 昆小智 backend infrastructure — conventions for the personal-KB feature

Repo: `/tmp/hoplite/workspace` @ `1498b534` (branch `hoplite/poteidaia-708f791c`). Python 3.12, FastAPI, Motor, Redis, arq.
All paths below are relative to the repo root. Line numbers are from this checkout. `UNCONFIRMED` = not verified in code.

Note: `pyproject.toml` already has an uncommitted `qdrant-client>=1.15.0` dependency (`git status`: `M pyproject.toml`, `M uv.lock`). Nothing in `src/` imports qdrant yet.

---

## 0. TL;DR — the pitfalls most likely to bite

1. **`.html` / `.htm` / `.xhtml` / `.svg` / `.js` / `.sh` / `.php`… are in `DANGEROUS_UPLOAD_EXTENSIONS`** (`src/api/routes/upload.py:85-130`). The PRD wants html KB docs, so reusing the denylist verbatim rejects html. Decide explicitly (exclude html from the KB denylist and sanitize on parse, or drop html support).
2. **Per-user "my files" quota is a separate, heavyweight ledger** (`UserStorageQuotaService`, `src/infra/storage/user_storage.py`, 2830 lines). Billable and listable sources must stay identical (`STORAGE_LISTABLE_SOURCES`, `src/kernel/schemas/storage.py:65`). The PRD wants KB storage accounted separately, so do **not** add a `StorageSource.KB`. Use the raw `get_or_init_storage()` and a KB-owned ledger.
3. **`RoleStorage.update()` replaces `limits` wholesale** (`src/infra/role/storage.py:316`), and `RolesPanel.tsx` only sends the limit fields it knows. New KB limit fields are silently wiped on any role edit unless `RolesPanel.tsx` and `RoleLimits` in `frontend/src/types/auth.ts:140` are updated together.
4. **Deployed `user` role is NOT auto-granted new permissions.** Only `is_system` roles (admin) are re-synced at every startup (`src/infra/role/storage.py:483-524`). `user` is `is_system: False` (`src/infra/auth/rbac.py:179`). The same applies to `scheduled_task:use` (`.trellis/spec/backend/scheduled-tasks.md:38`: "ops must assign to pilot roles"). Either gate normal use on login + feature switch + quotas (no new permission), or ship a one-shot grant migration.
5. **A raising startup index initializer aborts app startup.** `_initialize_startup_indexes` re-raises (`src/api/main.py:428-436`). The scheduler wraps its own `startup()` in try/except (`src/infra/scheduler/service.py:433-449`). Wrap Qdrant/optional-index setup likewise.
6. **No Mongo transactions anywhere** (`grep transaction|start_session src/` finds none; Mongo is standalone). Atomicity comes from single-document `find_one_and_update` with the condition in the filter, plus versioned/leased ledger rows. Never write "read then write across documents" without a guard.
7. **Settings precedence is DB > env > definition default** after `initialize_settings()` (`src/kernel/config/service.py:255-295`). For infra endpoints (QDRANT_URL / API key) use env-only `Settings` fields (not in `SETTING_DEFINITIONS`). Admin-tunable toggles/quotas go in `SETTING_DEFINITIONS` and need a matching `Settings` attribute, or `setattr(settings, key, …)` in `refresh_settings` raises.
8. **Multipart body-limit exemption is an allowlist.** Non-upload bodies are capped at 8 MiB (`src/api/main.py:87-88, 105-120`). A new KB upload route must be added to `API_MULTIPART_UPLOAD_PATHS` / `_is_body_limit_exempt`, or files over 8 MiB get HTTP 413.
9. **Deleting a user cascades nothing to other collections** (`src/infra/user/manager.py:150-176` only drops S3 objects under prefix `{user_id}` / `avatars/{user_id}` and the user row). KB cleanup for deleted owners must be added or tolerated.
10. **No non-admin user lookup exists.** `GET /api/users/` needs `user:read` (`src/api/routes/user.py:18-27`). A KB "validate employee IDs" endpoint is new work and must return only existence (the display name is the username).

---

## 1. App entry, routers, auth, errors, lifespan

### Entry
- `main.py:1-17` `run()` starts `uvicorn.run("src.api.main:app", host="0.0.0.0", port=settings.PORT, reload=settings.DEBUG, timeout_graceful_shutdown=30)`. `run.py` just calls it. The Dockerfile CMD is `python main.py`.
- `src/api/main.py:783` `create_app()` builds `FastAPI(lifespan=lifespan)`. A module-level `app = create_app()` is at the end.
- Middleware (added at 802-805, executed top-down): `RequestBodyLimitMiddleware` → `TracingMiddleware` → `AuthMiddleware` → `UserContextMiddleware`. CORS is `*`.

### Mounting a router
Route modules are imported in the tuple at `src/api/main.py:20-58` (`from src.api.routes import (agent, analytics, ..., websocket,)`). They are mounted in `create_app()` at lines 808-876:
```python
app.include_router(scheduled_task.router, prefix="/api/scheduled-tasks", tags=["Scheduled Tasks"])
app.include_router(scheduled_task_admin.router, prefix="/api/admin/scheduled-tasks", tags=["Scheduled Tasks Admin"])
app.include_router(mcp.admin_router, prefix="/api/admin/mcp", tags=["MCP Admin"])
```
Some routers are imported lazily inside `create_app` (marketplace, builtin_skill, lines 833-846). Convention:
- User routes: `/api/<domain>`. Admin routes: `/api/admin/<domain>`, in the same file as a second router (`mcp.admin_router`) or a `*_admin.py` file.
- The prefix is given in `include_router`, not in `APIRouter(...)`.
- `src/api/routes/__init__.py` is only a docstring.
- Register WebSocket-style routers last (line 876).
- Route order matters inside one router: in `src/api/routes/settings.py` the catch-all `GET /{key}` (line 319) is declared before `GET /dify-kb/datasets` (line 402), so a literal path after it is shadowed. Declare literal paths before `/{key}`.

### Auth middleware (default deny)
`src/api/middleware/auth.py:23-120`. Every path outside `PUBLIC_PATHS`, `PUBLIC_PREFIXES` (includes `"/api/agents"`, `"/api/upload/file/"`, `"/api/share/public/"`) and `PUBLIC_PATH_PATTERNS` needs a `Bearer` header, otherwise 401 `{"detail": "Not authenticated"}`. A new `/api/knowledge-bases/...` is private automatically.

### Auth dependencies (`src/api/deps.py`)
```python
async def get_current_user_required(request, credentials=Depends(security)) -> TokenPayload   # deps.py:329
def require_permissions(*permissions: str)  # deps.py:349 -> dependency returning TokenPayload; 403 "缺少权限: x"
```
- `TokenPayload` (`src/kernel/schemas/user.py:107`): `sub` (user id = `str(ObjectId)` of `users._id`; use this as `owner_id`), `username` (= employee ID / 工号), `roles: list[str]`, `permissions: list[str]` (re-derived from the DB roles on every request, cached 45 s), `sid`, `credential_version`.
- `get_current_user_required` also blocks with 403 `{"code": "PASSWORD_CHANGE_REQUIRED"}` when `must_change_password`.
- Optional auth: `get_current_user` / `get_current_user_optional` (returns `None`).
- Whole-router guards are the convention:
```python
router = APIRouter(dependencies=[Depends(require_permissions("scheduled_task:use", "chat:write"))])   # scheduled_task.py:36
_require_admin = require_permissions("scheduled_task:admin")
router = APIRouter(dependencies=[Depends(_require_admin)])                                              # scheduled_task_admin.py:35-37
```
- **There is no generic `is_admin` dependency.** "Admin-only" means a permission string held only by the admin role. `admin` is a system role holding every `Permission` enum value (`src/infra/auth/rbac.py:131`, synced on every boot). Define e.g. `Permission.KNOWLEDGE_BASE_ADMIN = "knowledge_base:admin"` (see §4).
- Handlers take `user: TokenPayload = Depends(get_current_user_required)` and scope every query by `user.sub`.

### Response models, pagination, errors
- Pydantic schemas live in `src/kernel/schemas/<domain>.py`. Routes declare `response_model=`; create uses `status_code=status.HTTP_201_CREATED`; delete often uses 204 with `-> None` (`scheduled_task.py:232`), or `{"status": "deleted"}` (`role.py:107`).
- Pagination is inconsistent. Pick one for the KB:
  - `skip`/`limit` returning `{items,total,skip,limit}`: `scheduled_task_admin`, `role` (`{roles,total,skip,limit}`), `user` (+`has_more`).
  - `page`/`page_size` returning `{items,total,page,page_size}`: `src/api/routes/revealed_file.py:15-47` (`page_size` le=50).
  - Cursor: `/api/storage/files` (`cursor`+`limit`).
  - Storage classes clamp the limit again (`min(max(int(limit),1),MAX)`, e.g. `scheduler/storage.py:99`).
- Errors: the domain layer raises its own exception types (usually `ValueError` subclasses such as `ScheduledTaskQuotaError`, `src/infra/scheduler/service.py:53`, or `StorageDomainError` subclasses carrying `.code` and `.status_code`). The **route** maps them to `HTTPException`:
```python
except (ScheduleValidationError, ScheduledTaskQuotaError, ScheduledTaskDisabledError) as exc:
    raise _bad_request(exc) from exc            # 400 {"detail": str(exc)}      scheduled_task.py:89-103
except Exception as exc:
    logger.exception("[ScheduledTask] confirm draft %s failed", draft_id)
    raise HTTPException(status_code=500, detail=GENERIC) from exc
```
  - Shared exception classes in `src/kernel/exceptions.py` (`ValidationError`, `NotFoundError`, `AuthorizationError`, `StorageError`… plain `Exception`s). Routes catch them explicitly (`role.py:62-66`).
  - Body is `{"detail": "..."}`. Structured form for the storage domain: `{"detail": {"code":..., "message":..., "usage":...}}` (`upload.py:537`). Messages are Chinese for user-facing text, English codes for machine-readable text. 404 for "not yours / not found" (non-enumerating).
  - Quota/disabled: scheduled tasks use 400; storage quota uses `exc.status_code` (`StorageQuotaExceededError`).
- Logging: `from src.infra.logging import get_logger; logger = get_logger(__name__)`; messages carry a bracket tag (`"[ScheduledTask] ..."`, `"[Settings] ..."`).
- Blocking/CPU work goes through `from src.infra.async_utils import run_blocking_io` (`blocking.py:48`).

### Lifespan hooks (`src/api/main.py:454-770`)
Order inside `lifespan`: `setup_logging` → `RoleStorage().init_default_roles()` → access log wiring → `initialize_settings()` (DB settings → global `settings`) → tracing → `ensure_local_filesystem_dirs` → memory monitor → warm agent registry (background) → **`_run_startup_indexes(app)` (line 516)** → **`start_runtime_services()` (line 519)** (arq embedded worker, pub/sub listeners) → `get_scheduled_task_service().startup()` (try/except) → `_schedule_stale_task_cleanup` → `_schedule_storage_maintenance(app)` (line 533) → model warmup → backfills → WeCom → `yield`.

Index initializers: `_startup_index_initializers()` (line 346) returns a list of `(name, async fn)`; they run concurrently under `asyncio.gather`:
```python
async def _init_storage_domain() -> None:                      # main.py:408
    from src.infra.storage.user_storage import UserStorageQuotaStorage
    await UserStorageQuotaStorage().ensure_indexes()
...
("storage_domain", _init_storage_domain),                      # appended to the returned list, main.py:424
```
Add a `("knowledge_base", _init_kb_storage)` entry here (lazy import inside the function), or call it from a service `startup()` guarded by try/except.

Background loop pattern (copy for the KB maintenance/reconcile loop): `_schedule_storage_maintenance` (`main.py:321-339`):
```python
async def _run() -> None:
    interval = max(10.0, float(getattr(settings, "USER_STORAGE_MAINTENANCE_INTERVAL_SECONDS", 60.0)))
    while True:
        try: await run_storage_maintenance()
        except asyncio.CancelledError: raise
        except Exception as exc: logger.warning("Personal storage maintenance failed: %s", exc)
        await asyncio.sleep(interval)
task = asyncio.create_task(_run(), name="storage:maintenance"); app.state.storage_maintenance_task = task
```
and add the `app.state` attribute name to `_LIFESPAN_BACKGROUND_TASK_NAMES` (`main.py:89-102`) so shutdown cancels it (`_cancel_background_tasks`, line 274).

Runtime listeners/workers live in `src/infra/runtime_services.py` (`start_runtime_services` line 80, `stop_runtime_services` line 119): it calls `start_arq_runtime()` / `stop_arq_runtime()`. Start/stop a KB worker next to it, or in `lifespan` using the `app.state.*_task` + `_LIFESPAN_BACKGROUND_TASK_NAMES` pattern.

### Existing arq infrastructure (relevant to the "kb queue")
- `src/infra/task/arq_settings.py:build_arq_redis_settings(settings) -> arq.connections.RedisSettings` (handles Sentinel/password/db).
- `src/infra/task/arq_runtime.py:EmbeddedArqRuntime` starts one embedded `arq.worker.Worker([run_agent_task], queue_name=settings.ARQ_QUEUE_NAME, redis_settings=..., handle_signals=False, max_jobs=settings.ARQ_WORKER_MAX_JOBS, job_timeout=settings.ARQ_JOB_TIMEOUT_SECONDS, ctx={...}, allow_abort_jobs=True)` via `asyncio.create_task(worker.async_run())`. Only when `TASK_BACKEND == "arq"` and `ARQ_EMBEDDED_WORKER`. `stop()` calls `worker.close()`, tolerates a Windows `SIGUSR1` AttributeError, cancels and awaits the task.
- Enqueue: `src/infra/task/manager.py:92` `create_pool(build_arq_redis_settings(settings), default_queue_name=settings.ARQ_QUEUE_NAME)` then `await pool.enqueue_job("run_agent_task", run_id, _job_id=run_id)` (line 458). A KB queue = a second `Worker` with `queue_name="kunxiaozhi:kb"` (new env), a second pool with `default_queue_name=...`, and `_job_id` for dedupe.
- Settings: `TASK_BACKEND: str = "arq"`, `ARQ_EMBEDDED_WORKER: bool = True`, `ARQ_QUEUE_NAME = "kunxiaozhi:arq"`, `ARQ_WORKER_MAX_JOBS = 64`, `ARQ_JOB_TIMEOUT_SECONDS = 86400` (`src/kernel/config/base.py:174-178`).
- Redis client: `from src.infra.storage.redis import get_redis_client`. The scheduler's `locks.py` shows token-based `SET NX EX` locks, Lua compare-and-delete and a ZSET concurrency cap (`acquire_run_slot(member, limit, ttl)`) — directly reusable as the "max 4 concurrent KB jobs" gate.
- Heartbeats: `src/infra/task/heartbeat.py`, `HEARTBEAT_INTERVAL=10`, `HEARTBEAT_TIMEOUT=60` (`task/constants.py`).
- WebSocket push: `from src.infra.websocket import get_connection_manager`; `await get_connection_manager().send_to_user_with_broadcast(user_id, {...})` (`websocket.py:308`) fans out across replicas via Redis.

---

## 2. MongoDB access

- Driver: **Motor** (`motor>=3.7.1`, plus `pymongo` for `DESCENDING`, `ReturnDocument`, `DuplicateKeyError`). No ORM, no migration system.
- Client singleton: `src/infra/storage/mongodb.py:30` `get_mongo_client()` (`@lru_cache`), created with `tz_aware=True, tzinfo=timezone.utc`, so datetimes read back are UTC-aware. DB name `settings.MONGODB_DB` (default `agent_state`).
- Collection-handle pattern (storage class, one per domain, instantiated per call or via module singleton):
```python
class ScheduledTaskStorage:                                   # src/infra/scheduler/storage.py:46
    def __init__(self) -> None:
        self._tasks = None
    @property
    def tasks(self):
        if self._tasks is None:
            self._tasks = get_mongo_client()[settings.MONGODB_DB][TASKS_COLLECTION]
        return self._tasks
...
_storage: Optional[ScheduledTaskStorage] = None
def get_scheduled_task_storage() -> ScheduledTaskStorage:     # line 248
    global _storage
    if _storage is None: _storage = ScheduledTaskStorage()
    return _storage
```
  mypy flags `self._tasks = None` then assigning a collection (baseline has these exact errors in `scheduler/storage.py`). Annotate `self._tasks: Any = None` in new code.
- Index creation: an `async def ensure_indexes()` using `create_index` (idempotent), called from the startup initializer list (§1). Two patterns exist: eager `ensure_indexes()` (scheduler, analytics, user_storage) and lazy `ensure_indexes_if_needed()` on first use (users, revealed_files, sessions). Use the eager one. Example (`scheduler/storage.py:64-71`):
```python
await self.tasks.create_index([("owner_id", 1), ("updated_at", DESCENDING)])
await self.runs.create_index([("task_id", 1), ("started_at", DESCENDING)])
```
  Unique indexes use `unique=True` and rely on `DuplicateKeyError` for races (`UserStorage.create`, `UserStorageQuotaStorage.insert_file`).
- IDs: no single convention.
  - `users` and `roles` use `ObjectId` (`_id`), exposed as `id: str`. `UserStorage.get_by_id` does `ObjectId(user_id)` and returns `None` on `InvalidId`.
  - Scheduled tasks use `str(uuid4())` as `_id` (`scheduler/service.py:140`).
  - Storage `file_id` is `uuid4().hex` (32 hex chars; the public-content regexp requires exactly that).
  - `owner_id` / `user_id` fields always hold the **user's ObjectId string** (`TokenPayload.sub`), not the username.
  - Recommendation: KB/document/job ids = `uuid.uuid4().hex`, stored as `_id`.
- Models with Mongo `_id` use `id: str = Field(..., alias="_id")` + `ConfigDict(populate_by_name=True)` and are dumped with `model_dump(by_alias=True, mode="python")` (`scheduled_task.py:157-159`, `scheduler/storage.py:30`). Validate on read; skip invalid docs in list endpoints so one bad document does not break listings (`_valid_tasks`, line 33).
- Timestamps: always **tz-aware UTC** `datetime` from `src.infra.utils.datetime.utc_now()`; helpers `utc_now_iso`, `ensure_utc`, `to_iso`, `parse_iso`. Exception: `system_settings` stores ISO strings (`utc_now_iso()`). The scheduler's `Asia/Shanghai` default is for schedule display only. Use `utc_now()` for `created_at`/`updated_at`; targeted `$set` for state transitions.
- Representative pair to copy: `src/api/routes/scheduled_task.py` (router + `Depends`) → `src/infra/scheduler/service.py` (`ScheduledTaskService.create_task`: feature check → count quota → build doc with `uuid4`/`utc_now` → `storage.create_task`) → `src/infra/scheduler/storage.py` (`ScheduledTaskStorage`) → `src/kernel/schemas/scheduled_task.py` (models). `get_task(task_id, owner_id)` always filters by `owner_id` when given; admin paths pass `None`.
- Quota check pattern (racy, accepted): `if await storage.count_tasks(owner_id) >= limit: raise ScheduledTaskQuotaError(...)`. For hard limits use a versioned/leased ledger doc like `UserStorageQuotaStorage` (collections `user_storage_usage`, `file_blobs`, `user_files`, `storage_operations`, `storage_operation_items`, `file_message_refs`; `user_storage.py:50-66`), or an atomic `find_one_and_update({"_id": uid, "count": {"$lt": limit}}, {"$inc": {"count": 1}}, upsert=...)`.
- **Transactions: none.** Standalone Mongo (see PRD note on mongot) cannot run them.
- Atomic state transitions use filters with the expected current state, e.g. `mark_run_interrupted` (`scheduler/storage.py:213`: `update_one({"_id": run_id, "status": {"$in": [PENDING, RUNNING]}}, {"$set": ...})` then `modified_count`). Copy this for "claim job" with lease expiry.
- Spec guidance (`.trellis/spec/backend/database-guidelines.md`): "Instantiate storage per call", "New fields need defaults", "targeted `$set` updates for status", no raw collection access outside Storage classes.
- Search inputs: escape user text with `re.escape` before `$regex` (`scheduler/storage.py:116`, `user/storage.py:_safe_search_pattern`).

---

## 3. Admin settings system

### Pieces
1. **Enum** `SettingCategory` in `src/kernel/schemas/setting.py:30-56` (`DIFY`, `DOCUMENT_PARSE`, `MEMORY_*`, `USER`, `AGENT`, …). Add `KNOWLEDGE_BASE = "knowledge_base"`.
2. **Definitions** `SETTING_DEFINITIONS: dict[str, dict]` in `src/kernel/config/definitions.py` (+ `EXTRA_SETTING_DEFINITIONS` in `_definitions_extra.py`, merged at the end of `definitions.py` via `SETTING_DEFINITIONS.update(...)`). Entry shape (`_definitions_extra.py:300-325`):
```python
"USER_STORAGE_DEFAULT_QUOTA_MB": {
    "type": SettingType.NUMBER,              # STRING|TEXT|NUMBER|BOOLEAN|JSON|SELECT
    "category": SettingCategory.USER,
    "subcategory": "personal_quota",
    "description": "settingDesc.USER_STORAGE_DEFAULT_QUOTA_MB",   # i18n key
    "default": 1024,
    "minimum": 1,                            # also "maximum", "step"; enforced in SettingsStorage.set
    "frontend_visible": True,                # visible to non-admin users via GET /api/settings/
    "depends_on": "SCHEDULED_TASK_ENABLED",  # or {"key": ..., "value": ...}
    "is_sensitive": True,                    # masked "********" in API
    "options": [...],                        # SELECT only
},
```
3. **Runtime attribute** on `class Settings(BaseSettings)` in `src/kernel/config/base.py` (e.g. `SCHEDULED_TASK_ENABLED: bool = True` line 408; `DIFY_KB_*` lines 469-478). **Required** for every key in `SETTING_DEFINITIONS`: `refresh_settings(key)` does an unconditional `setattr(settings, key, value)` (`config/service.py:~330`), which raises on an unknown pydantic field. Defaults must match (`tests/kernel/config/test_scheduled_task_governance_definitions.py:56` asserts `getattr(Settings(), key) == definition["default"]`). `model_config` reads `.env` with `extra="ignore"` (`base.py:480`).
4. **Persistence**: Mongo collection `system_settings`, `_id` = key, fields `value, type, category, description, default_value, updated_at (ISO str), updated_by` (`src/infra/settings/storage.py`, `SettingsStorage.set`). Type/range validation is in `SettingsStorage.set` (number min/max/finite, boolean strict, select options, json list/dict).
5. **Cache/propagation**: `SettingsService.set()` → `refresh_settings(key)` updates the process-wide `settings` singleton → `_publish_change` on Redis channel `settings:changed`; other replicas' `SettingsPubSub` call `refresh_settings(key)` (`src/infra/settings/pubsub.py:63-80`). So runtime reads are plain attribute reads: `settings.KB_ENABLED`, or `getattr(settings, "X", default)` as in `max_tasks_per_user()` (`scheduler/service.py:80`).
6. **Startup**: `initialize_settings()` (`config/service.py:255`) → `SettingsService.init_from_env()` imports `os.environ[KEY]` into the DB **once** for keys with no DB row (`updated_at is None`), then loads all DB values (or the definition default when none) onto `settings`. Net precedence: **DB > env > definition default** (docstring at line 258). Empty strings are ignored unless the key is in `_ALLOW_EMPTY_STRING_SETTINGS` (`config/service.py:20-29`, model-id keys).
7. **Restart-required**: add the key to `RESTART_REQUIRED_SETTINGS` (`src/kernel/config/constants.py:11-33`); the API reports `requires_restart`. Hot-reload hooks for subsystems are keyed sets in `refresh_settings` (e.g. `memory_affected_settings`, `_SANDBOX_AFFECTED_SETTINGS`); add one if the Qdrant client / embedding client must be rebuilt on change.
8. **Sensitive**: `is_sensitive: True` → `SENSITIVE_SETTINGS` set is auto-built (`constants.py:44`); API returns `"********"`, and `PUT` with `"********"` raises.

### Admin API contract (`src/api/routes/settings.py`, mounted `/api/settings`)
- `GET /api/settings/` (any logged-in user): `SettingsResponse = {"settings": {category: [SettingItem...]}}`. With `settings:manage` ⇒ all keys; otherwise only `frontend_visible: True` keys.
- `SettingItem` (`setting.py:81-100`): `key, value, type, category, subcategory, description, default_value, requires_restart, is_sensitive, frontend_visible, depends_on, options, json_schema, minimum, maximum, step, updated_at, updated_by`.
- `PUT /api/settings/{key}` body `{"value": <any>}` (permission `settings:manage`) → `SettingUpdateResponse {setting, message, requires_restart}`; `ValueError` ⇒ 400. `GET /api/settings/{key}`, `POST /api/settings/reset/{key}`, `POST /api/settings/reset`, `POST /api/settings/init` → `SettingResetResponse {message, reset_count}`.
- The frontend model pickers read `GET /api/agent/models` cards filtered by kind. The model-card settings keys are listed in `MODEL_CONFIG_SETTING_KEYS` and `MODEL_CARD_KIND_FILTER` (`frontend/src/components/panels/SettingsPanel.constants.ts:44-63`; kinds `chat|transcribe|embedding|rerank`). Backend reads a card via `await LLMClient.get_card_config(model_id, kind="embedding")` → dict with `api_base, api_key, model` (`src/infra/memory/client/native/backend.py:523-560`, `src/infra/tool/dify_kb_tool.py:249`). Existing embed code posts `{"input": text, "model": model}` to `{api_base}/v1/embeddings` (single text, no batching) and rerank posts to `/v1/rerank`.

### Exactly what a new `knowledge_base` group needs
Backend:
1. `SettingCategory.KNOWLEDGE_BASE = "knowledge_base"` (`setting.py`).
2. Definitions (put in `_definitions_extra.py` or `definitions.py`), e.g. `KB_ENABLED` (BOOLEAN, `frontend_visible: True` so the sidebar can gate on it), `KB_ALLOW_PUBLIC`, `KB_ALLOW_GRAPH`, `KB_PUBLIC_REQUIRES_REVIEW`, `KB_WORKER_CONCURRENCY` (NUMBER, min/max), `KB_EMBEDDING_MODEL_ID` / `KB_RERANK_MODEL_ID` / `KB_GRAPH_LLM_MODEL_ID` (STRING `""`; add to `_ALLOW_EMPTY_STRING_SETTINGS`), chunk defaults, max file bytes, etc. Use `depends_on: "KB_ENABLED"`.
3. Matching `Settings` fields in `base.py` with identical defaults.
4. Restart-required / hot-reload entries if needed (`constants.py`, `config/service.py`).
5. Tests mirroring `tests/kernel/config/test_scheduled_task_governance_definitions.py` (default parity, ranges, `depends_on`, zh+en i18n coverage).
Frontend (contract only):
6. `SettingCategory` union in `frontend/src/types/settings.ts:~39`; `CATEGORY_ORDER` in `SettingsPanel.constants.ts:3-29`; `CATEGORY_LABELS` map in `SettingsPanel.tsx:~64-90` (`t("categories.knowledge_base")`).
7. i18n: `categories.knowledge_base` and one `settingDesc.<KEY>` per key in `frontend/src/i18n/locales/{zh,en,ja,ko,ru}.json` (the existing test requires zh+en; PRD says ja/ko/ru may fall back to English).
8. Model-card pickers: add each `*_MODEL_ID` key to `MODEL_CONFIG_SETTING_KEYS` and `MODEL_CARD_KIND_FILTER` (`embedding`/`rerank`/`chat`).
9. Sidebar/feature gating: `SettingsContext` exposes `enableScheduledTasks: getBooleanSetting("SCHEDULED_TASK_ENABLED")` (`frontend/src/contexts/SettingsContext.tsx:30,163`). Add `enableKnowledgeBase` the same way.

Infra env-only values (QDRANT_URL, QDRANT_API_KEY, collection prefix): see §9; keep out of `SETTING_DEFINITIONS` so env/compose stays authoritative.

---

## 4. RoleLimits and quotas

- Schema: `src/kernel/schemas/role.py:12-49`:
```python
class RoleLimits(BaseModel):
    max_channels: Optional[int] = Field(default=None, ...)
    max_concurrent_chats: Optional[int] = Field(default=5, ...)
    max_queued_chats: Optional[int] = Field(default=10, ...)
    max_file_size_image/video/audio/document: Optional[int] = None   # MB
    max_files: Optional[int] = None
    storage_quota_mb: Optional[int] = Field(default=None, gt=0, le=(1 << 60)//(1024*1024), ...)
    model_config = ConfigDict(extra="allow")   # future extensions
```
  `None` means "use the global default" (or unlimited for channels/chats). `extra="allow"` means unknown keys round-trip through the API, but only the declared fields are validated.
- Storage: `roles` collection, `limits` is a nested dict (`RoleStorage.create` line 169 / `update` line 316: `update_dict["limits"] = role_data.limits.model_dump()`). `model_dump()` emits every declared field including `None`s. Reads are Redis-cached per role name; `invalidate_cache(role_name)` and `clear_auth_cache()` are called on writes.
- Admin edits: `PUT /api/roles/{id}` (permission `role:manage`, `src/api/routes/role.py:57`) with `RoleUpdate.limits: RoleLimits`. `RoleStorage.update` raises `ValidationError("系统角色不可修改")` for `is_system` roles (admin has `limits: None`, i.e. unlimited). Front end: `RolesPanel.tsx` (builds a `limits` object from local state, lines 57-210; **omits any field it does not know** — see TL;DR 3) and the TS type `RoleLimits` (`frontend/src/types/auth.ts:140-151`, has `[key: string]: number | null | undefined`).
- Defaults per role live in `RBACManager.get_default_roles()` (`src/infra/auth/rbac.py:120-202`): `admin` limits `None`; `user` limits `{"max_channels": 10}`; `guest` `{"max_channels": 0}`. `init_default_roles` (`role/storage.py:483`) **inserts missing default roles, and for `is_system` roles overwrites permissions/limits every boot; `user`/`guest` are only created if absent.** So new limit defaults for existing deployments cannot be pushed via `get_default_roles()` — absent field ⇒ `None` ⇒ the code must fall back to the global `Settings` default. No migrations framework: a field that does not exist on an old role document reads as the model default.
- Enforcement patterns to copy (all resolve "most permissive across the user's roles wins", fallback to global):
  1. Upload size: `resolve_upload_limits(user_roles)` in `src/api/routes/upload.py:493-534`:
```python
role_storage = RoleStorage()
for role_name in user_roles:
    role = await role_storage.get_by_name(role_name)
    if role and role.limits:
        value = getattr(role.limits, field_name, None)
        if value is not None:
            role_overrides[key] = max(role_overrides.get(key, value), value)
```
  2. Concurrency: `ConcurrencyLimiter.get_user_limits(roles)` (`src/infra/task/concurrency.py:136-165`) uses the batch `RoleStorage().get_by_names(roles)`.
  3. Storage quota: `UserStorageQuotaService.resolve_policy(user_id, roles)` (`user_storage.py:697-749`), precedence **user override > most permissive role > global default** (`source="user"|"role"|"global"`), roles capped at 32, invalid values logged and skipped, failure ⇒ `StorageReconciliationRequiredError` (fail closed). It reads `getattr(getattr(role, "limits", None), "storage_quota_mb", None)`.
  4. MCP per-role quota: `tests/test_mcp_role_quota.py` (`UNCONFIRMED` code path; not read).
- Per-user admin override exists only for storage (`quota_override_bytes` in `user_storage_usage`, endpoints `PUT /api/storage/admin/users/{user_id}/quota`).
- Adding KB limits end to end:
  1. `RoleLimits`: `max_kbs`, `max_docs_per_kb`, `kb_storage_mb`, `kb_max_file_mb` as `Optional[int] = Field(default=None, ge=0 or gt=0, ...)`. Use MiB for consistency (`storage_quota_mb`, `max_file_size_*`).
  2. A resolver `async def resolve_kb_limits(roles) -> KbLimits` (max across roles; `None` → `settings.KB_DEFAULT_*`); fail closed on error like `resolve_policy` or fail open like `resolve_upload_limits` (decide: quota ⇒ fail closed).
  3. Add inputs to `RolesPanel.tsx` form state **and** the `limits` builder, and fields to `RoleLimits` in `frontend/src/types/auth.ts`, plus i18n in `roles.*`. Otherwise editing a role wipes them (TL;DR 3).
  4. Add `Optional` fields to `RoleLimits` only; no change in `RoleStorage` (stores `model_dump()` wholesale).
  5. Tests: `tests/api/test_role_routes.py` style plus a resolver unit test.
- Permissions (new): add to `Permission` enum (`src/kernel/types.py:14-114`), `PERMISSION_METADATA` + `PERMISSION_GROUPS_CONFIG` (`src/kernel/schemas/permission.py:~246, ~395-425`, labels are Chinese; ungrouped permissions simply do not appear in the role editor), `RBACManager.get_default_roles()` for `user` (affects only fresh installs), and the frontend `Permission` enum (`frontend/src/types/auth.ts:78`). The admin role picks up `[p.value for p in Permission]` automatically.

---

## 5. Object storage

- Entry point for infra-layer code: `src/infra/storage/s3/service.py:542` `await get_or_init_storage() -> S3StorageService` (builds config from settings: `S3_ENABLED` true ⇒ provider from `S3_PROVIDER` (aws/aliyun/tencent/minio/custom/local); false ⇒ **LOCAL backend** at `settings.LOCAL_STORAGE_PATH` default `./uploads`). It reconfigures the singleton when the config changes. Re-exported from `src.infra.storage.s3` (`get_storage_service`, `init_storage`, `close_storage`, `S3Config`, `UploadResult`).
- `S3StorageService` methods (`service.py`):
```python
await storage.upload_bytes(data: bytes, folder: str, filename: str, content_type=None, metadata=None, *, skip_size_limit=False) -> UploadResult   # key = f"{folder}/{YYYYmmdd_HHMMSS}_{8hex}_{safe_filename}"  (line 198)
await storage.upload_to_key(data: bytes, key: str, content_type=None, metadata=None, *, skip_size_limit=False) -> UploadResult                    # caller-chosen key (line 229)
await storage.upload_stream_to_key(file: BinaryReadFile, key: str, content_type=None, metadata=None, *, skip_size_limit=False) -> UploadResult      # (line 254)
await storage.download_file(key) -> bytes                      # enforces internal size limit via get_size (line 358)
storage.download_stream(key, chunk_size=1 MiB) -> AsyncIterator[bytes]                                                                              # (line 380)
await storage.download_to_file(key, file, chunk_size=...) -> int                                                                                     # (line 370)
await storage.delete_file(key) -> bool ; await storage.file_exists(key) -> bool ; await storage.list_files(prefix) -> list[str]
await storage.get_file_url(key) ; await storage.get_presigned_url(key, expires=3600)
```
  `UploadResult(key, url, size, content_type, etag, last_modified)`. Uploads retry via `_retry_async`. Backends (`s3/backends/{local,minio,aliyun}.py`) all implement `get_size`, `download_range`, `download_stream`, `download_to_file`.
- **Size limits**: `S3Config.internal_max_upload_size` default 50 MiB (`S3_INTERNAL_UPLOAD_MAX_SIZE`) is enforced by `upload_*` and `download_file` unless `skip_size_limit=True`. `S3_MAX_FILE_SIZE` (10 MiB) is the public-upload validation size. For KB originals/Markdown larger than 50 MiB use `skip_size_limit=True` + the stream APIs after enforcing the role/global KB limit yourself.
- **Key conventions** in use: managed chat uploads `managed/chat/{user_id}/{uuid4().hex}[.ext]` (immutable, domain-tracked; `upload.py:~720`); agent-revealed files `revealed_files/{ts}_{8hex}_{name}` (`reveal_file_tool.py:263`); `avatars/{user_id}/...`; `generated-images/`, `tool_binaries/`, `revealed_projects/`. The legacy file proxy `GET /api/upload/file/{key:path}` (public prefix) only serves keys under `LEGACY_PUBLIC_OBJECT_PREFIXES` (`upload.py:62-67`) or avatars referenced by users, everything else is 404 (non-enumerating). So a KB key like `kb/{kb_id}/{doc_id}/original.ext` is **never publicly readable**; serve via an authenticated KB route. `delete_user_files` removes only prefixes `{user_id}` and `avatars/{user_id}` and only when S3 is enabled with a bucket (`service.py:306-352`).
- **Content URLs** (`src/infra/storage/content_url.py`): `build_content_url(base_url, file_id, name)` → `/api/storage/files/{file_id}/content/{filename}`; `content_disposition(name, "inline"|"attachment")` for headers (RFC 5987 for non-ASCII); `sanitize_content_filename`. Public regexp `^/api/storage/files/[0-9a-f]{32}/content(?:/.*)?$` (`auth.py:67-69`). Use these helpers for any KB download/preview response (spec `.trellis/spec/backend/managed-storage-url.md`: every branch needs `Content-Disposition`; never exempt a broader prefix).
- **Per-user accounting (user-storage-management)**: `UserStorageQuotaService` (`user_storage.py:611`; re-exported in `src/infra/storage/quota.py`, `user_storage_service.py`, `domain.py`):
  - Flow used by `POST /api/upload/file` (`upload.py:~720-790`): `prepare_create(user_id, source=StorageSource.CHAT, name, mime_type, category, size, content_hash, storage_key, idempotency_key, roles)` reserves quota and stages a `user_files` row → write the object → `complete_create(prepared, roles=...)`; on failure `compensate_create(user_id, operation_id)`. Exceptions: `StorageQuotaExceededError`, `StorageOperationBusyError`, `StorageReconciliationRequiredError`, … all `StorageDomainError` with `.code/.status_code`.
  - Collections: `user_storage_usage` (ledger, versioned, leased), `file_blobs`, `user_files`, `storage_operations`, `storage_operation_items`, `file_message_refs`.
  - Background reconcile/purge: `src/infra/storage/jobs.py` (`run_storage_maintenance`), started by `_schedule_storage_maintenance`.
  - Billable sources `StorageSource` = `CHAT, PROFILE_AVATAR, PERSONA_AVATAR, TEAM_AVATAR, SKILL, WECOM, LEGACY` (`schemas/storage.py:47-54`); billable ⇔ listable set (`STORAGE_LISTABLE_SOURCES`). Adding a KB source here would put KB bytes into "my files" quota, which the PRD forbids ⇒ build a separate `kb_usage` ledger (per-user `bytes`, `doc_count`, `kb_count` with atomic `$inc` + condition) and own reconcile.
  - Settings: `USER_STORAGE_ENFORCEMENT_ENABLED`, `USER_STORAGE_DEFAULT_QUOTA_MB`, `USER_STORAGE_WARNING_PERCENT`, `USER_STORAGE_MAINTENANCE_INTERVAL_SECONDS` (read via `getattr` in `main.py:325`; env-only).
- Local dev note: with `S3_ENABLED=false` the same `S3StorageService` API writes under `./uploads` (ok for tests); `deploy/docker-compose.yml` mounts `./uploads:/app/uploads` and k8s uses `LOCAL_STORAGE_PATH` (`k8s/kunxiaozhi.yaml:179-183`).

---

## 6. The agent-produced "files library" (`/files`) — copying into a KB

- Frontend: `frontend/src/services/api/revealedFile.ts` → `/api/files/revealed*` (router mounted at `/api/files`, `main.py:860`; handlers `src/api/routes/revealed_file.py`):
  - `GET /api/files/revealed` (params `page, page_size≤50, file_type, session_id, project_id, search, sort_by, sort_order, favorites_only`) → `{items, total, page, page_size}`; `GET /revealed/stats`, `/revealed/sessions`, `/revealed/grouped`; `PATCH /revealed/{file_id}/favorite`. **No download or get-by-id route exists** (the UI opens `item.url` = `{base}/api/upload/file/{file_key}`, public via the legacy prefix `revealed_files/`).
- Collection `revealed_files` (`RevealedFileStorage`, `src/infra/revealed_file/storage.py:26`; singleton `get_revealed_file_storage()` line 631). Document shape — written by `upsert_by_name` (line 124) from `src/infra/tool/reveal_file_tool.py:738-760`:
```text
_id: ObjectId            (serialized to "id": str in API via _serialize_item)
user_id: str             (== TokenPayload.sub)   <- ownership field
file_name: str           source: "reveal_file"     file_key: str  (S3 key "revealed_files/<ts>_<8hex>_<name>")
trace_id: str            created_at: datetime(UTC) (reset on re-reveal)   is_favorite: bool (protected)
file_type: str ("document"|"image"|...)   mime_type: str   file_size: int   url: str (proxy)
session_id, project_id, description, original_path
```
  Unique index `(user_id, file_key, source)`; also `(user_id, created_at)`, `(user_id, file_type)`, `session_id`, `(user_id, project_id)`. **No sha256/content hash is stored** and revealed objects are **not** in the managed `user_files` ledger (they are uploaded with `skip_size_limit=True` straight to storage).
- There is no `get_by_id`. To implement "copy into KB" add a method (e.g. `get_owned(user_id, file_id)`) in `RevealedFileStorage`:
```python
from bson import ObjectId; from bson.errors import InvalidId
try: oid = ObjectId(file_id)
except InvalidId: return None                               # route -> 404/400 ("Invalid file ID format", see revealed_file.py:108-113)
doc = await self.collection.find_one({"_id": oid, "user_id": user_id})   # ownership = filter on user_id; other users' ids -> None -> 404
```
  Then: `storage = await get_or_init_storage()`; `if not await storage.file_exists(doc["file_key"]): 404`; stream bytes with `storage.download_stream(file_key)` (or `download_file` when ≤ the 50 MiB internal limit), hash with `hashlib.sha256` while streaming (pattern: `_spool_upload_file_limited`, `upload.py:405-437`, uses `SpooledTemporaryFile` + `run_blocking_io`), and pass `name=doc["file_name"]`, `mime_type=doc.get("mime_type")`, `size=doc.get("file_size")` (re-measure after download; trust the stream length) into the KB document-create path. Reject dangerous suffixes first with the same helper (§8).
- Other user-file store (not requested but adjacent): the personal-settings "我的文件" are `user_files` rows: `UserStorageQuotaService().storage.get_owned_file(user_id, identifier, ...)` (`user_storage.py:407`) returns a doc; `UserFile` fields `file_id, user_id, blob_id, source, name, mime_type, size, category, content_hash (sha256), status` (`schemas/storage.py:230-254`); physical key via `storage.get_blob(blob_id)["storage_key"]`; content route `GET /api/storage/files/{file_id}/content[/{filename}]` (`routes/storage.py:235`). Here the sha256 is already known (`content_hash`).
- Frontend `/files` route and sidebar entry live in `frontend/src` (not examined; UNCONFIRMED exact component path).

---

## 7. Users

- Collection `users` (`UserStorage`, `src/infra/user/storage.py`; Pydantic `UserInDB(User(UserBase))` in `src/kernel/schemas/user.py`). `_id` = ObjectId → `id: str`. Fields: `username` (1-50 chars, **unique index**, = **employee ID / 工号**), `email` (unique), `avatar_url`, `roles: list[str]` (role **names**), `is_active`, `email_verified`, `must_change_password`, `credential_version`, `metadata: dict` (prefs: language, theme…), `created_at`, `updated_at`, oauth fields, `password_hash` (only in `UserInDB`). **There is no separate display-name field**: the name shown for a person is the `username`.
- Lookups: `await UserStorage().get_by_id(user_id)` (ObjectId; `None` on invalid id), `get_by_username(username)` (exact match, `find_one({"username": username})`, line 256), `get_by_email`, `list_users(skip, limit, is_active, search)` (regex on username/email, limit clamped to 100), `count_users`. `UserManager` wraps it (`src/infra/user/manager.py`).
- **Non-admin endpoint to list/look up users: none.** `/api/users/*` all require `user:read|write|delete` (`src/api/routes/user.py`). Other places that call `get_by_username`: WeCom handler only (`agent/wecom/handler.py:834,1298`). So "validate these employee IDs" must be a new KB route (e.g. `POST /api/knowledge-bases/resolve-users` with `usernames: list[str]` capped at ~50, case-exact like the unique index, returning `[{username, exists, is_active}]` and **never** `email/id/roles`). For stored shares keep the grantee `user_id` (ObjectId string) and a denormalized `username` for display; re-check `is_active` at access time.
- Because `TokenPayload.username` is refreshed from the DB in `get_current_user_base` (`deps.py:231`), it reflects renames. Usernames can be changed by users with `username:update` (`Permission.USERNAME_UPDATE`), so persist `user_id`, not `username`, as the share key.
- Role names ↔ `roles` collection (`name` unique); `TokenPayload.roles` is a list of role names.
- Deleting a user (`UserManager.delete_user`) does not clean any other collection (TL;DR 9).

---

## 8. Upload pipeline (`POST /api/upload/file`, `src/api/routes/upload.py:642-830`)

Order of operations (copy this order for the KB upload route):
1. `_reject_dangerous_upload_filename(file.filename)` **first** — before storage init, body reads, dedupe or writes (lines 133-147):
```python
def _final_extension(filename): basename = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]; ...  return basename.rsplit(".", 1)[-1].casefold()
def _reject_dangerous_upload_filename(filename):
    extension = _final_extension(filename)
    if extension in DANGEROUS_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Dangerous file extension '.{extension}' is not allowed")
```
   `DANGEROUS_UPLOAD_EXTENSIONS` (lines 85-130): `apk appimage asp aspx bat bin class cmd com cpl desktop docm dll dylib ear exe hta htm html jar js jse jsp lnk mjs msi msp php ps1 scr sh so svg vb vbe vbs pptm url war wsf xlsm xhtml`. Only the final suffix is checked, case-folded, no MIME/content sniffing (spec: `.trellis/spec/backend/upload-dangerous-extensions.md`). The functions are module-private (`_` prefix) in the routes module; importing them from a new route is fine, but better to lift them into `src/infra/upload/` (existing package) and re-import. Note that `html/htm/xhtml` collide with the PRD's html support and `json/csv/md/txt/xlsx/xls/pdf/docx/pptx/zip` are fine.
2. `storage = await get_or_init_storage()`.
3. Category/permission: `get_file_category(filename, content_type)` → `FileCategory` (`src/api/routes/file_type.py`, DOCUMENT allowlist at line 22: pdf doc docx xls xlsx ppt pptx txt md csv rtf odt ods odp epub json xml html htm … code files); needs `file:upload` or `file:upload:{type}`; extension must be in the category allowlist (`FILE_EXTENSIONS`).
4. Size: `resolve_upload_limits(current_user.roles)` (MiB per category; UNKNOWN = 10) ⇒ early `Content-Length` check ⇒ streamed enforcement in `_spool_upload_file_limited(file, max_size_bytes=..., max_size_mb=..., purpose="File")` (line 405): reads 1 MiB chunks (`UPLOAD_READ_CHUNK_SIZE`), updates `hashlib.sha256`, writes into a `SpooledTemporaryFile(max_size=2 MiB)` via `run_blocking_io`, raises `HTTPException(400, "File size exceeds maximum of {n}MB")` as soon as the limit is crossed; returns `SpooledUpload(file, sha256_hex, size)`; caller must `.close()` in `finally`.
5. Quota reserve (`prepare_create`) → `storage.upload_stream_to_key(file=spooled.file, key=..., content_type=..., metadata={...}, skip_size_limit=True)` → `complete_create`; exceptions compensate the reservation (`compensate_create`); response built by `_build_upload_response` (includes `key, url, name, type, mime_type, size, file_id, source, status, storage_usage, logical_url`).
6. Multipart/body limit: the request must be listed in `API_MULTIPART_UPLOAD_PATHS` (exact paths) or the prefixes in `_is_body_limit_exempt` (`main.py:105-120`); otherwise capped at 8 MiB (`API_REQUEST_BODY_MAX_BYTES`, 413 `{"detail": "Request body too large"}`). Multiple files per request or `/{kb_id}/documents` paths need a prefix/regex rule added there. `python-multipart` is installed; handlers use `file: UploadFile = File(...)`.
7. Dedupe: idempotency key header `x-idempotency-key` or `upload:{sha256}`; `prepared.reused` returns the existing file. For KB, dedupe by `(kb_id, sha256)` with a unique index.

---

## 9. Config, env, deploy

- Env settings class: `src/kernel/config/base.py` `class Settings(BaseSettings)` (pydantic-settings; `.env` at repo root via `PROJECT_ROOT/.env`, `extra="ignore"`). Re-exported by `src/kernel/config/__init__.py` and the shim `src/kernel/config.py`. Singleton `settings` (also `get_settings()`); consumers do `from src.kernel.config import settings`. Field name = env var name (case-insensitive), pydantic coerces types (`bool`, `int`, `list[str]` as JSON). Validators use `@field_validator(...)` (e.g. lines 337-348).
- Add env-only infra vars as plain fields near related ones, e.g.:
```python
QDRANT_URL: str = "http://localhost:6333"
QDRANT_API_KEY: str = ""            # optional
QDRANT_COLLECTION_PREFIX: str = "kb_"
KB_QUEUE_NAME: str = "kunxiaozhi:kb"
KB_WORKER_CONCURRENCY: int = 4
```
  Env-only ⇒ not in `SETTING_DEFINITIONS` ⇒ not admin-editable, env always wins (no DB override). If you also want admin control, add a definition **and** set `RESTART_REQUIRED_SETTINGS`; remember DB > env after first boot.
- `.env.example` (repo root, 297 lines, grouped with `# ===== Section =====` banners, inline `# comment` per var, e.g. `S3_ENABLED=false`, `REDIS_URL=...`). Append a `# Knowledge Base` block (QDRANT_URL, QDRANT_API_KEY, KB_*). `deploy/.env.example` is a 3-line stub (LLM config via UI).
- `deploy/docker-compose.yml` services: `redis` (redis:alpine, volume `redis-data`), `mongodb` (mongo:8.2.5, `--wiredTigerCacheSizeGB 0.5`, volume `mongodb-data`), `kunxiaozhi` (image `ghcr.io/123zhaozheng/kunxiaozhi:latest`, `depends_on: redis, mongodb` with `condition: service_started`, `environment:` list with `REDIS_URL=redis://redis:6379/0`, `MONGODB_URL=mongodb://mongodb:27017`, volumes `lamb-data:/app/data`, `./workspace`, `./uploads`; memory limit 2G). Named volumes at the bottom: `redis-data`, `mongodb-data`, `lamb-data`. To add Qdrant:
```yaml
  qdrant:
    image: qdrant/qdrant:v1.15.0        # pin; client is >=1.15.0
    container_name: kunxiaozhi-qdrant
    restart: always
    ports: ["6333:6333"]                 # REST; gRPC is 6334
    volumes: [qdrant-data:/qdrant/storage]
```
  plus `qdrant: {condition: service_started}` in `kunxiaozhi.depends_on`, env `QDRANT_URL=http://qdrant:6333`, and `qdrant-data:` under `volumes:`. (`deploy/docker-compose.phoenix.yml` is the model for an optional add-on compose file.) `deploy/deploy.sh` just does `mkdir -p workspace uploads; docker compose up -d`.
- k8s: `k8s/kunxiaozhi.yaml` defines Namespace, Secret, PVCs (`workspace-pvc`, `uploads-pvc`, `mongodb-pvc`), Service+StatefulSet `mongodb` (mongo:8.2.5), Deployment `kunxiaozhi` with env (`MONGODB_URL`, `REDIS_URL`, `TASK_BACKEND`, `ARQ_EMBEDDED_WORKER`, `S3_ENABLED`, `LOCAL_STORAGE_PATH`…); Redis is external (`REDIS_URL` env + `redis-password` secret, lines 163-170; no Redis manifest). Add a Qdrant StatefulSet/Service/PVC + `QDRANT_URL` env analogously; secrets in `k8s/kunxiaozhi-secret.yaml.example`.
- Dockerfile (58 lines): multi-stage; stage 1 `node:20-alpine` builds the frontend with pnpm; stage 2 `python:3.12-slim`, non-root `app` (uid 1000), `pip install uv`, `uv sync --frozen --no-dev --no-install-project` from `pyproject.toml` + `uv.lock`, copies `src/`, `main.py`, `static`; `CMD ["/app/.venv/bin/python", "main.py"]`. A new Python dependency only needs `pyproject.toml` + `uv.lock` (already done for `qdrant-client`; any parser libs for xlsx/etc. would be added the same way — keep the image light per the PRD, no docling/RapidOCR). CI: `.github/workflows/docker-build.yml`.
- MinerU parsing (reuse): `src/infra/tool/mineru_client.py` — `MinerUClient(base_url=settings.MINERU_API_BASE_URL, api_key=settings.MINERU_API_KEY or None).parse_bytes(filename, content, content_type) -> str (markdown)`, raising `MinerUError` (`read_document_tool.py:430-446`); `MINERU_EXTENSIONS` map at `read_document_tool.py:56`; settings `ENABLE_DOCUMENT_PARSE, MINERU_API_BASE_URL, MINERU_API_KEY, MINERU_BACKEND, MINERU_PARSE_EFFORT, MINERU_IMAGE_ANALYSIS, DOCUMENT_PARSE_MAX_BYTES (50 MiB), DOCUMENT_PARSE_MAX_OUTPUT_CHARS`. `charset_normalizer.from_bytes` is the text-decoding helper (`_decode_text`, line 241).

---

## 10. Tests and tooling

- Layout: `tests/api/` (route tests: `tests/api/test_*.py`, `tests/api/routes/`), `tests/infra/<domain>/`, `tests/kernel/{config,schemas}/`, `tests/agents/`, `tests/unit/{agents,infra}/`, `tests/events/`, `tests/persona_preset/`, plus top-level `tests/test_mcp_*.py`. `tests/conftest.py` only sets `os.environ["DEBUG"] = "false"`. `tests/api/routes/conftest.py` stubs `src.infra.backend` context if import fails. `tests/infra/scheduler/{conftest,fakes}.py` hold reusable in-memory fakes.
- pytest config (`pyproject.toml:110-113`): `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `pythonpath = ["."]`. `pytest-asyncio`, `pytest-cov` installed; **no `mongomock`/`mongomock-motor`**; no real Mongo/Redis in unit tests. Pattern: hand-written fakes — either fake Storage classes (`FakeStorage` in `tests/infra/scheduler/fakes.py`, installed with `monkeypatch.setattr(routes, "get_scheduled_task_storage", lambda: fakes.storage)`) or tiny `_FakeCollection` classes implementing just the used Motor methods (`find`, `find_one`, `update_one`, `to_list`, `create_index`…, e.g. `tests/infra/settings/test_settings_storage.py:10-47`, `tests/infra/folder/test_project_storage_limits.py`). Redis is faked similarly (`FakeRedis`, `tests/infra/scheduler/fakes.py:14-43`).
- API route test recipe (`tests/api/test_scheduled_task_routes.py:51-80`):
```python
app = FastAPI()
app.include_router(routes.router, prefix=BASE)
app.dependency_overrides[api_deps.get_current_user_required] = lambda: TokenPayload(
    sub="u1", username="u1", roles=["user"], permissions=["scheduled_task:use", "chat:write"])
async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
    resp = await client.post(BASE, json=BODY)
```
  Overriding `get_current_user_required` is enough even for `require_permissions(...)` guards (the permission checker depends on it, so give the fake user the needed `permissions`). Admin-only routes: pass `permissions=["knowledge_base:admin"]`. To test 401 you need the real middleware (`tests/api/test_auth_middleware_whitelist.py`). Tests build a bare `FastAPI()` and do not run `lifespan`/middleware.
- Commands (verified in this sandbox unless noted):
  - Subset: `cd /tmp/hoplite/workspace && .venv/bin/python -m pytest -q -p no:cacheprovider --import-mode=importlib tests/api/test_role_routes.py tests/infra/storage/test_content_url.py` → `25 passed`. Equivalent `uv run pytest ...` (Makefile `test`: `uv run pytest`; `uv` itself not re-verified). `--import-mode=importlib` is needed when collecting the whole `tests/` tree (duplicate basename `test_storage_limits.py`); subsets work without it in most cases (UNCONFIRMED).
  - Lint: `make lint` ⇒ `uv run ruff check .` (ruff 0.15.15 in `.venv`; pre-commit pins 0.9.10). Config `[tool.ruff]` line-length 100, `select = ["E","F","I","N","W"]`, `ignore = ["E501"]`, tests ignore `E402, F841`. Baseline: `ruff check src tests` ⇒ 2 pre-existing errors (`tests/infra/agent/test_wecom_session_owner.py:49`, `tests/infra/persona_preset/test_dify_kb_agent_options.py:64`). Format: `uv run ruff format .` (`make format`).
  - Types: `make typecheck` ⇒ `uv run mypy src/`; config `[tool.mypy]` (python 3.12, `ignore_missing_imports`, `check_untyped_defs`, `plugins = ["pydantic.mypy"]`, no `disallow_untyped_defs`). Baseline (run here with `.venv/bin/mypy src/`): **33 errors in 9 files** (`storage/user_storage.py` 21, `skill/builtin_copy.py` 3, `scheduler/locks.py` 3, `scheduler/drafts.py` 3, `tool/scheduled_task_tool.py` 2, `scheduler/storage.py` 2, `tool/mineru_client.py` 1, `agent/wecom/handler.py` 1, `api/routes/upload.py` 1). New code must add none.
  - Full baseline (from `.trellis/tasks/10-21-personal-knowledge-base/research/baseline-tests.md`): `uv run pytest -q -p no:cacheprovider --import-mode=importlib tests` ⇒ 11 failed / 2541 passed / 2 skipped (11 pre-existing failures listed there, e.g. `tests/infra/test_runtime_services.py` ×3 — relevant if you touch `start_runtime_services`).
  - Frontend typecheck: `pnpm exec tsc --noEmit -p .` (from `frontend/`), per the same baseline doc.
- Spec conventions (`.trellis/spec/backend/quality-guidelines.md`): annotate signatures; Pydantic API schemas; async I/O; Storage classes; `get_logger(__name__)`; `run_blocking_io` for CPU/blocking work; no `print`, no raw dict responses (some routes still return dicts), no silently swallowed exceptions; absolute imports from `src`.

---

## 11. Adjacent facts discovered (not asked, but needed)

- Existing KB-ish feature: Dify KB — settings `DIFY_KB_*` (`base.py:469-478`, category `DIFY`), tools `get_list_dify_knowledge_bases_tool` / `get_query_dify_knowledge_base_tool` registered in `src/infra/tool/internal_registry.py:12-62` (`build_internal_tools`, gated by `settings.DIFY_KB_ENABLED` and others), implementation `src/infra/tool/dify_kb_tool.py` (rerank at line 241, `/v1/rerank`). Personal-KB tools should register the same way; spec `.trellis/spec/backend/persona-runtime-and-dify-kb.md` (not read in full).
- `jieba>=0.42.1` is already a dependency (Chinese tokenization for BM25-style keyword scoring), as are `minio`, `oss2`, `boto3`, `arq`, `langchain-mongodb`.
- Native memory uses `$vectorSearch` that falls back to Python cosine (per PRD); do not depend on Mongo vector search.
- User-visible text on the frontend needs i18n in 5 locales (`frontend/src/i18n/locales/{en,zh,ja,ko,ru}.json`).

## Not found / UNCONFIRMED
- MCP per-role quota enforcement code (`tests/test_mcp_role_quota.py` exists; implementation not read).
- Exact frontend component for `/files` and how "我的文件" is rendered.
- Whether `uv run` works offline in this sandbox (used `.venv/bin/python -m pytest` instead).
- Behaviour of subsets of `tests/` without `--import-mode=importlib` in all directories.
