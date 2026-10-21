# 个人知识库功能：开发交接文档

> 更新时间：2026-10-21 · 交接人：Hoplite · 分支：`hoplite/poteidaia-708f791c`（PR 目标 `main`）
> 读完本文就能接着干，**不用再通读昆小智和 Yuxi 两个仓库**。需要细节时查 §11 的研究文档，里面都写了文件路径和行号。

---

## 0. 一句话现状

PR1（核心入库链路、Qdrant 混合检索、知识库列表/详情 UI、管理端设置）的代码**已经写完**，单测、lint、类型检查基本都过了（还剩 1 个 mypy 错误）。但下面三件事**没做**：
- 独立复核（Trellis W2 check）
- 本地端到端手工验证
- spec 沉淀

PR2（分享、订阅、配额、文件库导入）、PR3（昆小智工具与引用）、PR4（图谱）**只写好了设计文档，代码还没开始**。

---

## 1. 需求与已拍板的决策（不要再问用户）

目标：在左侧导航加一个「知识库」入口，做成类似 IMA 的个人知识库。
- 每人可以建多个库，数量等限制由管理员管控。
- 上传文档后自动解析、分块、嵌入、建索引。
- 可以按工号分享，也可以公开到「知识广场」让别人订阅。
- 昆小智能检索知识库并给出引用来源。
- 模型、OCR、Embedding、Rerank 全部在管理端配置，用户侧看不到。

| # | 决策 |
|---|---|
| 1 | 在昆小智代码上改。本仓库 `main` = `123zhaozheng/kunxiaozhi` main @ `1498b534`（已导入），开发分支通过 PR 合入 `main` |
| 2 | 向量库用 **Qdrant**。配置了 `QDRANT_URL` 就连服务端，否则用 qdrant-client 嵌入式本地模式。**Mongo 是唯一可信来源**，Qdrant 只是派生索引 |
| 3 | 图谱**不加新服务**：三元组存 Mongo，图增强检索在应用内完成。按库手动开启，用管理端配置的 LLM 抽取 |
| 4 | 解析：pdf/docx/pptx/图片走管理端已配置的 MinerU。MinerU 不可用时，pdf 退回文本层，docx 退回 python-docx。txt/md/html/json/csv/xlsx/xls 本地解析，zip 只支持内含 md。不引入 docling/RapidOCR |
| 5 | 建库时选分块预设（通用/问答/书籍/法规），块大小和重叠放在「高级」里。用户看不到模型和 OCR 选项 |
| 6 | 分享角色：查看者（检索 + 对话里用）、编辑者（可上传/删除文档，不能删库，不能改分享）。分享后直接出现在对方的「共享知识库」并发站内通知，不需要对方接受。只按工号分享和公开，不做链接分享 |
| 7 | 选人：输入或粘贴多个工号，后端校验用户是否存在后回显。本系统里姓名就是工号，不开放通讯录搜索 |
| 8 | 公开默认不审核。管理端有「公开需审核」开关和「下架」。订阅者只读，库主取消公开后订阅者立即失去访问 |
| 9 | 配额：RoleLimits 加 4 项——最多建库数、单库最多文档数、知识库总存储、单文件大小上限。全局有总开关、允许公开、允许图谱。知识库存储单独记账，不占「我的文件」配额 |
| 10 | 「文档库」指 `/files` 文件库（agent 产出的文件）：加「转到知识库」菜单，上传弹窗加「从文件库导入」。导入时复制一份，按 sha256 去重 |
| 11 | 每个库有「对话中启用」开关，按成员各自设置。输入框支持 `@知识库` 临时指定检索范围。做 5 个检索工具 + 引用来源卡片 + 「存入知识库」写工具（需用户确认）。Dify 知识库保持不变，两套并存 |
| 12 | 队列：Mongo 任务表 + arq 唤醒，独立队列 `kb`，默认并发 4，跟应用进程一起跑。进度以轮询为主，WS 加速。分 4 个叠加 PR 交付。中英文案完整，ja/ko/ru 先用英文兜底 |

用户指定开发流程用 **Trellis**（`.trellis/workflow.md`），子代理模型为 `claude-sonnet-5-5`。

---

## 2. 仓库、分支、PR

- 仓库：`margarethallg916/fd`。`main` 是昆小智基线，以后结果通过 PR 合入 `main`。
- 当前分支 `hoplite/poteidaia-708f791c` 有 2 个提交：
  - 规划 + 引擎接口桩
  - PR1 全部实现（从工作区检查点恢复后提交）
- 原计划是叠加 PR，PR2/3/4 依次从上一个分支派生，命名用 `--` 分隔：
  - `hoplite/poteidaia-708f791c--kb-share`
  - `hoplite/poteidaia-708f791c--kb-agent-tools`
  - `hoplite/poteidaia-708f791c--kb-graph`
- 如果想简化，也可以全部在同一分支上做。

---

## 3. 架构（PR1 已实现）

```
浏览器 /knowledge ──HTTP──► /api/kb/*  (src/api/routes/knowledge_base.py)
                               │
                       service.py (CRUD/上传/检索编排)  ── access.py (权限：PR1 仅库主)
                               │
   ┌───────────────┬───────────┴────────────┬──────────────────┐
 Mongo(唯一可信)   对象存储(get_or_init_storage)  queue.py(任务表)   Qdrant(派生索引)
 kb_bases          kb/{kb_id}/{doc_id}/original{ext}   │             集合 kb_{dim}_{sha1(model)[:8]}
 kb_members        kb/{kb_id}/{doc_id}/parsed.md       │             dense(COSINE)+bm25 稀疏(IDF)
 kb_documents                                          ▼
 kb_chunks                       runtime.py：arq Worker(队列 KB_ARQ_QUEUE_NAME) 或本地 runner
 kb_tasks                         └─► pipeline.process_document：
                                       parse(parsing.py) → chunk(chunking/) → embed(embedding.py)
                                       → sparse(sparse.py) → upsert(vector_store.py)
                                 对账循环(main.py _schedule_kb_reconcile, 每 30s)
```

- **文档状态机**：`uploaded → parsing → parsed → indexing → indexed`，外加 `error_parsing`、`error_indexing`。
  - 每次迁移都是 CAS：`update_one({_id, status: {$in: allowed}})`。
- **队列**：
  - 上传时把文档 `$addToSet` 进该库唯一一个 queued 的 ingest 任务（部分唯一索引保证），然后发布。
  - 领取用原子 `find_one_and_update`，同一用户同时只跑一个任务（公平）。
  - 租约 60s、心跳 15s，文件之间检查取消，单任务内文件串行。
  - 对账：过期租约会重新排队，最多 3 次，超过则失败并回滚文档状态；会补发长时间未发布的任务；会把孤儿文档入队；会继续清理状态为 deleting 的库。
- **检索**：dense 和 jieba BM25 稀疏各自召回，按 0.7/0.3 **加权融合**（对齐 Yuxi，不用 RRF）→ 阈值过滤 → 可选 rerank → 回填文件名并丢弃孤儿。
  - 库的嵌入签名和当前模型卡不一致时，退化为关键词检索，并返回 `warnings: ["index_outdated"]`。
- **完整设计**：`../10-21-kb-core-ingestion/design.md`（数据模型 §2、API 契约 §8、设置 §9、前端 §10）。

---

## 4. 进度总览

| 交付 | 设计 | 代码 | 单测 | 复核(W2) | 端到端 | spec |
|---|---|---|---|---|---|---|
| PR1 核心入库 + UI（`10-21-kb-core-ingestion`） | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| PR2 分享/订阅/配额/导入（`10-21-kb-share-subscribe-quota`） | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| PR3 昆小智工具/@知识库/引用（`10-21-kb-agent-tools`） | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| PR4 图谱（`10-21-kb-graph`） | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |

每个子任务目录下都有 `prd.md`、`design.md`、`implement.md`。PR1 的 `implement.jsonl`/`check.jsonl` 已整理好，PR2–4 的还没整理。

---

## 5. PR1 文件地图（已提交）

**后端（`src/infra/knowledge_base/`）**

| 文件 | 作用 |
|---|---|
| `parsing.py` | 按扩展名分派解析，`SUPPORTED_EXTENSIONS` 白名单，`parse_document()` 输出 markdown |
| `chunking/__init__.py`、`chunking/nlp.py`、`chunking/parsers/{general,qa,book,laws}.py` | 移植自 Yuxi ragflow_like（MIT，文件头已注明出处）。`chunk_markdown()` 带 1-based 行号 |
| `sparse.py` | jieba BM25 稀疏向量（crc32 作索引，TF 饱和，IDF 由 Qdrant 计算） |
| `embedding.py` | 解析 embedding/rerank 模型卡，归一化 `api_base`（去掉 `/v1`），批量嵌入 + 重试，rerank 失败时返回 None |
| `vector_store.py` | Qdrant 封装（服务端/本地），point id 用 `uuid5` 确定性生成 |
| `fusion.py` | 加权融合、阈值、rerank 分数合并（纯函数） |
| `storage.py` | 5 个集合 + `ensure_indexes()` |
| `access.py` | `KBAccess`（PR1 只有 owner；PR2 在这里扩展角色） |
| `service.py` | 业务编排（CRUD、上传去重、统计聚合、检索、删除、重建） |
| `objects.py` | 对象存储 key 规则 |
| `queue.py`、`pipeline.py`、`runtime.py` | 队列、单文档处理、Worker 与对账 |
| `events.py` | WS 推送 `kb:document_updated`、`kb:task_updated` |
| `errors.py` | 领域异常 |

- `src/kernel/schemas/knowledge_base.py`：API 模型，字段已和前端 TS 类型自动比对，一致。
- `src/api/routes/knowledge_base.py`：18 个接口，见 design §8。
- 接线改动：
  - `src/api/main.py`：注册路由 `/api/kb`；启动时建索引（失败不阻断启动）；上传路径加入 body-limit 豁免；对账后台任务；关闭时 `close_vector_store`。
  - `src/infra/runtime_services.py`：启停 KB worker（失败只记日志）。
- 设置：
  - `src/kernel/config/{base.py,_definitions_extra.py,service.py,constants.py}`、`src/kernel/schemas/setting.py`：新分类 `knowledge_base`。
  - 管理端可改：`KB_ENABLED`、`KB_EMBEDDING_MODEL_ID`（空则回退 `NATIVE_MEMORY_EMBEDDING_MODEL_ID`）、`KB_RERANK_MODEL_ID`、`KB_WORKER_CONCURRENCY`（需重启）、`KB_MAX_FILE_MB`、`KB_EMBEDDING_BATCH_SIZE`。
  - 仅环境变量：`QDRANT_URL`、`QDRANT_API_KEY`、`QDRANT_LOCAL_PATH=data/qdrant`、`KB_ARQ_QUEUE_NAME=kunxiaozhi:kb`、`KB_JOB_TIMEOUT_SECONDS=7200`。
- 部署：
  - `deploy/docker-compose.yml`：新增 `qdrant` 服务（`qdrant/qdrant:v1.15.5`）和 `QDRANT_URL`。
  - `nginx/nginx.conf`：KB 上传 location，200m。
  - `.env.example`：新增 Knowledge Base 段。
  - **`k8s/` 没改**，k8s 下默认走本地模式。
- 依赖：`qdrant-client`、`pypdf`、`openpyxl`、`xlrd`、`markdownify`、`python-docx`（另外带进来 bs4/lxml）。

**前端（`frontend/src/`）**
- `components/knowledge/`：
  - `KnowledgeBasePanel`：入口，读 `:kbId` 和 `?tab=`。
  - `KnowledgeBaseListPage` + `KnowledgeBaseCard` + `KnowledgeBaseDialog`/`KnowledgeBaseFormFields`：列表与建库/编辑。
  - `KnowledgeBaseDetail`（Tab：文档/检索测试/设置）+ `DocumentsTab`、`UploadDialog`、`DocumentDetailSidebar`（解析内容/分块）、`RetrievalTestTab`、`SettingsTab`、`DocumentStatusBadge`、`KnowledgeNotices`。
  - `hooks/`：`useAdaptivePolling`（5s 起步，×1.5 退避到 30s）、`useKbDetail`、`useKbUploadQueue`（3 个文件并发上传）、`useKnowledgeBases`、`useKbConfig`、`useKbForm`。
  - `knowledgeUtils.ts`、`knowledgeStyles.ts`。
- `services/api/knowledgeBase.ts`：`knowledgeBaseApi`、全部类型、XHR 上传（带进度、可中断、401 重试）。
- 导航改动：
  - `components/layout/AppContent/{types.ts,TabContent.tsx}`
  - `App.tsx`：路由 `/knowledge/:kbId?`
  - `SessionSidebar.tsx`、`SidebarParts/{SessionListContent,SidebarRail}.tsx`、`skeletons/SidebarSkeleton.tsx`
- 其他改动：
  - `contexts/SettingsContext.tsx`：`enableKnowledgeBase`
  - `hooks/useWebSocket.ts`：新增 `onKbEvent`
  - `types/settings.ts`、`SettingsPanel.constants.ts`、`SettingsPanel.tsx`：分类和模型卡选择器
  - `utils/backendErrors.ts`、`utils/knowledgeBaseEvents.ts`
  - `i18n/locales/{zh,en}.json`：`knowledge.*` 各 150 个键
- 测试：`components/knowledge/__tests__/*`、`i18n/__tests__/knowledgeKeys.test.ts`。

**后端测试**
- `tests/infra/knowledge_base/engine/`：6 个文件，116 个用例，含参数化后共 163 个测试。
- `tests/infra/knowledge_base/domain/`：内存版 Mongo fake 在 `kb_fakes.py`。
- `tests/api/test_knowledge_base_routes.py`
- `tests/kernel/config/test_knowledge_base_definitions.py`

---

## 6. 验证结果（2026-10-21 本次交接时实测）

| 检查 | 命令 | 结果 |
|---|---|---|
| KB + config + runtime 测试 | `uv run pytest -q -p no:cacheprovider --import-mode=importlib tests/infra/knowledge_base tests/api/test_knowledge_base_routes.py tests/kernel/config tests/infra/test_runtime_services.py` | **384 通过，2 失败**。失败的是 `test_runtime_services.py` 的 `start_…_all_distributed_listeners` 和 `stop_…_all_distributed_listeners`，都在基线已知失败列表里，不是新增 |
| 全量后端 | `uv run pytest -q -p no:cacheprovider --import-mode=importlib tests` | 见 §6.1 |
| 应用导入 | `uv run python -c "from src.api.main import app"` | 正常（共 319 条路由） |
| ruff | `uv run ruff check src tests` | 2 个错误，就是基线已有的那 2 个（行尾换行），没有新增 |
| mypy | `uv run mypy src/` | 34 个错误（基线 33）。**新增 1 个**：`service.py:186`，见 §7-1 |
| 前端类型 | `cd frontend && pnpm exec tsc --noEmit -p .` | 通过 |
| 前端 KB 测试 | `./node_modules/.bin/tsx --test <knowledge 测试文件...> src/components/panels/SidebarParts/__tests__/sidebarNavStyles.test.ts` | 30/30 通过 |
| 前端 lint | `pnpm exec eslint src/components/knowledge src/services/api/knowledgeBase.ts src/utils/knowledgeBaseEvents.ts` | 干净 |
| 前端构建 | `pnpm build` | 见 §6.1 |
| 端到端手工验证 | — | **没做** |

基线（main 原始状态）：全量 11 个失败（多数是因为本地没有 Redis 等环境），清单在 `research/baseline-tests.md`。

### 6.1 本次补测

（由交接提交时填写，见文末「附：交接时补测结果」。）

---

## 7. PR1 收尾清单（按顺序做）

1. **修 mypy**：`src/infra/knowledge_base/service.py:185-186` 中，`emb = kb.get("embedding") or None` 之后的 `emb.get(...)` 被 mypy 判为可能为 None。改成：
   ```python
   emb = kb.get("embedding") or None
   outdated = emb is not None and (card is None or card.model_name != emb.get("model_name"))
   ```
   或者给 `emb` 标注 `dict[str, Any] | None`。改完跑 `uv run mypy src/`，应回到 33 个。
2. **W2 复核**：开一个新的 check 子代理，Active task 指向 `.trellis/tasks/10-21-kb-core-ingestion`，按 `check.jsonl` 读规范。要特别注意：领域/API 实现子代理的最终报告**没收到**（运行被中断，代码来自 13:13 的工作区检查点），所以要逐条对照 design §2–§8 核查：
   - 部分唯一索引 `kb_tasks (kb_id,type) where status=queued` 是否真的建了。
   - 领取逻辑有没有带上「同用户已有 running 任务就不领」的公平条件。
   - 对账的 5 项职责是否都实现了。
   - 删除库是否从 `deleting` 状态正确续删。
   - 检索时孤儿过滤和 `index_outdated` 退化是否正确。
   - 下载接口是否带 `attachment` + `nosniff` 头。
   - 上传接口：白名单、大小、sha256 去重（重复时返回 200 + `duplicate: true`）。
   - 非库主访问返回 404，`KB_ENABLED=false` 时返回 403 `kb_disabled`。
3. **端到端验证**：沙箱没有 Docker，可以直接用二进制：
   ```bash
   # Mongo
   curl -sSfL -o /tmp/mongo.tgz https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-ubuntu2404-8.0.4.tgz && tar xzf /tmp/mongo.tgz -C /tmp
   mkdir -p /tmp/mongo-data && /tmp/mongodb-linux-x86_64-ubuntu2404-8.0.4/bin/mongod --dbpath /tmp/mongo-data --bind_ip 127.0.0.1 --fork --logpath /tmp/mongod.log
   # Redis
   apt-get update && apt-get install -y redis-server && redis-server --daemonize yes --save ""
   # 后端（不设 QDRANT_URL 即本地 Qdrant 模式）
   uv run python main.py           # 8000 端口
   cd frontend && pnpm dev         # 3001 端口，代理 /api
   ```
   - 需要先在管理端「模型」里加一个 **embedding 模型卡**（OpenAI 兼容 `/v1/embeddings`），然后在设置 → 知识库里选中它。没有可用的 embedding 服务时，可以写一个本地假服务（返回固定维度的随机向量）来走通流程。
   - 验收流程：建库 → 上传 md/txt/csv/xlsx（不需要 MinerU）→ 状态变为「已完成」→ 检索测试命中 → 分块预览和行号正确 → 删除文档后检索不再命中 → 换 embedding 模型后出现「重建索引」提示 → 重建成功。
   - 取截图放进 PR。
4. **沉淀 spec**：新增 `.trellis/spec/backend/knowledge-base.md`（状态机、队列契约、Qdrant 布局、上传白名单与 html 下载策略、设置优先级），以及 `.trellis/spec/frontend/knowledge-base-ui.md`（轮询、WS 加速、Tab 与路由）；同时更新两个 `index.md`。
5. **引擎实现的偏差**（都已接受，写 spec 时注明）：
   - 任何非空文本至少算 1 个 token。
   - `nlp.py` 的随机数固定种子，保证重复分块结果确定。
   - utf-8 优先解码，失败才用 charset_normalizer。
   - zip 中 md 的相对图片链接会被去掉。
   - 融合时负的 dense 分数截为 0；稀疏分数全部相同时都归一化为 1。
   - qa/laws 预设改写过的块，行号为 None。
   - 本地 Qdrant 模式会阻塞事件循环（开发可接受）。
   - `.xls` 只用伪造的 workbook 测过。
6. **已知限制**（写进 PR 描述）：
   - 本地 Qdrant 模式是**单进程文件锁**。多副本或多 worker 部署必须设 `QDRANT_URL`（compose 已配好）。
   - `k8s/` 还没加 Qdrant。
   - `run_blocking_io` 线程池（8 线程）和聊天共用，大批量解析可能抢占线程。
   - MinerU 默认关闭（`ENABLE_DOCUMENT_PARSE=False`），这时 pptx/图片无法解析，会给出明确报错。
7. 收尾后：把 PR 标为 Ready，执行 `task.py finish`/`archive`，然后进入 PR2。

---

## 8. PR2–PR4 怎么做

**通用流程**：读该子任务的 `design.md`（已定稿，数据模型、API、前端、测试都列全了）→ 用 `task.py add-context` 整理 `implement.jsonl`/`check.jsonl`（参考 PR1 的条目）→ `task.py start` → 后端和前端两个实现子代理并行（写入的文件集合不能重叠）→ check 子代理复核 → 主代理做端到端验证、更新 spec、提交、开叠加 PR。

### PR2 分享 / 订阅 / 配额 / 文件库导入（`10-21-kb-share-subscribe-quota/design.md`）

1. `access.py` 扩展角色矩阵：owner/editor/viewer/subscriber/visitor。subscriber 和 visitor 的读权限要**每次请求实时判断**，条件是 `visibility==public && public_status==approved && KB_ALLOW_PUBLIC`。停用用户视为无权限。
2. 成员接口：`/members`。工号校验用 `POST /users/resolve`，**只返回 username/exists/active 三个字段**。
3. 站内通知：新集合 `kb_notifications`（TTL 90 天），同时推 WS `kb:notification`。现有 `notifications` 集合是管理员公告，**不能复用**。
4. 公开、订阅、广场 `/plaza`，以及管理端 `/api/admin/kb/*`（审核、下架）。新增权限 `knowledge_base:admin`，要改 4 处：
   - `src/kernel/types.py` 的 `Permission`
   - `src/kernel/schemas/permission.py` 的 metadata 和 group
   - 前端 `types/auth.ts`
   - admin 角色会自动获得。**普通使用不要加新权限**，因为已部署的 user 角色不会自动拿到新权限。
5. 配额：`RoleLimits` 加 `kb_max_count`、`kb_max_docs_per_kb`、`kb_storage_quota_mb`、`kb_max_file_size_mb`。
   - 多角色取最宽松的值，缺省回退全局设置 `KB_DEFAULT_*`。
   - 用量按聚合实时计算，计到库主名下。
   - **前端 `RolesPanel.tsx` 必须把新字段加进表单状态和 limits 构造**，因为后端会整体覆盖 limits，漏了就会被清空。
6. 文件库导入：
   - `RevealedFileStorage` 新增 `get_owned(user_id, file_id)`。目前没有 get_by_id，ObjectId 无效时返回 None。
   - 跳过 `file_type=="project"` 的文件。
   - 用 `storage.download_stream(file_key)` 流式读取，同时算 sha256。
   - 和上传共用 `service.add_document_from_stream`，需要把 PR1 的上传逻辑抽出来。
   - 前端改动链：`FileContextMenu` 的回调要沿 `RevealedFilesPanel → SessionGroup → RevealedFileCard → GridCard/ListCard → FileContextMenu` 一路传下去。

### PR3 昆小智工具（`10-21-kb-agent-tools/design.md`）

1. 6 个工具写在 `src/infra/tool/knowledge_base_tool.py`，在 `build_internal_tools()` 里当 `KB_ENABLED` 时注册：
   - `list_knowledge_bases`
   - `search_knowledge_base`
   - `open_kb_document`
   - `find_in_kb_document`
   - `search_kb_files`
   - `save_to_knowledge_base`（只生成草稿，用户在卡片上确认后才写入）
   - 注册后管理端的工具策略 UI 自动可用。
2. **坑**：
   - 带 `runtime: ToolRuntime` 参数的工具不会走 harness 中文本地化，所以 **docstring 和 `Annotated` 参数描述都要直接写中文**。
   - 返回的 JSON 要 ≤8000 字符，超过的话前端拿不到结构化结果，引用卡片就渲染不出来。
   - 成功返回的文本不要以 `error`/`failed` 开头，否则会被判为失败。
   - 工具里不要抛异常。
3. `agent_options` 不会被清洗，客户端可以伪造任意键。所以：
   - 新增 `AgentRequest.kb_ids`。
   - `chat.py` 先删掉客户端传来的 `_kb_*` 键，再写 `_kb_mentioned_ids`。
   - 持久化会话配置时剔除这个键。
   - 工具每次调用都要重新授权。
4. 写工具要加进三处排除：`SCHEDULED_RUN_EXCLUDED_TOOLS`、`TEAM_ROUTER_EXCLUDED_TOOLS`、企业微信的 `disabled_tools`。企业微信那处有 AST 测试 `tests/infra/tool/test_scheduled_task_registration.py::test_wecom_submit_disables_draft_tool`，要同步更新。
5. 前端：
   - `@` 已经被专家/团队提及占用，要把知识库合并进同一个弹窗作为一个分组。欢迎页 `@` 过滤专家卡片的行为不能变（spec `composer-mention-slash-skills.md`）。
   - `submitChat` 末尾追加 `kbIds` 参数。
   - 结果卡片参照 `MessagePartRenderer.tsx` 里 `ScheduledTaskDraftCard` 的「谓词 + 解析器」模式。
   - 草稿卡不收进执行过程分组，要改 `executionProcess.ts`。

### PR4 图谱（`10-21-kb-graph/design.md`）

- 新集合 `kb_graph_entities`、`kb_graph_triples`，ID 规则沿用 Yuxi（hash 生成，保证幂等合并）。
- 复用 PR1 队列，任务类型为 `graph`。
- 抽取提示词已写在 design 里。LLM 用 `LLMClient.get_model(model_id=KB_GRAPH_LLM_MODEL_ID)`。**注意**：不传 model_id 时会回退到「第一个启用的卡」，这张卡可能是 embedding 卡。
- 图增强检索：PPR 用纯 Python 幂迭代实现，再用 RRF k=60 和常规结果融合，出错时退化为常规结果。
- 可视化用已有的 `@xyflow/react`，配合自写的确定性力导向布局（纯函数），不引入新库。

---

## 9. 必须知道的坑（研究结论，已验证）

- **Mongo 是单机社区版**：没有事务，也没有 `$vectorSearch`。状态迁移全部用带条件的 `find_one_and_update`/`update_one`。
- **设置系统**：
  - `SETTING_DEFINITIONS` 里的每个 key 都要在 `Settings` 里有同名、同默认值的字段，否则 `refresh_settings` 会报错。
  - 首次启动后优先级是 **DB > env > 默认值**，所以基础设施地址（Qdrant）只放环境变量。
  - 模型卡类的 key 要加进 `_ALLOW_EMPTY_STRING_SETTINGS`。
- **请求体限制**：
  - 后端 `main.py` 对非白名单路径限制 8MiB。
  - nginx 除了 `/api/upload/` 和 KB 上传 location 外都是 1MB。新增大请求接口时两处都要改。
- **arq**：
  - KB 用独立队列名。
  - `max_jobs` 不支持热更新，所以 `KB_WORKER_CONCURRENCY` 需要重启。
  - 崩溃的 job id 会被锁住约 `job_timeout + 10` 秒，所以**真相在 Mongo 任务表**，arq 只负责唤醒。
- **WebSocket**：只在聊天页挂载了一个。KB 详情页自己挂了 `useWebSocket({onKbEvent})`，每个 hook 实例会开一条独立连接。前端只分发已知的事件类型，新增 `kb:*` 类型要加分支（已加）。
- **用户**：工号就是 `username`，但持久化时一律存 `user_id`（`TokenPayload.sub`），因为用户名可以改。
- **前端约定**：
  - 没有共享的 Button/Modal 组件，照抄相邻页面的写法。
  - 测试是 `node:test` + `tsx --test`，不是 vitest。
  - i18n 只维护 zh 和 en。
  - 卡片禁止渐变 banner（spec `workbuddy-card-banner.md`），只用 lucide 图标，不用 emoji 和 CDN。
- **后端测试**：
  - 全量跑必须加 `--import-mode=importlib`（有重名测试文件）。
  - 新测试文件名要全仓库唯一，并且不要加 `__init__.py`。
- **HTML 上传**：html 在主上传接口的危险扩展名黑名单里。KB 上传用的是独立白名单，原件下载一律以 attachment + nosniff 返回，所以允许上传 html。写 spec 时注明这一点。

---

## 10. 常用命令

```bash
uv sync && (cd frontend && pnpm install --frozen-lockfile)
uv run pytest -q -p no:cacheprovider --import-mode=importlib tests/infra/knowledge_base tests/api/test_knowledge_base_routes.py
uv run ruff check src tests && uv run mypy src/
cd frontend && pnpm exec tsc --noEmit -p . && pnpm lint && pnpm build
./node_modules/.bin/tsx --test src/components/knowledge/__tests__/knowledgeUtils.test.ts
python3 ./.trellis/scripts/task.py list   # 查看任务状态
```

---

## 11. 文档索引（替代重读两个仓库）

| 文件 | 内容 |
|---|---|
| `prd.md`（本目录） | 父任务目标、12 条决策、拆分方案 |
| `research/kxz-backend-infra.md` | 昆小智后端约定：路由注册、鉴权依赖、Mongo 模式、设置系统、RoleLimits、对象存储、文件库集合、用户、上传流程、部署、测试写法（附行号） |
| `research/kxz-backend-ai.md` | MinerU 客户端、embedding/rerank 模型卡、后台 LLM 调用、arq、WS 推送、工具定义/注册/门控/输出限制、聊天请求参数链路 |
| `research/kxz-frontend.md` | 导航怎么加（8 处改动）、UI 原语表、Tabs/卡片/样式变量、API 层、轮询、i18n、`/files` 菜单、管理端设置与 RolesPanel、输入框 `@` 与消息渲染、图形库 |
| `research/yuxi-kb.md` | Yuxi 的状态机、队列、分块预设与算法、检索参数、工具 docstring 原文、图谱抽取提示词与 PPR、前端交互流程 |
| `research/baseline-tests.md` | main 基线测试的失败清单 |
| `../10-21-kb-*/design.md` | 四个 PR 的定稿设计 |

参考仓库（需要时再看）：`https://github.com/123zhaozheng/kunxiaozhi`（= 本仓库 main），`https://github.com/xerrors/Yuxi`（KB 代码在 `backend/package/yuxi/knowledge/`）。
