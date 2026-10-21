# Design: 昆小智知识库工具 + 对话启用 + @知识库 + 引用来源（PR3）

> 子任务 10-21-kb-agent-tools · 叠加在 PR2 之上。决策见父 prd.md（11）。
> 研究：`[ai §6]` 工具定义/注册/输出/确认、`[ai §7]` 请求参数、`[fe §9]` 输入框与消息渲染、`[yuxi §6]` Yuxi 工具与提示词。

## 1. 对话可见范围（`src/infra/knowledge_base/chat_scope.py`）

`resolve_chat_kb_scope(user_id, mentioned_ids: list[str] | None) -> list[ChatKB]`（每次工具调用都实时计算，不缓存）：
- 可读库 = owner/editor/viewer 成员 + subscriber（且库仍公开且通过、`KB_ALLOW_PUBLIC`）。库 `status == "active"`。
- 有 `mentioned_ids`（本轮 @）→ 只取 mentioned ∩ 可读（显式 @ 时忽略 `chat_enabled`）。
- 否则 → 可读且该用户成员行 `chat_enabled == true`。
- `ChatKB{kb_id, name, description, role, doc_count, indexed: bool}`。
- 工具里的 `kb_id` 必须在该范围内，否则返回 `{"success": false, "error": "知识库不存在或当前对话未启用: {kb_id}"}`（不调用检索）。

## 2. 每成员「对话中启用」

- `kb_members.chat_enabled`（PR1 已有字段，owner 默认 true；分享/订阅新建成员默认 true）。
- `PATCH /api/kb/{kb_id}/chat-enabled {enabled: bool}`（任意成员；visitor 404）→ KnowledgeBase；`KnowledgeBase` 响应增加 `chat_enabled: bool | null`（非成员为 null）。
- 前端：卡片与详情头部的「对话中启用」开关（`ToggleSwitch`）。

## 3. 每轮 @知识库 的传递

- 后端：`AgentRequest` 增加 `kb_ids: Optional[list[str]]`（≤10，去重，每个 ≤64 字符）。`chat.py` 提交时：先从 `agent_options` 中删除所有以 `_kb_` 开头的客户端键，再写入 `agent_options["_kb_mentioned_ids"] = kb_ids`（仅当非空）；`build_conversation_config` 持久化会话配置时**剔除** `_kb_mentioned_ids`（只对本轮生效）。WeCom/定时任务不传，工具按 `chat_enabled` 解析。
- 工具从 `runtime.config["configurable"]["agent_options"]["_kb_mentioned_ids"]` 读取（作为提示，仍走 §1 授权）。
- 前端：`services/api/session.ts` `submitChat`/`buildSubmitChatBody` 末尾追加 `kbIds?: string[]` → body `kb_ids`；`useAgent` 新增 getter `getKnowledgeBaseIds`；`ChatView` 持有 KB chip 状态（同 `emphasizedSkillNames` 位置与语义），发送成功后清空。

## 4. 工具（`src/infra/tool/knowledge_base_tool.py`，在 `build_internal_tools()` 中当 `KB_ENABLED` 时注册）

通用约定：`@tool async def ...(..., runtime: Annotated[ToolRuntime, InjectedToolArg] = None) -> str`；**中文 docstring 与每个参数的 `Annotated` 描述写在源码里**（`runtime` 工具不走 harness 本地化 `[ai §6.7]`）；整个函数体 try/except，永不抛出；返回 JSON 字符串（`run_blocking_io(json.dumps, ..., ensure_ascii=False)`）；面向前端渲染的结果 ≤ 8000 字符（超限时按比例截断片段文本，保证 JSON 完整）；成功文本不得以 `failed/error` 等开头。

| 工具名 | 参数 | 行为 | 返回（`entity_type`） |
|---|---|---|---|
| `list_knowledge_bases` | `include_disabled: bool = False` | 列出本对话可用的知识库（§1）；`include_disabled=True` 时额外列出未启用但可读的库（标 `chat_enabled=false`，提示用户可 @ 或在知识库页开启） | `kb_list`：`{knowledge_bases: [{kb_id, name, description, role, doc_count, chat_enabled}], mentioned: bool}` |
| `search_knowledge_base` | `query`（提炼后的检索关键词或问题）、`kb_ids: list[str] \| None`（空=本对话全部可用库）、`file_name: str \| None`（文件名关键词过滤，非必要不用）、`top_k: int = 6`（1-10） | 调 PR1 `service.search_many`（多库，hybrid + 有 rerank 就用；`file_name` 先解析为 doc_id 集合过滤）；每条结果编号 `ref` 从 1 开始 | `kb_search`：`{query, results: [{ref, kb_id, kb_name, doc_id, filename, start_line, end_line, chunk_count, score, content}], warnings}`，`content` 默认 ≤ 600 字 |
| `open_kb_document` | `kb_id`、`doc_id`、`line: int \| None`（1-based 起始行，优先）、`offset: int \| None`（0-based）、`window_size: int = 200`（1-800） | 读解析后的 markdown，行窗口，`f"{n:6d}\t{line}"` 格式（Yuxi `_build_open_file_window`） | `kb_document_window`：`{kb_id, doc_id, filename, start_line, end_line, total_lines, has_more_before, has_more_after, next_offset, content}` |
| `find_in_kb_document` | `kb_id`、`doc_id`、`patterns: list[str]`（≥1）、`use_regex=False`、`case_sensitive=False`、`max_windows=5`（1-20）、`window_size=40`（1-200） | 行级匹配并合并窗口（Yuxi `_build_find_file_windows`）；非法正则返回错误 | `kb_document_find`：`{kb_id, doc_id, filename, match_mode, total_matches, windows: [{start_line, end_line, matched_lines, content}]}` |
| `search_kb_files` | `query: str \| None`（文件名关键词）、`kb_name: str \| None`、`limit: int = 50`（1-200） | 在可用库中按文件名模糊搜索（不能两者都为空） | `kb_files`：`{files: [{kb_id, kb_name, doc_id, filename, status, chunk_count, size, created_at}], total, has_more}` |
| `save_to_knowledge_base` | `title`（文档标题，用作文件名）、`content`（Markdown 正文，≤200k 字符）、`kb_id: str \| None`（建议的目标库，可空） | **只创建草稿**（不写库）：存 `kb_save_drafts`（TTL 30 分钟），返回卡片数据；用户在卡片中选择目标库（可写库列表）并确认后才真正入库 | `kb_save_draft`：`{draft_id, title, preview(≤500字), char_count, suggested_kb_id, suggested_kb_name, expires_at, message: "已生成保存草稿，请用户在卡片中选择知识库并确认"}` |

检索类工具的 docstring 需教会模型使用顺序（参考 Yuxi SKILL.md）：先 `list_knowledge_bases`（不确定时）→ `search_knowledge_base` → 片段不足时 `open_kb_document`（用返回的 `start_line`）/ `find_in_kb_document` → 回答时注明「来源：文件名（知识库名）」并可用 `[ref]` 编号；不要编造 `kb_id`/`doc_id`。

草稿确认接口（PR3 新增，`/api/kb`）：
- `GET /drafts/{draft_id}` → `{draft_id, status: pending|confirmed|cancelled|expired, title, preview, char_count, suggested_kb_id, kb_id?, document_id?, expires_at}`（仅草稿创建者）。
- `POST /drafts/{draft_id}/confirm {kb_id, title?}` → `{document: KBDocument, kb_id}`：原子 `pending → applying → confirmed`；校验 `kb_id` 可写（owner/editor）与配额；写 `{title}.md`（文件名清洗，`source="agent"`, `source_ref=draft_id`）走 PR2 的 `add_document_from_stream`；重复内容返回已存在文档。
- `POST /drafts/{draft_id}/cancel` → 草稿状态。

工具门控：
- 只读工具在 `KB_ENABLED` 时注册；`save_to_knowledge_base` 额外加入 `SCHEDULED_RUN_EXCLUDED_TOOLS`、`TEAM_ROUTER_EXCLUDED_TOOLS`、WeCom `disabled_tools`（同步更新 `tests/infra/tool/test_scheduled_task_registration.py` 的 AST 断言）。
- 工具会自动出现在管理端 internal MCP 工具策略列表（`get_internal_tool_infos`），无需额外接线。

## 5. 系统提示注入（`KnowledgeBasePromptMiddleware`）

- 在 fast/search agent 的 `user_middleware` 中、`PromptCachingMiddleware` 之前注册（团队路由不注册），模式照 `MemoryIndexMiddleware` / `SectionPromptMiddleware`（`src/infra/agent/middleware/prompt_injection.py`）。
- 每次运行首次模型调用时计算一次 §1 范围（按 run/thread 缓存在中间件实例内），用户有可用库时追加简短段落：
  ```
  <知识库>
  当前对话可用的个人知识库：{name}（{description 截断 60 字}）；…（最多 10 个，超出写“等 N 个”）
  当问题可能与这些知识库内容相关时，先用 search_knowledge_base 检索，回答中注明来源文件。
  [若本轮有 @：] 用户本轮指定了知识库：{names}。回答前必须先在这些知识库中检索。
  </知识库>
  ```
- 无可用库或 `KB_ENABLED=false` 时不注入。

## 6. 前端

- `@` 弹窗：在现有 persona 模式的 `@` 弹窗中加入「知识库」分组（置于专家/角色之前，按名称过滤，数据来自 `knowledgeBaseApi.list({scope: "readable"})`，带 10s TTL 缓存）；团队模式同样提供知识库分组。合并后的候选列表统一参与上下键/Enter/Tab 索引（`useMentionState.setMentionResultCount`）。选择知识库 → 移除 `@query` 文本、添加 KB chip（`LibraryBig` 图标，样式与技能 chip 一致，可移除），**不改变** persona 选择；欢迎页 `@` 过滤专家卡片的行为不变（spec `composer-mention-slash-skills.md`）。
- 发送：chip 的 kb_ids 随 `submitChat` 发送；用户消息气泡展示「@知识库名」小标签（从会话事件中的 `kb_ids` 或消息元数据恢复；若历史无法恢复则只在实时显示，不强求）。
- 结果渲染（`components/chat/ChatMessage/items/`）：
  - `knowledgeBaseResult.ts`：`isKnowledgeBaseToolName(name)`（支持 `server:tool` 前缀）与各 `entity_type` 的容错解析器。
  - `KnowledgeBaseSearchCard`：`search_knowledge_base` 结果 → 查询词 + 来源列表（编号、文件名、知识库名、行号范围、得分、可展开片段）；点击条目打开文档查看抽屉（复用 PR1 的文档详情组件，定位到 `start_line`）。
  - `KbSaveDraftCard`：标题、预览、可写库下拉（默认 `suggested_kb_id`）、确认/取消；状态从 `GET /drafts/{id}` 恢复（pending/confirmed/cancelled/expired）；确认后显示「已存入《库名》」并可跳转。草稿卡与定时任务草稿一样**不收进执行过程分组**（`executionProcess.ts` 分区规则加入该工具名）。
  - 其余 KB 工具使用通用 `ToolCallItem`，`executionProcess.ts` 为 KB 工具提供能力图标（`LibraryBig`）与简洁步骤文案（如「检索知识库：{query}」「打开文档：{filename} 第 x 行」）。
  - 消息级「参考来源」：在 assistant 消息正文之后（执行过程分组之外）汇总本条消息内所有 `kb_search` 结果中被正文引用的或全部来源（按 doc+行号去重，最多 8 条），渲染为紧凑来源条目，点击打开文档查看抽屉。
- 知识库页：卡片与详情头部「对话中启用」开关；详情页提示「在对话中输入 @ 可临时指定知识库」。
- i18n zh/en：`knowledge.chat.*`、`knowledge.tools.*`、`knowledge.citations.*`、`knowledge.draft.*`。

## 7. 测试

后端：范围解析（mentioned/启用/订阅失效/停用库）、每个工具的授权拒绝与正常输出、8000 字符预算、行窗口与查找合并逻辑、`_kb_` 键清洗与会话配置剔除、草稿确认原子性/过期/权限/配额、门控集合（定时任务/团队路由/WeCom AST 测试）、中间件注入文本。
前端：`buildSubmitChatBody` 携带 `kb_ids` 契约测试、结果解析器容错测试、mention 合并列表索引纯函数测试、i18n 键一致。
