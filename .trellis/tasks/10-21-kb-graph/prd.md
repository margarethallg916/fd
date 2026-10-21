# 知识图谱（PR4）

> 父任务 `10-21-personal-knowledge-base`（决策 3）。设计见 `design.md`。叠加在 PR3 之上。

## Goal

库主可以为知识库开启图谱：用管理端配置的大模型从分块中抽取实体与关系（存 Mongo，向量存 Qdrant），图谱参与检索增强（PPR + RRF），并在详情页「图谱」Tab 可视化浏览实体、关系与原文提及。

## Requirements

- R1 设置 `KB_ALLOW_GRAPH`、`KB_GRAPH_LLM_MODEL_ID`、`KB_GRAPH_CONCURRENCY`、`KB_GRAPH_MAX_CHUNKS_PER_KB`。
- R2 开启/构建/关闭接口；复用队列（任务类型 graph），增量与删除回收。
- R3 图增强检索（检索测试开关 + 工具自动启用），异常退化。
- R4 可视化（React Flow + 纯函数力导向布局）与实体详情面板。
- R5 中英文案完整。

## Acceptance Criteria

- [ ] 测试覆盖 design §8；全量测试失败集合不超过基线；ruff/mypy/tsc/lint/build 通过。
- [ ] 本地用 mock/可用 LLM 走通：开启图谱 → 构建完成 → 图谱 Tab 显示节点与边 → 图谱增强检索返回带图谱来源的结果。
