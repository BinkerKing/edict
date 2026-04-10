# Agent 隔离路由规范（第一版）

用于解决“按钮互相污染上下文、项目间串会话”的问题。

---

## 1. 设计目标

- 按钮隔离：不同 action 不共享同一上下文。
- 项目隔离：不同 project 不共享同一上下文。
- 统一入口：所有需要隔离的调用统一走一个路由器，不再各写各的 session 逻辑。
- 可治理：有统一 registry，可做 list/gc。

---

## 2. 统一规范

### 2.1 Scope 约定

使用三元组定义隔离范围：

- `projectId`
- `domain`（如 `pm`）
- `action`（如 `design-requirements` / `version-generate` / `gongbu-review-review`）

Scope key：

```text
{projectId}:{domain}:{action}
```

### 2.2 Runtime Agent 约定

每个 scope 路由到一个独立 runtime agent，命名：

```text
{baseAgent}__{domain}__{action}__{hash8}
```

例如：

- `gongbu__pm__design-requirements__8a21f3c1`
- `gongbu__pm__version-generate__e39082af`

### 2.3 Registry 约定

注册表文件：

- `data/agent_isolation_registry.json`

记录：

- `scopeKey`
- `baseAgentId`
- `runtimeAgentId`
- `projectId`
- `domain`
- `action`
- `createdAt`
- `lastUsedAt`

---

## 3. 当前接入点（v1）

已接入以下 PM 入口（base agent = `gongbu`）：

- 工部生成（设计文档）：按 `design-{section}` 路由
- 更新版本：按 `version-generate` 路由
- 工部复审：按 `gongbu-review-{mode}` 路由

说明：这三类操作现在会落到不同 runtime agent，不再共用 `gongbu main` 会话。

---

## 4. 运维脚本

脚本：

- `scripts/agent_isolation_gc.py`

常用命令：

```bash
cd /Users/binlian/Documents/Github/edict

# 查看当前隔离路由
python3 scripts/agent_isolation_gc.py list

# 预演清理（默认阈值 7 天）
python3 scripts/agent_isolation_gc.py gc --dry-run

# 实际清理
python3 scripts/agent_isolation_gc.py gc --max-idle-days 7
```

---

## 5. 推广建议

后续新增按钮时，统一按以下流程接入：

1. 定义 action 名称（稳定、可读）。
2. 调用统一隔离路由函数，获取 runtime agent。
3. 使用 runtime agent 执行，不再传“伪 session id”做隔离。
4. 通过 registry + gc 管理生命周期。
