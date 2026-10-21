# Implement: KB core（PR1）

> 配套 `prd.md` / `design.md`。子代理模型 claude-sonnet-5-5（用户指定）；三个实现包并行（写集合互不重叠），完成后由新的 check 子代理统一复核。

## 0. 前置（主代理已完成）

- [x] 基线导入 `main`（= kunxiaozhi @ 1498b534），分支 `hoplite/poteidaia-708f791c`。
- [x] `qdrant-client>=1.15.0` 已加入 `pyproject.toml`/`uv.lock`；Qdrant 本地模式 hybrid/过滤/按条件删除已验证。
- [x] 引擎接口桩：`src/infra/knowledge_base/{parsing,sparse,embedding,vector_store,fusion}.py`、`chunking/__init__.py`（签名为契约，不得修改签名/数据类字段；可新增私有函数和模块）。
- [x] research/ 四份 + baseline-tests.md；design.md 定稿。

## W1-A 引擎（后端）

写集合：`src/infra/knowledge_base/{parsing.py, sparse.py, embedding.py, vector_store.py, fusion.py}`、`src/infra/knowledge_base/chunking/**`、`tests/infra/knowledge_base/engine/**`、`pyproject.toml` + `uv.lock`（仅新增 pypdf/openpyxl/xlrd/markdownify/python-docx）。

1. 依赖：`uv add pypdf openpyxl xlrd markdownify python-docx`。
2. chunking：从 `/tmp/ref/Yuxi/backend/package/yuxi/knowledge/chunking/ragflow_like/` 移植 `nlp.py`、`parsers/{general,qa,book,laws}.py`（及其依赖的 utils，如有），改包路径，文件头注明 `Adapted from Yuxi (https://github.com/xerrors/Yuxi), MIT License, Copyright (c) 2025 Yuxi Project Contributors`；去掉 yuxi logger 依赖；实现 `chunk_markdown` 行号定位（design §6）。
3. parsing：design §5 全表；MinerU 用 `src/infra/tool/mineru_client.py` 的 `MinerUClient`；解码复用 charset_normalizer 模式；阻塞调用走 `run_blocking_io`。
4. sparse / embedding / vector_store / fusion：design §7.1-7.4。
5. 测试（`tests/infra/knowledge_base/engine/`）：每种扩展名的解析（用内存构造的 docx/xlsx/xls?/pdf/zip 小样本；xls 若无法构造可只测分派与错误）、MinerU 可用/不可用/失败回退（monkeypatch MinerUClient）、四预设分块+行号、token 计数、稀疏编码确定性、融合/阈值/rerank 合并、embedding 批量顺序/重试/api_base 归一化（`httpx.MockTransport` 或 monkeypatch）、vector_store 用 `QDRANT_LOCAL_PATH=tmp_path` 本地模式跑 ensure/upsert/search/delete。
6. 验证：`uv run pytest -q -p no:cacheprovider tests/infra/knowledge_base/engine`、`uv run ruff check src/infra/knowledge_base tests/infra/knowledge_base`、`uv run mypy src/infra/knowledge_base`。

## W1-B 领域 + API（后端）

写集合：`src/kernel/schemas/knowledge_base.py`、`src/infra/knowledge_base/{storage,access,service,objects,queue,pipeline,runtime}.py`、`src/api/routes/knowledge_base.py`、`src/kernel/config/{base.py,_definitions_extra.py,service.py,constants.py}`、`src/kernel/schemas/setting.py`、`src/api/main.py`、`src/infra/runtime_services.py`、`nginx/nginx.conf`、`deploy/docker-compose.yml`、`.env.example`、`tests/infra/knowledge_base/domain/**`、`tests/api/test_knowledge_base_routes.py`、`tests/kernel/config/test_knowledge_base_definitions.py`。

1. 设置：design §9（分类、definitions、Settings 字段、空串白名单、restart-required）。
2. schemas + storage（ensure_indexes 含部分唯一索引）+ access（PR1：owner 才有权限，其他 404）。
3. objects + service（CRUD、上传流式落盘与去重、列表/分页/搜索、统计聚合、重试/删除/重建、content 分页、chunks 分页、下载流）。
4. queue + pipeline + runtime（design §4；arq Worker 与本地 runner；对账后台任务注册到 lifespan 并加入 `_LIFESPAN_BACKGROUND_TASK_NAMES`；KB worker 在 `runtime_services.py` 启停，失败不阻断启动）。
5. 检索编排 `service.search`（调用引擎 `fusion`/`embedding`/`vector_store`/`sparse`），回填 filename、孤儿过滤、`index_outdated` 退化。
6. 路由：design §8 契约；`main.py` 注册路由（`/api/kb`）、启动索引初始化（try/except 不阻断）、body-limit 豁免；nginx location；compose qdrant；.env.example。
7. WS 推送（尽力而为）。
8. 测试：用手写 fake（参考 `tests/infra/scheduler/fakes.py`、`tests/infra/settings/test_settings_storage.py` 的 `_FakeCollection`）覆盖 storage 关键查询、队列领取 CAS/公平性/取消/对账/重试上限、pipeline 状态迁移（引擎函数 monkeypatch）、上传去重、权限 404、路由契约（bare FastAPI + `dependency_overrides`）、设置定义一致性。
9. 验证：`uv run pytest -q -p no:cacheprovider --import-mode=importlib tests/infra/knowledge_base/domain tests/api/test_knowledge_base_routes.py tests/kernel/config`、ruff、mypy。

## W1-C 前端

写集合：`frontend/src/components/knowledge/**`、`frontend/src/services/api/knowledgeBase.ts`、`frontend/src/utils/knowledgeBaseEvents.ts`、导航相关（`components/layout/AppContent/{types.ts,TabContent.tsx}`、`App.tsx`、`components/panels/SessionSidebar.tsx`、`components/panels/SidebarParts/{SidebarRail.tsx,SessionListContent.tsx}`、`components/skeletons/SidebarSkeleton.tsx`）、`contexts/SettingsContext.tsx`、`hooks/useWebSocket.ts`、`types/settings.ts`、`components/panels/SettingsPanel.constants.ts`、`components/panels/SettingsPanel.tsx`、`utils/backendErrors.ts`、`i18n/locales/{zh,en}.json`、`src/i18n/__tests__/knowledgeKeys.test.ts`、`components/knowledge/__tests__/**`。

1. 按 design §10 实现；样式与组件只复用现有（`[fe §3]` 原语表），不引入新 UI 库。
2. 验证：`cd frontend && pnpm exec tsc --noEmit -p . && pnpm lint`（不新增错误）、`./node_modules/.bin/tsx --test <新增测试文件>`、`pnpm build`。

## W2 复核（新的 check 子代理）

按 `check.jsonl` 读规范，复核三包产出与 design 一致性、跨层契约（后端响应字段 ↔ 前端类型）、权限边界、错误处理、i18n；修复问题；跑全量后端测试（对比基线）、ruff、mypy、前端 tsc/lint/测试/build。

## W3 主代理

本地起后端（本地 Qdrant）+ 前端，走通验收用例并截图；更新 spec；提交；开 PR。
