# 旧工作流与瘦身梳理（2026-04-12）

本文用于支持两类任务：
- 三省六部余孽清理（识别与归类）
- 未启用新看板瘦身（删除前置核查）

## 1. 当前在役链路（确认仍在使用）

- 看板服务入口：`dashboard/server.py`
- 主界面：`dashboard/dashboard.html`
- 本地启动脚本：`scripts/edict_services.sh`（实际启动 `dashboard/server.py`）
- PM/研发部任务数据：`data/project_management.json`

结论：当前业务主链路仍是旧看板（`dashboard/*`）。

## 2. 旧工作流相关代码检索结果（归类）

### A. 仍在使用（保留）

- `dashboard/dashboard.html`
  - 含会话筛选和流程态映射（中书/门下/尚书/六部等历史字段）
  - 同时承担现行“研发部/PM小组/人事部/藏经阁”等新文案兼容
- `dashboard/server.py`
  - 包含部门映射、会话来源映射、PM任务接口、学习/策略接口
- `scripts/dispatch_pending_agents.py`、`scripts/kanban_update.py`
  - 仍与任务流转和看板状态更新相关

### B. 疑似废弃或低频入口（建议进入删除候选）

- `dashboard/court_discuss.py`
  - 仍保留完整“三省六部讨论”逻辑，当前主界面未作为核心入口使用
- `agents/gongbu/SOUL.md`
  - 已完成 `gongbu -> rnd` 迁移后，目录仍保留旧工部 SOUL
- `agents/libu/SOUL.md`、`agents/libu_hr/SOUL.md`、`agents/hubu/SOUL.md`、`agents/xingbu/SOUL.md`
  - 文案仍有“尚书/三省六部”历史术语，需按现行命名策略评估是否保留兼容

### C. 文档与历史资料（可分批清理）

- `docs/wechat.md`、`docs/wechat-article.md`
- `docs/getting-started.md`（部分章节仍是三省六部旧叙述）
- `docs/remote-skills-guide.md`、`docs/remote-skills-quickstart.md`（仍有旧名词）
- `data/regression-backup-*`（历史回归备份数据）

## 3. 新看板/分离式代码现状（瘦身核查）

前后端分离实验目录在当次瘦身中已整体移除。
从 `scripts/edict_services.sh` 可见，当前默认仅启动 `dashboard/server.py`。

## 4. 删除候选清单（带前置条件）

### P0（低风险，可先动）

- 文档中的历史宣传与旧术语（`docs/wechat*` 等）
- 历史回归备份目录 `data/regression-backup-*`（建议先打包归档）

### P1（中风险，需要调用链确认）

- `dashboard/court_discuss.py`
- `agents/gongbu/`（旧目录）

前置条件：
- 全局检索确认没有运行时 import/调用
- 看板页面中不存在对应入口按钮
- 保留可回滚备份（Git tag 或打包）

### P2（高风险，需专项评审后删）

- 分离式目录（已完成删除）

前置条件：
- 明确未来不再启用“新看板/前后端分离架构”
- 安装脚本、README、CI 不再引用
- 通过一次完整启动与回归（仅旧看板）后再删除

## 5. 建议执行节奏

1. 先清理 P0（文档/备份）。
2. 对 P1 做一次调用链确认后删除。
3. P2 已完成，后续仅做残留引用与文档收口。
