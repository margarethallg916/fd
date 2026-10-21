# Design: KB core（入库链路 + Qdrant 混合索引 + 列表/详情 UI）

> 子任务 10-21-kb-core-ingestion · 父任务 `10-21-personal-knowledge-base`（决策见父 prd.md）
> 研究出处：`../10-21-personal-knowledge-base/research/{kxz-backend-infra,kxz-backend-ai,kxz-frontend,yuxi-kb}.md`
> 本文件先写与代码约定无关的核心设计；§9「落地约定」在研究完成后补全。

## 1. 模块布局（后端）

```
src/infra/knowledge_base/
  __init__.py
  models.py        # 枚举 / pydantic 模型（KB、Document、Chunk、Task、请求响应）
  repository.py    # Mongo 集合句柄 + ensure_indexes()
  access.py        # 权限解析：resolve_kb_access(user, kb_id) -> KBAccess(role)
  service.py       # KB CRUD、文档上传/删除/重试、统计
  storage.py       # 原件 / 解析后 markdown 的对象存储 key 规则与读写
  parsing.py       # 按扩展名分派解析 -> ParsedDocument(markdown, meta)
  chunking.py      # 分块预设（general/qa/book/laws）-> list[Chunk]
  sparse.py        # jieba BM25 稀疏向量编码
  embedding.py     # 适配管理端 embedding 模型卡（批量、重试）
  vector_store.py  # Qdrant 封装（服务端 / 嵌入式本地模式）
  retrieval.py     # 混合检索 + rerank + 阈值
  queue.py         # Mongo 任务表、租约/心跳/取消/对账、worker 循环、arq 唤醒
  settings.py      # 管理端 knowledge_base 设置组
src/api/routes/knowledge_base.py   # /api/kb/...
```

## 2. 数据模型（Mongo，唯一可信来源）

### kb_bases
| 字段 | 说明 |
|---|---|
| `_id` | 字符串 id（与项目惯例一致，见 §9） |
| `owner_id` / `owner_username` | 库主 |
| `name` / `description` / `icon`(emoji 或 lucide 名) / `color` | 展示 |
| `chunk_preset` | `general` / `qa` / `book` / `laws` |
| `chunk_size` / `chunk_overlap` | 可选覆盖，空则用预设默认 |
| `embedding` | `{model_id, model_name, dim, collection}`：建库/首次索引时锁定；管理员换模型后 `index_outdated=true`，需重建 |
| `visibility` | `private` / `public`（PR2 使用；PR1 恒为 private） |
| `graph_enabled` | PR4 使用 |
| `stats` | `{doc_count, indexed_count, chunk_count, size_bytes}`：由 service 增量维护 + 对账重算 |
| `created_at` / `updated_at` | UTC |

### kb_members（PR1 只写 owner；PR2 扩展分享/订阅）
`{kb_id, user_id, username, role: owner|editor|viewer|subscriber, chat_enabled: bool, granted_by, created_at}`，唯一索引 `(kb_id, user_id)`。
「对话中启用」按成员各自保存（PR3 使用，PR1 建库时 owner 默认 `true`）。

### kb_documents
| 字段 | 说明 |
|---|---|
| `_id`, `kb_id`, `uploader_id` | |
| `filename`, `ext`, `mime`, `size`, `sha256` | 同库内 `(kb_id, sha256)` 去重：重复上传直接返回已有文档并标记 `duplicate=true` |
| `source` | `upload` / `files_library`(PR2) / `agent`(PR3) ；`source_ref` 来源 id |
| `object_key` / `markdown_key` | 原件 / 解析 markdown 在对象存储中的 key |
| `status` | 见状态机 |
| `error` | `{stage, message, at}` |
| `parse_method` | `mineru` / `pdf_text` / `local` |
| `chunk_count`, `line_count` | |
| `task_id` | 最近一次处理任务 |
| `created_at` / `updated_at` / `indexed_at` | |

### kb_chunks
`{_id(=f"{doc_id}:{index}"), kb_id, doc_id, index, text, start_line, end_line, headers: [str], char_count}`；索引 `(doc_id, index)`、`kb_id`。
用于分块预览与无需重新解析的重建索引；Qdrant point id 由 `uuid5(NAMESPACE, chunk_id)` 派生，保证幂等 upsert。

### kb_tasks（处理队列）
`{_id, kb_id, type: ingest|reindex, doc_ids, status: queued|running|succeeded|failed|cancelled, cancel_requested, progress:{total, done, failed, current_doc_id}, attempts, lease_owner, lease_until, heartbeat_at, error, created_by, created_at, started_at, finished_at}`；索引 `(status, created_at)`、`(kb_id, status)`、`lease_until`。

## 3. 文档状态机

```
uploaded ──► parsing ──► parsed ──► indexing ──► indexed
               │                      │
               ▼                      ▼
          error_parsing          error_indexing
```
- 上传成功写 `uploaded` 并加入 ingest 任务；worker 串行处理任务内文件：parse → chunk → embed → upsert。
- 重试：`error_parsing` 从解析重来；`error_indexing` 若已有 markdown/chunks 则只重建索引。
- 删除：先删 Qdrant 中 `doc_id` 的点，再删 chunks/对象存储/文档记录；任务中正在处理的文档被删除时 worker 跳过。
- 进程崩溃：对账把租约过期的 `running` 任务重新排队（attempts+1，超过 3 次判失败），任务内处于 `parsing/indexing` 的文档回退到可重试状态。

## 4. 队列（参考 Yuxi「PG 任务表 + arq 唤醒」，换成 Mongo）

- 入队：插入 `kb_tasks(status=queued)` → 通过 arq `kb` 队列投递 `kb_wake` 唤醒（Redis 不可用时只靠 worker 轮询兜底）。
- 领取：`find_one_and_update({status: queued} 或 {status: running, lease_until < now}, {$set: running, lease_owner, lease_until=now+60s}, sort created_at)`——原子操作保证多副本不重复领取。
- 并发：进程内最多 `concurrency`（默认 4，管理端可配）个任务同时运行；单任务内文件串行。
- 心跳：每处理完一个阶段续租 `lease_until`；每个文件开始前检查 `cancel_requested`。
- 对账：启动时 + 每 60s 扫描过期租约与卡在中间态的文档。
- 进度：每个文件状态变化写 Mongo，并向 KB 的相关用户推送 WS 事件 `kb.document.updated` / `kb.task.updated`；前端以轮询为主（有活跃任务时 5s，指数退避至 30s），收到 WS 事件立即刷新。

## 5. Qdrant 布局

- 连接：`QDRANT_URL`(+`QDRANT_API_KEY`) → `AsyncQdrantClient(url=...)`；未配置 → `AsyncQdrantClient(path=QDRANT_LOCAL_PATH 默认 data/qdrant)`（已验证本地模式支持 dense+sparse+RRF+过滤+按条件删除）。
- 集合：按 embedding 签名共享，`kb_chunks_{dim}_{sha1(model_name)[:8]}`；向量 `dense`(COSINE, dim) + 稀疏 `bm25`(Modifier.IDF)；服务端模式下为 `kb_id` 建 keyword 索引（`is_tenant=True`）、`doc_id` keyword 索引。
- payload：`{kb_id, doc_id, chunk_id, index, text, filename, start_line, end_line, headers}`。

## 6. 稀疏编码（中文 BM25）

- `jieba.lcut_for_search` + 小写 + 去标点/停用词；token → `crc32(token) & 0x7fffffff` 作为稀疏索引。
- 文档侧值 = BM25 TF 饱和项 `tf*(k1+1)/(tf+k1*(1-b+b*dl/avgdl))`，`k1=1.2, b=0.75, avgdl=256`；IDF 由 Qdrant `Modifier.IDF` 在服务端计算。
- 查询侧：去重 token，值 1.0。

## 7. 检索

`search(kb_ids, query, top_k=10, mode=hybrid|vector|keyword, rerank=auto, score_threshold=0)`：
1. 按库分组到其 embedding 集合（同签名的库合并查询，`kb_id MatchAny` 过滤）。
2. hybrid：dense / sparse 各 prefetch `max(top_k*4, 40)`，RRF 融合；vector/keyword 只用单路。
3. rerank：管理端配置了 rerank 模型且 `rerank!=false` 时，对候选前 N 条重排，分数替换为 rerank 分数，应用阈值。
4. 返回 `[{chunk_id, kb_id, kb_name, doc_id, filename, index, text, start_line, end_line, headers, score}]`。

## 8. API 契约（PR1）

前缀 `/api/kb`，全部需要登录；库访问校验见 `access.py`（PR1：owner 与 admin 可写，其他人 404）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/config` | `{enabled, presets[{key, chunk_size, chunk_overlap}], allowed_extensions, max_file_bytes, max_files_per_upload}` |
| GET | `` | `?scope=mine`（PR2 加 shared/subscribed）→ `{items: KB[]}` |
| POST | `` | `{name, description?, icon?, chunk_preset, chunk_size?, chunk_overlap?}` → KB |
| GET/PATCH/DELETE | `/{kb_id}` | 详情（含 `my_role`、`stats`、`active_task`）/ 修改 / 删除（异步清理索引） |
| GET | `/{kb_id}/documents` | `?status=&q=&page=&page_size=` → `{items, total}` |
| POST | `/{kb_id}/documents` | multipart `files[]` → `{documents, duplicates, task_id}` |
| POST | `/{kb_id}/documents/retry` | `{doc_ids}` → `{task_id}` |
| POST | `/{kb_id}/documents/delete` | `{doc_ids}` 批量删除 |
| GET | `/{kb_id}/documents/{doc_id}` | 文档详情 |
| GET | `/{kb_id}/documents/{doc_id}/content` | `?start_line=&end_line=` → 解析后 markdown 片段 |
| GET | `/{kb_id}/documents/{doc_id}/chunks` | `?page=&page_size=` |
| GET | `/{kb_id}/documents/{doc_id}/download` | 原件下载 |
| POST | `/{kb_id}/search` | `{query, top_k, mode, rerank, score_threshold}` → `{results}` |
| GET | `/{kb_id}/tasks` | `?active=true` → 任务进度 |
| POST | `/{kb_id}/tasks/{task_id}/cancel` | 取消 |
| POST | `/{kb_id}/reindex` | 模型变更后重建索引 |

管理端：`knowledge_base` 设置组 `{enabled, worker_concurrency, embedding_model_id, rerank_model_id, rerank_top_n, default_top_k, max_file_mb, max_files_per_upload}`（PR2 再加公开/审核/图谱开关与 RoleLimits 配额）。

## 9. 落地约定（研究完成后补全）

待补：路由注册、鉴权依赖、Mongo 句柄与 id 惯例、设置组定义、对象存储 API、MinerU/embedding/rerank 调用、arq 进程内 worker、WS 推送、前端导航/路由/组件/i18n/API 层。
