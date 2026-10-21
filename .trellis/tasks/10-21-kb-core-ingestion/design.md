# Design: KB core（入库链路 + Qdrant 混合索引 + 列表/详情 UI）

> 子任务 10-21-kb-core-ingestion · 父任务 `10-21-personal-knowledge-base`（决策见父 prd.md）
> 研究：`../10-21-personal-knowledge-base/research/{kxz-backend-infra,kxz-backend-ai,kxz-frontend,yuxi-kb,baseline-tests}.md`（下文 `[infra §n]` `[ai §n]` `[fe §n]` `[yuxi §n]` 指向对应章节）

## 0. 范围（PR1）

做：建库/改库/删库（仅库主）、单文件上传→自动排队解析→分块→嵌入→Qdrant 混合索引、文档列表/状态/重试/删除/下载/解析内容与分块预览、检索测试、重建索引、管理端 `knowledge_base` 设置组、左侧导航「知识库」入口。
不做（后续 PR）：分享/公开/订阅/配额(RoleLimits)/文件库导入（PR2）；agent 工具/@知识库/引用卡片（PR3）；图谱（PR4）。但数据模型预留 `kb_members`、`visibility`、`graph_enabled` 字段。

## 1. 模块布局

后端（新包 `src/infra/knowledge_base/`；schemas 放 `src/kernel/schemas/knowledge_base.py`，遵循 `[infra §1-2]`）：

| 文件 | 归属 | 职责 |
|---|---|---|
| `parsing.py` | 引擎 | 按扩展名分派解析 → `ParsedDocument` |
| `chunking/`（`__init__.py` + 移植自 Yuxi 的 `nlp.py`、`parsers/{general,qa,book,laws}.py`） | 引擎 | 分块预设 → `list[TextChunk]`（含行号） |
| `sparse.py` | 引擎 | jieba BM25 稀疏向量 |
| `embedding.py` | 引擎 | 解析管理端 embedding/rerank 模型卡，批量嵌入、rerank |
| `vector_store.py` | 引擎 | Qdrant 封装（服务端 / 嵌入式本地） |
| `fusion.py` | 引擎 | 加权融合、阈值、rerank 合并（纯函数） |
| `storage.py` | 领域 | Mongo 集合（`KnowledgeBaseStorage`）+ `ensure_indexes()` |
| `access.py` | 领域 | `resolve_kb_access(user, kb_id) -> KBAccess` |
| `service.py` | 领域 | KB/文档 CRUD、上传落盘、重试、删除、重建、检索编排 |
| `objects.py` | 领域 | 对象存储 key 规则与读写 |
| `queue.py` | 领域 | `kb_tasks` 任务表、领取/租约/心跳/取消/对账、发布（arq 或本地） |
| `pipeline.py` | 领域 | 单文档处理：解析→分块→嵌入→写索引（CAS 状态迁移） |
| `runtime.py` | 领域 | 嵌入式 arq Worker（队列 `KB_ARQ_QUEUE_NAME`）/本地 runner、对账循环启动停止 |
| `src/api/routes/knowledge_base.py` | 领域 | `/api/kb` 路由 |

引擎模块的**接口桩**已由主代理写好（函数签名 + docstring，函数体 `raise NotImplementedError`）；引擎实现者只填实现，不改签名；领域实现者按签名调用，测试中 monkeypatch。

前端（`frontend/src/components/knowledge/` 新目录，遵循 `[fe §2-6, §12]`）：见 §8。

## 2. 数据模型（Mongo，唯一可信来源；无事务，所有状态迁移用带条件的 `find_one_and_update` / `update_one`）

ID：`uuid4().hex` 作 `_id`；`owner_id`/`user_id` 一律存 `TokenPayload.sub`；时间 `utc_now()`（tz-aware UTC）。Pydantic 模型 `id: str = Field(alias="_id")` + `populate_by_name`。

### kb_bases
`{_id, owner_id, owner_username, name(1-64), description(≤500), icon(lucide 名，默认 "library-big"), chunk_preset, chunk_token_num(100-2048, 默认 512), overlap_percent(0-50, 默认 0), embedding: null | {model_id, model_name, dim, collection}, visibility: "private", graph_enabled: false, status: "active"|"deleting", created_at, updated_at}`
索引：`(owner_id, updated_at desc)`、`status`。同一用户库名不强制唯一。

### kb_members（PR1 只写 owner 行；PR2 扩展）
`{_id, kb_id, user_id, username, role: owner|editor|viewer|subscriber, chat_enabled: true, granted_by, created_at}`；唯一索引 `(kb_id, user_id)`，索引 `(user_id, role)`。

### kb_documents
`{_id, kb_id, uploader_id, uploader_username, filename, ext, mime, size, sha256, source: "upload", source_ref: null, object_key, markdown_key: null, status, error: null | {stage: "parse"|"index", message, at}, parse_method: null|"mineru"|"pdf_text"|"local", chunk_count: 0, token_count: 0, line_count: 0, processing_task_id: null, created_at, updated_at, indexed_at}`
索引：唯一 `(kb_id, sha256)`；`(kb_id, created_at desc)`；`(kb_id, status)`；`(kb_id, filename)`。

### kb_chunks
`{_id: f"{doc_id}:{index}", kb_id, doc_id, index, text, start_line, end_line, token_count}`；索引 `(doc_id, index)`、`kb_id`。重建索引时先删该文档旧 chunks。

### kb_tasks
`{_id, kb_id, type: "ingest", created_by, doc_ids: [..], status: queued|running|succeeded|failed|cancelled, cancel_requested: false, progress: {total, done, failed, current_doc_id}, attempts: 0, lease_owner, lease_until, heartbeat_at, error, created_at, started_at, finished_at, last_published_at}`
索引：**部分唯一索引** `(kb_id, type)` where `status == "queued"`（保证每库最多一个排队中的 ingest 任务，上传时并发 upsert 撞 `DuplicateKeyError` 重试一次）；`(status, created_at)`；`(kb_id, created_at desc)`；`(created_by, status)`。终态任务每库保留最近 20 条（对账时清理）。

## 3. 文档状态机（照搬 Yuxi `[yuxi §1.4]`）

```
uploaded ─► parsing ─► parsed ─► indexing ─► indexed
              │                     │
              ▼                     ▼
        error_parsing         error_indexing
```
- 每次迁移都是 CAS：`update_one({_id, status: {$in: allowed}}, {$set: {status: next, processing_task_id: task_id, ...}})`，`modified_count == 0` 视为文档已被删除/被他人处理 → 跳过。
- `parsing → parsed` 时写 `markdown_key`、`parse_method`、`line_count`；`indexing → indexed` 时写 `chunk_count`、`token_count`、`indexed_at`；失败写 `error`。
- 重试：`error_parsing → uploaded`，`error_indexing → parsed`；重建：`indexed|error_indexing → parsed`（重新按当前库分块设置切分并嵌入）。
- 删除文档：直接删记录 + chunks + 向量 + 对象；pipeline 写回时 CAS 失败即跳过，并在写完向量后复查文档是否存在，不存在则按 `doc_id` 删向量（防孤儿）；检索结果回填时丢弃文档已不存在的命中 `[yuxi §5.3]`。

## 4. 队列（Yuxi「任务表 + arq 唤醒」的 Mongo 版 `[yuxi §4]` `[ai §4]`）

- **入队（上传/重试/重建）**：`find_one_and_update({kb_id, type:"ingest", status:"queued"}, {$addToSet:{doc_ids:{$each: ids}}, $setOnInsert:{...}}, upsert=True)` → `publish(task_id)`。排队中的任务持续合并新上传的文件，相当于 Yuxi 的「一批一个任务」。
- **发布**：`TASK_BACKEND == "arq" and ARQ_EMBEDDED_WORKER` 时，用独立 pool（`default_queue_name=KB_ARQ_QUEUE_NAME`）`enqueue_job("kb_process_task", task_id)`（不传 `_job_id`，重复消息由领取 CAS 去重）；否则交给本地 runner（`asyncio.create_task` + `Semaphore(KB_WORKER_CONCURRENCY)`）。发布失败只记日志，对账会补发。
- **执行 `process_kb_task(task_id)`**：
  1. 读任务，终态直接返回；`cancel_requested` 且 queued → 置 `cancelled`。
  2. 公平性：同一 `created_by` 已有租约有效的 running 任务 → 不领取直接返回（任务保持 queued，等前一个任务结束后补发）。
  3. 领取 CAS：`{_id, status:"queued"} → {status:"running", lease_owner, lease_until: now+60s, heartbeat_at, started_at, progress.total: len(doc_ids)}`，`$inc attempts`。
  4. 心跳协程每 15s 续租（过滤 `lease_owner`）；续租失败或 `cancel_requested` → 取消执行。
  5. 文档**串行**处理（`pipeline.process_document`），每个文件前检查取消；单文件异常只影响该文件状态。每处理完一个文件更新 `progress` 并推 WS。
  6. 结束：`succeeded`（`progress.failed` 记录失败数）/ `cancelled`；`finally` 补发最早的若干 queued 任务。
- **arq Worker**：`Worker([kb_process_task], queue_name=KB_ARQ_QUEUE_NAME, redis_settings=build_arq_redis_settings(settings), handle_signals=False, max_jobs=KB_WORKER_CONCURRENCY, job_timeout=KB_JOB_TIMEOUT_SECONDS(7200), keep_result=0, max_tries=1)`，在 `src/infra/runtime_services.py` 中与 `start_arq_runtime()` 并列启动/停止；`KB_WORKER_CONCURRENCY` 为 restart-required 设置。
- **对账（每 30s，lifespan 后台任务，模式见 `[infra §1 Lifespan]` `_schedule_storage_maintenance`）**：
  - running 且 `lease_until < now`：`attempts < 3` → 回到 queued（其文档 `parsing→uploaded`、`indexing→parsed`）并补发；否则 `failed`，文档 `parsing→error_parsing`、`indexing→error_indexing`。（与 Yuxi 不同：我们的解析/索引幂等——chunk id 与 point id 确定性派生——所以允许有限次自动重试，避免发版重启后用户要手动重试。）
  - queued 且 `last_published_at < now-30s` → 补发。
  - `uploaded/parsed` 状态、超过 2 分钟且不在任何 queued/running 任务中的文档 → 入队（插入文档与入队之间崩溃的兜底）。
  - `status == "deleting"` 的库 → 继续清理。
  - 清理终态任务（每库保留 20 条）。

## 5. 解析（`parsing.py`，决策 4 + `[yuxi §2.2]` `[ai §1]`）

| 扩展名 | 方式 |
|---|---|
| `.pdf` | MinerU 可用（`ENABLE_DOCUMENT_PARSE` 且 `MINERU_API_BASE_URL` 非空）→ `MinerUClient.parse_bytes`；不可用或 MinerU 失败 → `pypdf` 文本层；文本层为空 → `ParseError("PDF 没有可提取的文本层，需要管理员启用文档解析服务")` |
| `.docx` | MinerU 可用 → MinerU；否则/失败 → `python-docx`（段落 + 表格转 md 表格，标题样式转 `#`） |
| `.pptx` | 仅 MinerU；不可用 → `ParseError("需要管理员启用文档解析服务")` |
| `.png .jpg .jpeg .webp .bmp .tif .tiff` | 仅 MinerU；同上 |
| `.txt .md` | `charset_normalizer.from_bytes(...).best()` 解码 |
| `.html .htm` | 解码后 `markdownify(heading_style="ATX")`，去掉 script/style |
| `.json` | `json.dumps(indent=2, ensure_ascii=False)` 包在 ```` ```json ```` 中（解析失败按纯文本） |
| `.csv` | stdlib `csv`（先解码），每个 sheet 一个 `## 标题`，每行一行 `表头1: 值1 \| 表头2: 值2` |
| `.xlsx` | `openpyxl`(read_only, data_only)，同 csv 的行格式，每个 sheet `## {sheet}` |
| `.xls` | `xlrd`，同上 |
| `.zip` | 只取 md：优先 `full.md`，否则第一个 `.md`；拒绝绝对路径/`..`；成员数 ≤ 1000、解压总量 ≤ 64 MiB；无 md → `ParseError("压缩包中未找到 .md 文件")` |

阻塞解析统一走 `run_blocking_io`。新增依赖：`pypdf`、`openpyxl`、`xlrd`、`markdownify`、`python-docx`（不引入 docling/RapidOCR/pandas）。
KB 上传使用**白名单**（上表扩展名）；html 只作为文本解析，原件下载一律 `Content-Disposition: attachment` + `application/octet-stream` + `X-Content-Type-Options: nosniff`，因此不受主上传 denylist 限制（在 spec 中写明）。

## 6. 分块（`chunking/`，移植 Yuxi `ragflow_like` `[yuxi §3]`，MIT 许可，文件头注明出处）

- 预设：`general`（默认）、`qa`、`book`、`laws`；不移植 semantic/separator。
- 参数：`chunk_token_num`（默认 512，范围 100-2048）、`overlap_percent`（默认 0，范围 0-50）、`delimiter` 固定 `"\n"`。
- token 计数沿用 Yuxi 近似：`len(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", text))`。
- 行号：在 markdown 中从游标处 `str.find` 定位 chunk 文本（失败时用 chunk 第一行定位），按行起始偏移二分换算 `start_line/end_line`（1-based，含端点）；book/laws/qa 改写过的文本定位失败时为 `None`。
- 空 chunk 丢弃，index 连续重排。

## 7. 向量与检索

### 7.1 Qdrant（`vector_store.py`）
- `QDRANT_URL` 非空 → `AsyncQdrantClient(url, api_key=QDRANT_API_KEY or None)`；否则 `AsyncQdrantClient(path=QDRANT_LOCAL_PATH)`（默认 `data/qdrant`，compose 已挂载 `/app/data`）。单例，lifespan 关闭时 `close()`。
- 集合按嵌入签名共享：`kb_{dim}_{sha1(model_name)[:8]}`；`dense`(COSINE) + 稀疏 `bm25`(`Modifier.IDF`)；服务端模式下建 `kb_id`(keyword, `is_tenant=True`)、`doc_id`(keyword) payload 索引（本地模式跳过）。
- point id = `uuid5(NAMESPACE_URL, f"kb-chunk:{chunk_id}")`；payload `{kb_id, doc_id, chunk_id, index, text, start_line, end_line}`。
- 操作：`ensure_collection`、`upsert_chunks`（分批 128，3 次重试）、`delete_by_doc`、`delete_by_kb`、`search_dense`、`search_sparse`。

### 7.2 稀疏编码（`sparse.py`）
`jieba.lcut_for_search` → 小写 → 丢弃纯标点/空白与内置停用词 → 索引 `zlib.crc32(token.encode()) & 0x7FFFFFFF`；文档侧值 = BM25 TF 饱和 `tf*(k1+1)/(tf+k1*(1-b+b*dl/avgdl))`（k1=1.2, b=0.75, avgdl=256），IDF 由 Qdrant 计算；查询侧去重后值 1.0。jieba 首次加载放 `run_blocking_io`。

### 7.3 嵌入 / rerank（`embedding.py`）
- 模型卡：`KB_EMBEDDING_MODEL_ID or NATIVE_MEMORY_EMBEDDING_MODEL_ID` → `LLMClient.get_card_config(id, kind="embedding")`；rerank 用 `KB_RERANK_MODEL_ID`（空则不 rerank）。
- `api_base` 归一化：去掉结尾的 `/`、`/v1`、`/v1/embeddings`（rerank 同理 `/v1/rerank`），统一 POST `{base}/v1/embeddings`、`{base}/v1/rerank` `[ai §2.3-2.4]`。
- 批量：`input` 为列表，批大小 `KB_EMBEDDING_BATCH_SIZE`（默认 32），按 `index` 排序；429/5xx/网络错误指数退避重试 3 次；维度取首个结果长度。
- rerank 失败 → 返回 `None`，调用方退化为原顺序。

### 7.4 检索编排（`service.search` + `fusion.py`）
参数：`query`、`top_k`(默认 10，1-50)、`mode`(`hybrid` 默认 / `vector` / `keyword`)、`use_rerank`(`null`=有 rerank 模型就用)、`score_threshold`(默认 0，0-1)、`vector_weight`(默认 0.7)。
1. 按 `kb.embedding.collection` 分组（同集合多库用 `kb_id MatchAny`），未建索引的库跳过。
2. 召回数 `recall = max(50, top_k)`（开 rerank 时）否则 `top_k*2`。dense 与 sparse 分别查询。
3. hybrid：sparse 分数在结果集内 min-max 归一化，`score = w*dense + (1-w)*sparse_norm`（缺一路记 0）——对齐 Yuxi 的 0.7/0.3 加权而非 RRF，分数可直接展示为百分比。
4. `score_threshold` 作用于 vector/hybrid 的融合分（keyword 不过滤）。
5. rerank：对候选文本调用 rerank，分数替换为 `relevance_score`，按其排序；失败退化。
6. 回填：从 `kb_documents` 批量取 filename（丢弃孤儿），返回 `[{chunk_id, kb_id, kb_name, doc_id, filename, index, text, start_line, end_line, score, rerank_score?}]` 截断到 `top_k`。
7. 库的嵌入签名与当前模型卡不一致（卡被删除或模型名/维度变化）→ 该库退化为 keyword 并在响应 `warnings` 中返回 `index_outdated`。

## 8. API 契约（PR1，前缀 `/api/kb`，全部需要登录；路由全部声明在一个 `router` 上）

通用：KB 不存在或无权 → 404 `{"detail": "知识库不存在"}`；功能关闭 → 403 `{"detail": {"code": "kb_disabled", "message": "知识库功能未开启"}}`；校验错误 400 `{"detail": "..."}`。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| GET | `/config` | – | `KBConfig {enabled, ready, not_ready_reason: null\|"embedding_model_missing", presets: [{key, chunk_token_num, overlap_percent}], allowed_extensions: [".pdf", ...], mineru_enabled, max_file_mb, rerank_available}` |
| GET | `` | `?scope=mine` | `{items: KnowledgeBase[]}` |
| POST | `` | `KBCreate {name, description?, icon?, chunk_preset?, chunk_token_num?, overlap_percent?}` | 201 `KnowledgeBase` |
| GET | `/{kb_id}` | – | `KnowledgeBase` |
| PATCH | `/{kb_id}` | `KBUpdate`（同上字段，全部可选） | `KnowledgeBase` |
| DELETE | `/{kb_id}` | – | 204（标记 deleting，后台清理） |
| GET | `/{kb_id}/documents` | `?status=&q=&page=1&page_size=50(≤200)` | `{items: KBDocument[], total, page, page_size}` |
| POST | `/{kb_id}/documents/upload` | multipart `file` | 201 `{document: KBDocument, duplicate: bool}`（重复返回已有文档，200） |
| POST | `/{kb_id}/documents/retry` | `{doc_ids: []}`（空=全部失败文档） | `{task_id, count}` |
| POST | `/{kb_id}/documents/delete` | `{doc_ids: [] (1-500)}` | `{deleted: n}` |
| GET | `/{kb_id}/documents/{doc_id}` | – | `KBDocument` |
| GET | `/{kb_id}/documents/{doc_id}/content` | `?offset=1&limit=400(≤2000)` | `{doc_id, filename, total_lines, offset, lines: [{n, text}], has_more}` |
| GET | `/{kb_id}/documents/{doc_id}/chunks` | `?page=1&page_size=20(≤100)` | `{items: [{chunk_id, index, text, start_line, end_line, token_count}], total, page, page_size}` |
| GET | `/{kb_id}/documents/{doc_id}/download` | – | 原件流，`Content-Disposition: attachment`（`content_disposition()` helper） |
| POST | `/{kb_id}/search` | `KBSearchRequest`（§7.4） | `{results: KBSearchHit[], warnings: string[], took_ms}` |
| GET | `/{kb_id}/tasks` | `?active=true` | `{items: KBTask[]}` |
| POST | `/{kb_id}/tasks/{task_id}/cancel` | – | `KBTask` |
| POST | `/{kb_id}/reindex` | – | `{task_id, count}` |

`KnowledgeBase`：`{id, name, description, icon, owner_id, owner_username, my_role: "owner", chunk_preset, chunk_token_num, overlap_percent, visibility, graph_enabled, embedding: {model_name, dim} | null, index_outdated: bool, stats: {doc_count, indexed_count, processing_count, failed_count, chunk_count, size_bytes}, active_task: KBTask | null, created_at, updated_at}`（stats 由按 `(kb_id, status)` 聚合实时计算，不存冗余计数）。
`KBDocument`：`{id, kb_id, filename, ext, mime, size, status, error, parse_method, chunk_count, token_count, line_count, uploader_username, source, created_at, updated_at, indexed_at}`。
`KBTask`：`{id, kb_id, type, status, progress: {total, done, failed, current_doc_id}, attempts, error, created_at, started_at, finished_at}`。

上传：先校验扩展名白名单与大小（`KB_MAX_FILE_MB`，Content-Length 预检 + 流式计数，复用 `_spool_upload_file_limited` 模式 `[infra §8]`），流式计算 sha256；`(kb_id, sha256)` 已存在 → 返回 `{document, duplicate: true}`；否则写对象 `kb/{kb_id}/{doc_id}/original{ext}`（`upload_stream_to_key(..., skip_size_limit=True)`）→ 插入文档（唯一索引冲突视为重复）→ 入队。
必须把 `^/api/kb/[^/]+/documents/upload$` 加入 `src/api/main.py` 的 body-limit 豁免 `[infra §8.6]`，并在 `nginx/nginx.conf` 增加对应 location（`client_max_body_size 200m`，其余参数照 `/api/upload/`）`[fe §0.7]`。

WS（尽力而为）：文档状态变化/任务进度 → `send_to_user_with_broadcast(member_user_id, {"type": "kb:document_updated"|"kb:task_updated", "data": {kb_id, doc_id?, status?, task_id?, progress?}})`，推给该库所有成员（PR1 即库主）。

## 9. 管理端设置（新分类 `SettingCategory.KNOWLEDGE_BASE = "knowledge_base"`，`[infra §3]`）

| key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `KB_ENABLED` | BOOLEAN | `True` | `frontend_visible: True`（前端导航开关） |
| `KB_EMBEDDING_MODEL_ID` | STRING | `""` | 模型卡（embedding）；空则回退 `NATIVE_MEMORY_EMBEDDING_MODEL_ID`；加入 `_ALLOW_EMPTY_STRING_SETTINGS` |
| `KB_RERANK_MODEL_ID` | STRING | `""` | 模型卡（rerank）；空=不 rerank；同上 |
| `KB_WORKER_CONCURRENCY` | NUMBER | `4` | 1-16，`RESTART_REQUIRED_SETTINGS` |
| `KB_MAX_FILE_MB` | NUMBER | `100` | 1-1024，单文件上限（PR2 由 RoleLimits 覆盖） |
| `KB_EMBEDDING_BATCH_SIZE` | NUMBER | `32` | 1-256 |

均 `depends_on: "KB_ENABLED"`（`KB_ENABLED` 自身除外），`Settings` 中同名同默认值字段，测试对齐 `tests/kernel/config/test_scheduled_task_governance_definitions.py`。
仅环境变量（`Settings` 字段、不进 definitions）：`QDRANT_URL=""`、`QDRANT_API_KEY=""`、`QDRANT_LOCAL_PATH="data/qdrant"`、`KB_ARQ_QUEUE_NAME="kunxiaozhi:kb"`、`KB_JOB_TIMEOUT_SECONDS=7200`。
部署：`deploy/docker-compose.yml` 增加 `qdrant` 服务（`qdrant/qdrant:v1.15.5`，卷 `qdrant-data`）并给应用设 `QDRANT_URL=http://qdrant:6333`；`.env.example` 增加 `# ===== Knowledge Base =====` 段；`k8s/` 可选不改（本地模式可用）。

## 10. 前端（`[fe §2-8, §12]`）

- 导航：`TabType` 加 `"knowledge"`；`TabContent` 懒加载 `KnowledgeBasePanel`；`App.tsx` 路由 `/knowledge/:kbId?`（单路由 + 可选参数，`ProtectedRoute` 无额外权限，仅登录）；`SessionListContent`/`SidebarRail`/`SessionSidebar` 增加「知识库」按钮（图标 `LibraryBig`，`is-active` 用 `pathname.startsWith("/knowledge")`），受 `SettingsContext.enableKnowledgeBase`（`KB_ENABLED`）控制；`SidebarSkeleton` 加一行；i18n `nav.knowledge`、`seo.knowledge.*`。
- `services/api/knowledgeBase.ts`：`knowledgeBaseApi` + 全部类型（snake_case），上传用 XHR（照 `services/api/upload.ts` 结构：token 刷新、401 重试一次、`onProgress`、`abort`）。
- `KnowledgeBasePanel`：无 `kbId` → 列表页（`PanelHeader` + 搜索 + 「新建知识库」；tablist `我的知识库`（PR1 只有这一项，tablist 结构保留以便 PR2 增加「共享知识库/知识广场」）；卡片网格 `auto-grid-cols`，卡片用 `.scb` 样式，展示图标、名称、描述、`文档数 · 分块数 · 大小`、处理中徽标、更多菜单（编辑/删除）；空状态；`config.ready=false` 时顶部提示「管理员尚未配置向量模型，暂时无法入库」）。有 `kbId` → 详情页。
- 新建/编辑对话框（单步，模态样式照 `TaskEditorDialog`）：名称*、描述、分块预设（4 个卡片式单选，带说明）、「高级」折叠区（块大小 100-2048、重叠 0-50%）；无任何模型选项。
- 详情页：头部（返回、图标、名称、描述、统计、`设置` 按钮）+ tablist `文档` / `检索测试` / `设置`（`?tab=`）：
  - 文档：工具栏（上传按钮、文件名搜索、状态筛选、刷新）；表格列 文件名（类型图标）/ 状态徽标（错误态 Tooltip 显示 `error.message`，且可点击重试）/ 分块数 / 大小 / 上传时间 / 操作（查看、下载、重试、删除）；多选批量重试/删除（`Checkbox`）；分页 `Pagination`；活跃任务进度条（`已处理 x/y`、取消按钮）；`index_outdated` 时提示条 + 「重建索引」。
  - 状态文案：uploaded=排队中、parsing=解析中、parsed=待入库、indexing=入库中、indexed=已完成、error_parsing=解析失败、error_indexing=入库失败。
  - 上传对话框：拖拽/选择多个文件，前端校验扩展名与大小，并发 3 个 XHR，单文件进度、重复/失败标记，完成后刷新。
  - 文档详情抽屉（`EditorSidebar width="wide"`）：`解析内容`（带行号，分页加载）/ `分块`（index、行号范围、token 数、文本）。
  - 检索测试：输入框（Enter 搜索）、模式选择、top_k、rerank 开关（`rerank_available` 为 false 时禁用）、阈值；结果卡片：序号、得分百分比（rerank 分）、文件名、行号范围、分块文本，点击打开文档详情定位到该行。
  - 设置：名称/描述/分块设置（修改分块设置后提示「仅对新入库文档生效，可点击重建索引应用到全部」）、危险区删除（`ConfirmDialog`）。
- 轮询：存在处理中文档或活跃任务时启动链式 `setTimeout`，基础 5s，结果无变化时 ×1.5 退避至 30s，变化时重置，无处理中时停止；提交上传/重试后 1s 立即刷新一次。`useWebSocket` 增加 `onKbEvent`（`type.startsWith("kb:")`）分支，详情页挂载 `useWebSocket({enabled, onKbEvent})` 收到事件立即刷新（加速，轮询兜底）。
- 管理端：`types/settings.ts` 分类联合、`SettingsPanel.constants.ts`（`CATEGORY_ORDER`、`MODEL_CONFIG_SETTING_KEYS`、`MODEL_CARD_KIND_FILTER`: `KB_EMBEDDING_MODEL_ID`→embedding、`KB_RERANK_MODEL_ID`→rerank）、`SettingsPanel.tsx` 分类标签；i18n `categories.knowledge_base`、`settingDesc.KB_*`。
- `utils/backendErrors.ts` 加 `kb_disabled` 映射；i18n 顶层命名空间 `knowledge.*`，zh/en 完整，ja/ko/ru 不加（en 兜底）；新增 `src/i18n/__tests__/knowledgeKeys.test.ts`（en/zh 键一致、无占位）与源码契约测试（无渐变 banner、导航 is-active）。
