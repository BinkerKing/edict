# 旧看板说明书

## 目的

这份文档服务于两个目标：

1. 让旧看板功能继续稳定迭代时，有一份可读、可维护的系统说明。
2. 为未来迁移到新看板提供结构化参考，避免迁移时重新从代码里摸索。

当前原则：

- 日常功能开发仍以旧看板为主。
- 每次涉及旧看板的重要功能改动时，同步更新本说明书。
- 本说明书优先记录“系统如何工作”，而不是堆砌零散变更日志。

---

## 当前定位

旧看板是当前项目里功能最完整、业务最成熟的一套实现。

它的核心形态是：

- 一个主服务：
  - `dashboard/server.py`
- 一个主界面：
  - `dashboard/dashboard.html`
- 一组业务脚本：
  - `scripts/*.py`
- 一组运行时数据文件：
  - `data/*.json`

它与 OpenClaw 运行时深度集成，负责把“任务流转、项目治理、AI 协作、配置管理、运营面板”整合到一个工作台中。

---

## 代码边界

### 旧看板核心文件

- 页面主文件：
  - `dashboard/dashboard.html`
- 服务主文件：
  - `dashboard/server.py`
- 朝堂议政相关：
  - `dashboard/court_discuss.py`
- 任务和同步脚本：
  - `scripts/kanban_update.py`
  - `scripts/refresh_live_data.py`
  - `scripts/sync_agent_config.py`
  - `scripts/sync_officials_stats.py`
  - `scripts/run_loop.sh`

### 旧看板主要数据文件

- 任务源：
  - `data/tasks_source.json`
- 看板聚合状态：
  - `data/live_status.json`
- Agent 配置快照：
  - `data/agent_config.json`
- 模型切换记录：
  - `data/model_change_log.json`
- 项目管理数据：
  - `data/project_management.json`
- 将作监数据：
  - `data/jiangzuojian.json`

### 不属于旧看板主实现的目录

前后端分离实验目录已在瘦身阶段移除，不再作为当前实现的一部分维护。
当前请以 `dashboard/*`、`scripts/*`、`data/*` 为准。

---

## 系统运行方式

### 启动链路

旧看板正常运行通常依赖两条进程：

1. 看板服务
   - `python3 dashboard/server.py`
2. 数据刷新循环
   - `bash scripts/run_loop.sh`

其中：

- `server.py` 负责页面、接口、操作响应。
- `run_loop.sh` 负责周期性刷新任务状态、官员状态、统计数据等。

### 外部依赖

旧看板依赖以下运行环境：

- OpenClaw CLI
- `~/.openclaw` 运行目录
- Agent workspaces
- 本地 Python 运行时

说明：

- 旧看板不强依赖 Postgres/Redis 才能启动。
- 它主要依赖本地文件与 OpenClaw 运行时生态。

---

## 功能总览

旧看板当前是一个综合控制台，而不是单纯任务列表。核心模块包括：

- 旨意看板
- 朝堂议政
- 省部调度
- 官员总览
- 模型配置
- 技能配置
- 小任务
- 奏折阁
- 旨库
- 天下要闻
- 鲁班阁
- 将作监

其中最有业务深度的模块主要是：

- 鲁班阁
- 将作监
- 朝堂议政
- 旨意看板

---

## 模块说明

### 1. 旨意看板

职责：

- 展示当前核心任务流转状态
- 支持查看详情、归档、推进、控制任务
- 体现三省六部流程状态

主要依赖：

- `data/tasks_source.json`
- `data/live_status.json`
- `scripts/kanban_update.py`
- `dashboard/server.py`

关键风险：

- 状态流转规则较多，脚本和服务端判断要保持一致
- 一些历史兼容字段可能会造成行为隐蔽耦合

### 2. 朝堂议政

职责：

- 多角色对话、讨论、总结
- 以官员角色参与议题讨论

主要依赖：

- `dashboard/court_discuss.py`
- `dashboard/server.py`

关键风险：

- 依赖 OpenClaw 配置与模型调用能力
- 会话状态与结论摘要逻辑耦合较深

### 3. 鲁班阁

职责：

- 工部治理台
- 项目问题清单治理
- 项目设计文档管理
- 版本控制与发布记录管理

主要依赖：

- `data/project_management.json`
- `dashboard/server.py`
- `dashboard/dashboard.html`

数据组织特点：

- 一个项目下可能包含多个文件夹和问题项
- 包含项目设计目录、版本控制目录等结构化内容

迁移价值：

- 是未来最值得抽象成独立领域模型的模块之一

### 4. 将作监

职责：

- 项目跟进
- 策议司
- 智能看板
- 日常事项与项目节奏治理

主要依赖：

- `data/jiangzuojian.json`
- `dashboard/server.py`
- `dashboard/dashboard.html`

当前结构：

- 项目跟进
- 策议司
- 智能看板

迁移价值：

- 是未来平台化的重要模块，适合拆成独立子系统

### 5. 模型配置 / 技能配置

职责：

- 维护官员模型配置
- 管理 skills 安装与展示

主要依赖：

- `data/agent_config.json`
- `data/model_change_log.json`
- `scripts/sync_agent_config.py`
- `scripts/apply_model_changes.py`

### 6. 省部调度 / 官员总览

职责：

- 展示各 Agent 状态、负载、消耗、在线情况
- 为运维和调度提供全局视图

主要依赖：

- `scripts/sync_officials_stats.py`
- `data/live_status.json`
- `data/agent_config.json`

---

## 数据流说明

### 核心数据流

旧看板的数据流总体是：

1. OpenClaw 运行时产生 agent/session/task 相关信息
2. `scripts/*.py` 从运行时和本地数据中抽取、整理、写入 JSON
3. `dashboard/server.py` 读取 JSON，提供接口与操作能力
4. `dashboard/dashboard.html` 渲染页面并触发交互

### 特点

- 数据是“文件聚合型”而不是“数据库事务型”
- 聚合结果高度依赖同步脚本
- 页面展示与数据预处理之间耦合较深

### 优势

- 结构灵活
- 适合快速增加字段和业务试验

### 局限

- 并发控制能力弱
- 复杂查询和关联分析不够自然
- 后续迁移时需要额外做字段梳理

---

## 接口与实现关系

旧看板不是严格前后端分离系统。

表现为：

- 页面逻辑大量写在 `dashboard/dashboard.html`
- 接口与业务判断大量写在 `dashboard/server.py`
- 共享状态主要来自 `data/*.json`

这意味着：

- 一个需求经常同时要改 HTML、JS、Python 接口、JSON 结构
- 改动快，但边界不天然清晰

---

## 旧看板的优势

- 功能成熟度高
- 业务模块完整
- 迭代速度快
- 跟 OpenClaw 运行时贴合紧
- 适合快速实现新想法

---

## 旧看板的局限

- `server.py` 和 `dashboard.html` 会持续膨胀
- 数据文件越来越多后，维护和审计成本增加
- 模块边界不够清晰
- 前后端未分层，复用能力有限
- 大功能继续增加后，回归风险会上升

---

## 迁移视角下最重要的内容

未来如果迁移到新看板，最需要保留和抽象的是“业务结构”，不是页面样式。

优先应沉淀这些对象：

- 任务
- 项目
- 文件夹
- 问题
- 设计文档
- 版本记录
- 跟进事项
- AI 讨论主题
- 提醒与定时任务

也就是说，迁移时真正重要的是：

- 旧模块分别解决了什么问题
- 数据怎么组织
- 哪些按钮触发哪些动作
- 哪些状态会联动哪些结果

而不仅仅是“页面长什么样”

---

## 推荐的说明书维护方式

以后每次改旧看板功能时，优先补充以下内容：

### 1. 改了哪个模块

例如：

- 鲁班阁
- 将作监
- 旨意看板
- 朝堂议政

### 2. 改动的业务目的

例如：

- 新增功能
- 修复交互问题
- 修复状态流转
- 优化项目结构

### 3. 影响了哪些文件

例如：

- `dashboard/server.py`
- `dashboard/dashboard.html`
- `scripts/kanban_update.py`
- `data/project_management.json`

### 4. 数据结构是否变化

例如：

- 新增字段
- 旧字段语义变化
- 新增目录结构

### 5. 迁移提示

例如：

- 这个功能未来迁移时应拆成独立接口
- 这个模块适合拆成数据库表
- 这个交互现在写死在前端，后面应下沉到服务层

---

## 变更记录模板

后续每次重要改动，可以按下面格式追加到本文末尾。

```md
## 变更记录

### YYYY-MM-DD - 模块名

- 业务目的：
- 修改文件：
- 数据结构变化：
- 交互变化：
- 迁移提示：
```

---

## 当前结论

旧看板仍然是当前项目最适合持续开发的主系统。

短期策略：

- 继续在旧看板承接新需求
- 每次重要改动同步更新本说明书

中期策略：

- 用本说明书持续沉淀模块边界和数据结构
- 为未来迁移到新架构做准备

长期策略：

- 以本说明书为基础，把旧看板里的成熟业务模块逐步抽象迁移

---

## 变更记录

### 2026-04-10 - 初版建立

- 业务目的：为旧看板后续持续开发与未来迁移建立统一说明书
- 修改文件：
  - `docs/legacy-dashboard-manual.md`
- 数据结构变化：无
- 交互变化：无
- 迁移提示：
  - 后续所有旧看板重要改动都应同步补充到本说明书中

### 2026-04-10 - 项目级 OpenClaw 配置隔离（已退役）

- 业务目的：当时用于让旧看板优先读取项目自己的 OpenClaw 配置，降低对本机全局 `~/.openclaw/openclaw.json` 的直接依赖
- 修改文件：
  - `scripts/openclaw_config.py`
  - `scripts/sync_agent_config.py`
  - `scripts/sync_officials_stats.py`
  - `scripts/apply_model_changes.py`
- 数据结构变化：
  - 当时新增 `data/openclaw_project.json` 作为项目级配置源（现已不再作为主来源）
- 交互变化：
  - 模型配置页后续会优先显示项目内定义的 11 部门 agent，而不是仅显示本机全局配置中存在的 agent
- 迁移提示：
  - 该方案已退役，现统一使用全局 `~/.openclaw/openclaw.json` 作为单一配置源

### 2026-04-11 - 统一为全局 OpenClaw 配置

- 业务目的：避免项目级配置与 OpenClaw 会话运行时配置不一致，统一一套配置源
- 修改文件：
  - `scripts/openclaw_config.py`
  - `scripts/sync_agent_config.py`
  - `scripts/apply_model_changes.py`
- 数据结构变化：
  - 不再依赖 `data/openclaw_project.json` 作为运行主配置
- 交互变化：
  - 看板与同步脚本读取的 agent/model/workspace 与 OpenClaw 运行时一致
- 迁移提示：
  - 若历史环境仍保留 `openclaw_project.json`，仅作为备份，不应再作为主配置入口
