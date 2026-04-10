# 长上下文保真机制（通用）

本机制用于处理会话过长导致的上下文溢出问题，目标是：

- 在切换新会话时尽量不丢关键信息
- 把“历史聊天”转成结构化上下文胶囊（capsule）
- 支持按 token 阈值批量扫描并提前生成续接包

核心脚本：`scripts/context_continuity.py`

---

## 0. 会话策略分流（开发必读）

在需求进入时先做“会话策略”判定，再决定怎么执行。统一规则如下：

1. 短任务/临时对话
- 特征：一次性问答、无跨阶段依赖、无需复盘。
- 策略：新开会话，完成即关闭，不保留上下文。

2. 中等任务
- 特征：需要 2-5 轮推进，但依赖较少。
- 策略：保留当前会话；建议设置“定时收口”（例如 30-60 分钟无新动作即结束）。

3. 长链路任务
- 特征：多角色协同、跨阶段交付、需要追溯决策。
- 策略：按任务维度持续同一会话；每个里程碑执行一次 `capture` 沉淀 capsule。

4. 超长任务/高 token 风险
- 特征：上下文接近模型窗口、会话内容持续膨胀。
- 策略：执行 `context-continuity` 生成 capsule + resume，再切新会话续跑。

建议判定信号（需求受理时即可评估）：
- 任务复杂度（模块数量、变更范围）
- 依赖数量（跨部门/跨系统）
- 预估轮次（是否超过单会话舒适区）
- 是否要求跨阶段追溯（审计/复盘）

---

## 1. 快速使用

### 1) 为某个 agent 生成 capsule

```bash
cd /Users/binlian/Documents/Github/edict
python3 scripts/context_continuity.py capture --agent gongbu --task-id JJC-20260411-001
```

输出会写入：

- `data/context_capsules/gongbu/*.json`

### 2) 生成新会话续接提示词

```bash
python3 scripts/context_continuity.py resume-prompt \
  --capsule data/context_capsules/gongbu/<capsule>.json \
  --output data/context_capsules/gongbu/<capsule>.resume.md
```

把 `.resume.md` 里的内容作为新会话的首条输入即可续接。

### 3) 扫描高风险会话（按 token）

```bash
python3 scripts/context_continuity.py scan --token-threshold 120000
```

会对各 agent 最新会话扫描，超过阈值自动生成：

- `*.json`（capsule）
- `*.resume.md`（续接提示词）

---

## 2. 与 PRD/Ralph 联动（推荐）

当任务复杂且周期长时，建议把 `prd/ralph` 与 capsule 组合：

1. 用 `prd` skill 生成需求文档（目标与边界清晰）
2. 用 `ralph` skill 转为 `prd.json`（拆成小故事）
3. 每完成一个故事或接近 token 阈值时，执行一次 `capture`
4. 开新会话时使用 `resume-prompt` 续接，并继续下一个故事

这样“长期上下文”会拆成两层记忆：

- 结构化任务记忆：`prd.json`（计划层）
- 会话执行记忆：`capsule`（执行层）

---

## 3. 机制边界

- 该机制不直接修改 OpenClaw 会话，仅做“抽取与续接包生成”。
- `scan` 只扫描每个 agent 的最新会话，不会重写历史数据。
- 若希望全自动“到阈值就切新会话”，可在后续增加调度器联动（建议分步上线）。

---

## 4. 团队执行建议（与 PRD/Ralph 组合）

推荐统一采用“双层记忆”：

- 计划层：`prd/ralph` 产出 `prd.json`（故事拆解、优先级、验收）
- 执行层：`context-continuity` 产出 `capsule/resume`（会话续接与保真）

最小执行闭环：
1. 需求进入时先判定会话策略（本页第 0 节）。
2. 中长任务先用 `prd/ralph` 拆小故事。
3. 每个故事结束或接近阈值时执行 `capture`。
4. 换会话时把 `resume.md` 作为首条输入继续执行。
