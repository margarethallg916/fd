# Design: KB 分享 / 公开订阅 / 配额 / 文件库导入（PR2）

> 子任务 10-21-kb-share-subscribe-quota · 叠加在 PR1（`10-21-kb-core-ingestion`）之上。决策见父 prd.md（6/7/8/9/10）。
> 研究：`../10-21-personal-knowledge-base/research/` — `[infra §4]` RoleLimits、`[infra §6]` 文件库、`[infra §7]` 用户、`[ai §5]` WS/无站内信、`[fe §7]` /files 菜单、`[fe §8]` 管理端与 RolesPanel。

## 1. 角色与权限（扩展 PR1 `access.py`）

| 角色 | 来源 | 读（详情/文档列表/内容/分块/下载/检索） | 对话使用 | 上传/导入/删除/重试/重建 | 改库设置/删库 | 分享与公开 |
|---|---|---|---|---|---|---|
| owner | 建库者 | ✓ | ✓ | ✓ | ✓ | ✓ |
| editor | 分享 | ✓ | ✓ | ✓ | ✗ | ✗ |
| viewer | 分享 | ✓ | ✓ | ✗ | ✗ | ✗ |
| subscriber | 订阅公开库 | ✓（仅当库仍为公开且已通过） | ✓ | ✗ | ✗ | ✗ |
| visitor | 隐式：任何登录用户访问「公开且已通过」的库 | ✓ | ✗（需订阅） | ✗ | ✗ | ✗ |

- `KBAccess{role, can_read, can_write_docs, can_manage, can_share, can_chat}`；无读权限一律 404。
- subscriber/visitor 的读权限每次请求实时判定 `visibility=="public" and public_status=="approved"`，库主取消公开或管理员下架后立即失效。
- 被分享用户 `is_active=false` 时视同无权限（读 `users` 时校验）。
- 管理员权限：新增 `Permission.KNOWLEDGE_BASE_ADMIN = "knowledge_base:admin"`（后端 `Permission` 枚举、`PERMISSION_METADATA`、`PERMISSION_GROUPS_CONFIG`，前端 `types/auth.ts`）；admin 系统角色自动获得。普通使用**不新增权限**（登录 + `KB_ENABLED` + 配额）。

## 2. 数据模型增量

- `kb_bases` 增加：`public_status: "none"|"pending"|"approved"|"rejected"|"delisted"`（默认 none）、`published_at`、`review: {action, reason, reviewer_username, at} | null`。`visibility` 取 `private|public`。
- `kb_members`：使用 PR1 已有结构，`role` 取 `editor|viewer|subscriber`；分享写 `granted_by`；订阅 `granted_by=null`。
- 新集合 `kb_notifications`：`{_id, user_id, type, kb_id, kb_name, actor_username, role, reason, read_at, created_at}`；索引 `(user_id, created_at desc)`、`(user_id, read_at)`；TTL 90 天（`created_at` + `expireAfterSeconds`）。类型：`kb_shared`、`kb_role_changed`、`kb_unshared`、`kb_unpublished`（推给订阅者）、`kb_publish_approved`、`kb_publish_rejected`、`kb_delisted`。
- `revealed_files` 增加 storage 方法（不改文档结构）：`get_owned(user_id, file_id)`、`list_owned_by_extensions(user_id, extensions, search, skip, limit)`。

## 3. 配额（决策 9）

- `RoleLimits` 增加（全部 `Optional[int]`，`None` = 用全局默认）：`kb_max_count`(≥0)、`kb_max_docs_per_kb`(≥0)、`kb_storage_quota_mb`(>0)、`kb_max_file_size_mb`(>0)。
- 全局默认（设置，`knowledge_base` 分类）：`KB_DEFAULT_MAX_COUNT=10`、`KB_DEFAULT_MAX_DOCS_PER_KB=500`、`KB_DEFAULT_STORAGE_QUOTA_MB=2048`；单文件默认沿用 PR1 的 `KB_MAX_FILE_MB`。
- 解析：多角色取最宽松值，缺省回退全局（模式同 `resolve_upload_limits` `[infra §4]`）。`resolve_kb_limits(roles) -> KBLimits`。
- 计量：知识库存储单独记账，不进 `user_storage` 账本；用量 = 该用户**拥有的**库内所有文档 `size` 之和（聚合，不落冗余计数）；编辑者上传的文档计入库主。
- 执行点：建库（拥有的活跃库数 < 上限）；上传/导入（文件大小 ≤ **上传者**单文件上限；库内文档数 < **库主**单库上限；库主存储用量 + 本文件 ≤ 库主总存储）。检查非原子（可接受的轻微超额，与定时任务配额一致）。
- 错误：400 `{"detail": {"code": "kb_quota_count"|"kb_quota_docs"|"kb_quota_storage"|"kb_file_too_large", "message": 中文, "limit": n}}`。
- `GET /api/kb/quota` → `{limits: {max_count, max_docs_per_kb, storage_quota_mb, max_file_size_mb}, usage: {kb_count, storage_bytes}}`。

## 4. 全局开关（设置，`depends_on: KB_ENABLED`）

`KB_ALLOW_PUBLIC`(BOOLEAN, True, frontend_visible)、`KB_PUBLIC_REQUIRES_REVIEW`(BOOLEAN, False, frontend_visible)、`KB_DEFAULT_MAX_COUNT`/`KB_DEFAULT_MAX_DOCS_PER_KB`/`KB_DEFAULT_STORAGE_QUOTA_MB`(NUMBER)。`KB_ALLOW_PUBLIC=false` 时：发布接口 403 `kb_public_disabled`，广场返回空列表，已公开库对 visitor/subscriber 不可见（只读判定增加该条件）。

## 5. API 增量（前缀 `/api/kb`；字面路径必须声明在 `/{kb_id}` 之前）

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `?scope=mine\|shared\|subscribed\|writable\|readable` | 登录 | shared=editor/viewer；writable=owner+editor；readable=mine+shared+subscribed。每项带 `my_role`、`member_count`、`subscriber_count`、`public_status` |
| GET | `/quota` | 登录 | 见 §3 |
| POST | `/users/resolve` | 登录 | `{usernames: [] (1-50)}` → `{items: [{username, exists, active}]}`；精确匹配，只回这三个字段 |
| GET | `/plaza` | 登录 | `?q=&sort=latest\|popular&page=1&page_size=24(≤60)` → `{items: PlazaKB[], total, page, page_size}`；`PlazaKB` = KnowledgeBase 公共字段 + `subscribed`、`subscriber_count`、`owner_username` |
| GET | `/notifications` | 登录 | `?unread_only=false&limit=50` → `{items, unread_count}` |
| POST | `/notifications/read` | 登录 | `{ids?: []}`（空=全部已读）→ `{updated}` |
| GET | `/notifications/unread-count` | 登录 | `{unread_count}` |
| GET | `/importable-files` | 登录 | `?q=&page=1&page_size=20(≤50)` → 我的文件库中扩展名受支持的文件 `{items: [{id, file_name, file_size, mime_type, created_at, session_name?}], total, page, page_size}` |
| GET | `/{kb_id}/members` | owner | `{items: [{user_id, username, role, granted_by, created_at}], subscriber_count}`（不含订阅者明细） |
| POST | `/{kb_id}/members` | owner | `{usernames: [] (1-50), role: "viewer"\|"editor"}` → `{added: [...], updated: [...], errors: [{username, reason: "not_found"\|"inactive"\|"self"}]}`；已存在则改角色；订阅者被分享时升级为分享角色；每个新增/变更发通知 |
| PATCH | `/{kb_id}/members/{user_id}` | owner | `{role}` |
| DELETE | `/{kb_id}/members/{user_id}` | owner，或 `user_id == me`（退出共享） | 204，发 `kb_unshared`（owner 移除时） |
| POST | `/{kb_id}/publish` | owner | 需 `KB_ALLOW_PUBLIC`；`KB_PUBLIC_REQUIRES_REVIEW` 或当前 `delisted` → `pending`，否则 `approved`；→ KnowledgeBase |
| POST | `/{kb_id}/unpublish` | owner | `visibility=private, public_status=none`，删除全部订阅行并通知订阅者 |
| POST | `/{kb_id}/subscribe` | visitor | 仅公开且通过、非自己的库；→ KnowledgeBase |
| DELETE | `/{kb_id}/subscribe` | subscriber | 204 |
| POST | `/{kb_id}/documents/import` | can_write_docs | `{revealed_file_ids: [] (1-50)}` → `{imported: KBDocument[], duplicates: KBDocument[], errors: [{id, file_name, message}]}` |

管理端（新 router，前缀 `/api/admin/kb`，`require_permissions("knowledge_base:admin")`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/public` | `?status=pending\|approved\|rejected\|delisted&q=&skip=0&limit=20` → `{items: [KnowledgeBase + owner_username, subscriber_count, review], total, skip, limit}` |
| POST | `/{kb_id}/review` | `{action: "approve"\|"reject", reason?}`（仅 pending）；通知库主 |
| POST | `/{kb_id}/delist` | `{reason}`（必填，≤200）；`public_status=delisted`；通知库主；订阅行保留（重新通过后恢复） |

导入：校验文件库归属（`revealed_files.user_id == me`，ObjectId 无效/他人 → 该项 `errors`），跳过 `file_type == "project"`；扩展名白名单与配额（§3）；`storage.download_stream(file_key)` 流式写入 spooled 临时文件并计算 sha256；同库去重；复制到 `kb/{kb_id}/{doc_id}/original{ext}`；`source="files_library"`, `source_ref=<revealed id>`；入队。上传与导入共用一个 `service.add_document_from_stream(...)`。

通知：写 `kb_notifications` + WS `{"type": "kb:notification", "data": {id, type, kb_id, kb_name, actor_username, role, reason}}`（尽力而为）。

## 6. 前端

- 列表页 tablist：`我的知识库` / `共享知识库`（二级筛选：全部 / 共享给我 / 我订阅的）/ `知识广场`（搜索、排序 最新/最热、卡片显示库主、文档数、订阅数、`订阅/已订阅` 按钮；自己的库显示「我的」）。`?tab=mine|shared|plaza`。
- 我的知识库头部显示配额摘要（`已建 x/y · 存储 a/b`），达到上限时「新建知识库」禁用并提示。
- 卡片角标：`编辑者` / `查看者` / `已订阅` / `公开中` / `审核中` / `已驳回` / `已下架`（`scb__status-pill` 样式）。
- 详情页按 `my_role` 控制：visitor/subscriber/viewer 隐藏上传/删除/重试/重建；非 owner 隐藏「设置」Tab；owner 显示「分享」按钮；visitor 显示「订阅」；subscriber 显示「取消订阅」；editor/viewer 显示「退出共享」。
- 分享对话框（owner）：
  - 「按工号添加」：多行输入框，支持逗号/空格/换行分隔粘贴；点「校验」调用 `/users/resolve`，逐个回显 ✓ 存在 / ✗ 不存在 / 已停用；角色选择 查看者/编辑者；「添加」仅提交存在且启用的工号；结果 toast。
  - 成员列表：工号、角色（可切换）、移除（ConfirmDialog）。
  - 「公开到知识广场」区块（`KB_ALLOW_PUBLIC` 为 false 时隐藏）：当前状态与审核/下架原因、订阅人数、发布/取消公开按钮（取消公开前 ConfirmDialog 提示订阅者将失去访问）。
- 通知：KB 面板头部铃铛（未读角标）+ 弹出列表（点击跳转对应库、标记已读、全部已读）；侧边栏「知识库」导航显示未读红点（60s 轮询 `/notifications/unread-count`，`KB_ENABLED` 时）；`useWebSocket` 的 `onKbEvent` 收到 `kb:notification` 时刷新并 toast。
- 文件库：`FileContextMenu` 增加「转到知识库」（`LibraryBig`，仅非 project 且扩展名受支持时显示），按 `[fe §7]` 透传回调；`ImportToKnowledgeBaseDialog` 列出 `scope=writable` 的库（无可写库时提供「新建知识库」入口），导入后 toast 并提供「查看」。上传对话框增加「从文件库导入」页签（`/importable-files` 分页+搜索+多选）。
- 管理端：`/admin/knowledge-bases`（`ProtectedRoute permissions=[KNOWLEDGE_BASE_ADMIN]`，`UserMenu` adminItems 增加入口），Tab 待审核/已公开/已驳回/已下架、搜索、分页；操作 通过/驳回（原因）/下架（原因必填）/查看（打开 `/knowledge/{id}`——管理员对公开库具备读权限即可）。
- `RolesPanel`：新增可折叠「知识库限额」区块（4 个字段），`types/auth.ts` `RoleLimits` 增加字段；注意 `[fe §0.5]` 表单重建 limits 的坑，必须把新字段纳入表单状态与提交构造。
- `backendErrors.ts`：`kb_quota_count`/`kb_quota_docs`/`kb_quota_storage`/`kb_file_too_large`/`kb_public_disabled` 映射。
- i18n zh/en：`knowledge.share.*`、`knowledge.plaza.*`、`knowledge.notifications.*`、`knowledge.quota.*`、`knowledge.admin.*`、`fileLibrary.context.addToKnowledge`、`roles.kb*`、`settingDesc.KB_*`。

## 7. 测试

后端：access 角色矩阵（含订阅者在取消公开/下架/`KB_ALLOW_PUBLIC=false` 后失效、停用用户）、成员增删改与通知、resolve 只回三个字段、发布/审核/下架/取消公开流转、订阅/退订、配额（四项 + 多角色取最宽松 + 全局默认）、导入（归属、project 跳过、去重、配额）、管理端权限、RoleLimits 新字段 round-trip、设置定义一致性。
前端：i18n 键一致、RolesPanel 新字段进入 limits 构造（源码契约）、FileContextMenu 菜单项条件、工号解析（分隔/去重/上限 50）纯函数。
