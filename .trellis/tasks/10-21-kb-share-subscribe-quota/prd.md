# KB 分享 / 公开订阅 / 配额 / 文件库导入（PR2）

> 父任务 `10-21-personal-knowledge-base`（决策 6/7/8/9/10）。设计见 `design.md`。叠加在 PR1 之上。

## Goal

知识库可以按工号分享给同事（查看者/编辑者），可以公开到知识广场供他人订阅；所有数量/存储/文件大小受管理员 RoleLimits 与全局默认管控；`/files` 文件库的文件可一键转到知识库；分享与审核结果有站内通知。

## Requirements

- R1 角色矩阵 owner/editor/viewer/subscriber/visitor（design §1），无权一律 404。
- R2 工号校验接口只回 `username/exists/active`；分享不需对方接受，直接出现在「共享知识库」并通知。
- R3 公开：默认免审，`KB_PUBLIC_REQUIRES_REVIEW` 开启时需审核；管理员可下架；取消公开后订阅者立即失去访问。
- R4 配额四项 + 全局默认 + 多角色取最宽松；知识库存储单独记账。
- R5 文件库导入：复制 + sha256 去重 + 配额；上传对话框「从文件库导入」页签；文件卡片菜单「转到知识库」。
- R6 管理端审核页、RolesPanel 知识库限额区块、新设置项。
- R7 中英文案完整。

## Acceptance Criteria

- [ ] 后端测试覆盖 design §7 全部条目；全量测试失败集合不超过基线。
- [ ] ruff/mypy 不新增错误；前端 tsc/lint/build 通过，新增测试通过。
- [ ] 本地两账号手动走通：A 分享给 B（编辑者）→ B 收到通知并能上传；A 公开 → C 在广场订阅 → A 取消公开 → C 失去访问；配额超限提示；文件库转入。
