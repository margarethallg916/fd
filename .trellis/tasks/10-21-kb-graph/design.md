# Design: 知识图谱（Mongo 三元组 + Qdrant 实体/关系向量 + PPR 图增强检索 + 可视化）（PR4）

> 子任务 10-21-kb-graph · 叠加在 PR3 之上。决策见父 prd.md（3）：不加新服务；按库手动开启；用管理端配置的 LLM 抽取。
> 研究：`[yuxi §7]`（抽取提示词、ID 规则、PPR 检索、可视化 API）、`[ai §3]`（后台 LLM 调用）、`[fe §11]`（已有 `@xyflow/react`，无力导向库）。

## 1. 设置（`knowledge_base` 分类）

`KB_ALLOW_GRAPH`(BOOLEAN, True, frontend_visible)、`KB_GRAPH_LLM_MODEL_ID`(STRING "", chat 模型卡选择器；空则用 `DEFAULT_MODEL_ID`，且必须是 chat 卡)、`KB_GRAPH_CONCURRENCY`(NUMBER 2, 1-16)、`KB_GRAPH_MAX_CHUNKS_PER_KB`(NUMBER 3000, 100-100000，单库参与抽取的分块上限，控制成本)。

## 2. 数据模型（Mongo）

- `kb_bases`：`graph_enabled: bool`、`graph_schema: str`（可选的抽取约束，≤1000 字）。
- `kb_chunks` 增加：`graph_status: "pending"|"done"|"failed"|null`（null=未启用图谱）、`graph_attempts`、`graph_error`、`entity_ids: []`、`triple_ids: []`。
- `kb_graph_entities`：`{_id: hash32(f"{kb_id}:{normalized_name}:{label}"), kb_id, name, normalized_name, label, attributes: {名称: 值}, chunk_ids: [], doc_ids: [], mention_count, vector_status: pending|indexed|failed, created_at, updated_at}`；唯一 `(kb_id, normalized_name, label)`，索引 `(kb_id, mention_count desc)`、`(kb_id, doc_ids)`、文本检索用 `(kb_id, normalized_name)`。
- `kb_graph_triples`：`{_id: hash32(f"{kb_id}:{src_norm}:{src_label}:{label}:{tgt_norm}:{tgt_label}"), kb_id, source_id, target_id, label, text, chunk_ids: [], doc_ids: [], vector_status, created_at}`；索引 `(kb_id, source_id)`、`(kb_id, target_id)`、`(kb_id, doc_ids)`。
- 归一化：`normalize_entity_name = " ".join(text.strip().lower().split())`；实体名 > 512 或类型 > 128 丢弃（及相关关系），关系类型 > 256 丢弃，默认实体类型 `Entity`、关系类型 `RELATED_TO`（Yuxi 规则）。

## 3. 抽取与构建（复用 PR1 队列，任务类型 `graph`）

- `POST /api/kb/{kb_id}/graph/enable {schema?}`（owner；需 `KB_ALLOW_GRAPH`）：`graph_enabled=true`，把该库已索引分块的 `graph_status` 置 `pending`（超过 `KB_GRAPH_MAX_CHUNKS_PER_KB` 的部分不入选并在状态中提示），入队 `graph` 任务（部分唯一索引已保证每库每类型最多一个 queued 任务）。
- `POST /api/kb/{kb_id}/graph/build`（owner/editor）：把 `failed` 与新增 `pending` 分块重新入队。
- `POST /api/kb/{kb_id}/graph/disable`（owner）：`graph_enabled=false`，删除该库实体/关系/向量，分块 `graph_status=null`。
- 增量：图谱已开启的库，ingest 任务结束后若有新分块，自动把新分块置 `pending` 并入队 `graph` 任务；文档删除/重建时先删除其图谱贡献（从实体/关系的 `chunk_ids/doc_ids` 中 `$pull`，计数归零的实体/关系删除并删向量）。
- 处理：任务内用 `asyncio.Semaphore(KB_GRAPH_CONCURRENCY)` 并发抽取，分页拉取 `pending` 分块；每块一次 LLM 调用（提示词沿用 Yuxi，附加 schema 约束与分块文本，要求严格 JSON），解析容错（去代码围栏、`json.loads`，失败时截取首个 `{` 到末个 `}` 重试），每块最多 3 次（2s/10s 退避），失败置 `failed` 并记录错误；成功后 upsert 实体（`$addToSet chunk_ids/doc_ids`、`$inc mention_count`、合并 attributes）与关系，分块写 `entity_ids/triple_ids` 并置 `done`；每 20 块批量嵌入 `vector_status=pending` 的实体名（`name`）与关系文本（`{src} → {label} → {tgt}`）写入 Qdrant 图谱集合。协作式取消、心跳与进度同 PR1。
- LLM：`LLMClient.get_model(model_id=KB_GRAPH_LLM_MODEL_ID or None, temperature=0, max_tokens=2048)` + `ainvoke`；未配置可用 chat 卡 → 任务失败并提示「管理员尚未配置图谱抽取模型」。

## 4. 图谱向量（Qdrant）

集合 `kbg_{dim}_{sha1(model_name)[:8]}`（与库嵌入签名一致），dense 向量 + payload `{kb_id, kind: "entity"|"triple", ref_id, text}`；point id = `uuid5(f"kbg:{kind}:{ref_id}")`。服务端模式建 `kb_id`、`kind` payload 索引。

## 5. 图增强检索

搜索请求增加 `use_graph: bool = false`（检索测试可切换；`search_knowledge_base` 工具在库已构建图谱时默认开启）。仅对 `graph_enabled` 且有已完成分块的库生效：
1. 查询向量在图谱集合中检索实体 top 10、关系 top 10（`kb_id` 过滤）。
2. 种子权重：实体命中 `score*1.0`；关系命中给两端实体各 `score*0.8`；常规检索命中分块所含实体各 `chunk_score*0.3`；归一化。
3. 子图：从种子出发 2 跳（实体 →关系→ 实体），节点上限 2000（Mongo 批量查询 `source_id/target_id $in`）；再加入这些实体的分块节点（`chunk_ids`）。
4. 个性化 PageRank（纯 Python 幂迭代，无新依赖，`alpha=0.85`，50 次或收敛 1e-6），对分块节点排序取前 20。
5. 与常规结果按 RRF（k=60，常规权重 1.0、图谱权重 1.0）融合，结果标注 `sources: ["chunk"]|["graph"]|["chunk","graph"]`，再走 rerank/截断。图谱阶段任何异常只记日志并退化为常规结果。

## 6. 可视化 API（读权限）

- `GET /api/kb/{kb_id}/graph/status` → `{enabled, schema, total_chunks, pending, done, failed, excluded, entity_count, relation_count, active_task, allow_graph}`。
- `GET /api/kb/{kb_id}/graph?q=&max_nodes=100(10-500)&max_depth=2(1-3)` → `{nodes: [{id, name, label, mention_count}], edges: [{id, source, target, label, text}], truncated}`；无 `q` 时取 `mention_count` 最高的实体为种子；有 `q` 时按名称包含匹配（`re.escape`）为种子。
- `GET /api/kb/{kb_id}/graph/entities/{entity_id}` → `{entity, relations: [...最多 50], mentions: [{doc_id, filename, chunk_id, start_line, end_line, snippet}]（最多 20）}`。
- `GET /api/kb/{kb_id}/graph/failed?limit=10` → 失败分块样例（owner/editor）。

## 7. 前端

- 详情页新增「图谱」Tab（`KB_ALLOW_GRAPH=false` 时隐藏）：
  - 未开启：说明卡片（会使用管理员配置的大模型抽取实体与关系，可能耗时）、可选「抽取约束」文本框、「开启图谱」按钮（owner）。
  - 已开启：顶部状态条（待构建 / 已完成 / 失败（点击看样例）/ 实体 / 关系、进度、「继续构建/重试失败」、owner 的「关闭图谱」）；画布用已有的 `@xyflow/react`（懒加载），自定义圆形节点（按 `label` 着色、大小按 `mention_count`）、带标签的边；布局用 `components/knowledge/graph/forceLayout.ts` 纯函数力导向（Fruchterman-Reingold，确定性种子，≤500 节点）；工具栏：搜索实体、最大节点数、深度、重新布局；点击节点打开右侧详情面板（属性、关系列表、提及片段 → 点击打开文档查看抽屉定位行号）。
- 检索测试增加「图谱增强」开关（库已构建图谱时可用），结果条目显示来源标签（常规/图谱）。
- 管理端设置：`settingDesc.KB_ALLOW_GRAPH/KB_GRAPH_LLM_MODEL_ID/KB_GRAPH_CONCURRENCY/KB_GRAPH_MAX_CHUNKS_PER_KB`；`KB_GRAPH_LLM_MODEL_ID` 加入 `MODEL_CONFIG_SETTING_KEYS`（chat）。
- i18n zh/en：`knowledge.graph.*`。

## 8. 测试

后端：抽取结果归一化（丢弃规则、默认类型、ID 确定性、跨块合并）、JSON 容错解析、构建任务状态迁移/重试/取消、增量入队、文档删除时图谱贡献回收、PPR 幂迭代（小图已知排序）、RRF 融合与退化、可视化 API 截断与权限、开关与 `KB_ALLOW_GRAPH`。
前端：forceLayout 确定性与无 NaN、图谱数据到 React Flow 节点/边映射纯函数、i18n 键一致。
