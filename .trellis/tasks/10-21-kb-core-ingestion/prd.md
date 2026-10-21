# KB core：入库链路 + Qdrant 混合索引 + 知识库列表/详情 UI（PR1）

> 父任务 `10-21-personal-knowledge-base`（全部用户决策见父 prd.md）。技术设计见 `design.md`，执行计划见 `implement.md`。

## Goal

用户在左侧导航进入「知识库」，可以新建/编辑/删除自己的知识库，上传文档后自动排队解析、分块、嵌入并写入 Qdrant 混合索引，能看到每个文档的处理状态、失败原因并重试，能预览解析内容与分块，能做检索测试。管理员在设置面板配置知识库开关、向量模型、rerank 模型与并发。

## Requirements

- R1 导航与路由：左侧「知识库」入口（受 `KB_ENABLED` 控制），`/knowledge` 列表、`/knowledge/:kbId` 详情。
- R2 知识库 CRUD（仅库主）：名称、描述、分块预设（通用/问答/书籍/法规）、高级（块大小、重叠）；用户看不到任何模型/OCR 选项。
- R3 上传：单文件请求、前端并发、进度可见；白名单扩展名（design §5）；`KB_MAX_FILE_MB` 限制；同库 sha256 去重。
- R4 处理队列：Mongo 任务表 + arq 独立队列（本地 runner 兜底），默认并发 4，单任务内文件串行、按用户公平、租约/心跳/取消/对账/有限自动重试（design §4）。
- R5 文档状态机 uploaded→parsing→parsed→indexing→indexed + 两个失败态；失败可重试；删除文档同步清理向量/分块/对象。
- R6 解析：MinerU（pdf/docx/pptx/图片）+ 本地解析（txt/md/html/json/csv/xlsx/xls/zip-md）+ pdf 文本层兜底、docx 本地兜底。
- R7 分块：移植 Yuxi 四个预设，记录行号。
- R8 检索：Qdrant dense + jieba BM25 sparse，0.7/0.3 加权融合，可选 rerank、阈值、模式切换；模型变更后提示重建索引并可一键重建。
- R9 管理端设置组 `knowledge_base`（design §9），模型卡选择器复用现有组件。
- R10 中英文案完整（ja/ko/ru 英文兜底）。

## Acceptance Criteria

- [ ] 后端单测覆盖：解析分派、分块（四预设+行号）、稀疏编码、加权融合、嵌入批量与重试、Qdrant 本地模式 upsert/search/delete、队列领取/公平/取消/对账、上传去重、权限（非库主 404）、路由契约。
- [ ] `uv run pytest -q -p no:cacheprovider --import-mode=importlib tests` 失败集合不超过基线 11 个（research/baseline-tests.md）。
- [ ] `ruff check src tests` 不新增错误；`mypy src/` 不新增错误（基线 33）。
- [ ] 前端 `pnpm exec tsc --noEmit -p .` 通过，`pnpm lint` 不新增错误，新增 node:test 测试通过。
- [ ] 本地起服务（Qdrant 本地模式）手动走通：建库→上传 md/txt/csv→已完成→检索测试命中→删除文档。
