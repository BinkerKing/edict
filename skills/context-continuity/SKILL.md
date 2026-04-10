---
name: context-continuity
description: "当会话过长、接近溢出或需要跨会话续接时，生成上下文胶囊并产出续接提示词。触发词：上下文太长、会话溢出、续接会话、保留上下文、context capsule。"
user-invocable: true
---

# Context Continuity

用于在长会话下保留信息密度，避免换会话后“丢脑子”。

## 使用场景

- 当前会话太长，担心上下文溢出。
- 需要换一个新会话继续做同一任务。
- 需要给其他 agent 交接“高密度上下文”。

## 标准流程

1. 先生成 capsule（结构化摘要）

```bash
cd /Users/binlian/Documents/Github/edict
python3 scripts/context_continuity.py capture --agent <agent_id> --task-id <task_id>
```

2. 再生成续接提示词

```bash
python3 scripts/context_continuity.py resume-prompt \
  --capsule data/context_capsules/<agent_id>/<capsule>.json \
  --output data/context_capsules/<agent_id>/<capsule>.resume.md
```

3. 把 `.resume.md` 作为新会话首条输入，继续执行。

## 批量预防（可选）

当你希望提前发现高风险会话：

```bash
python3 scripts/context_continuity.py scan --token-threshold 120000
```

会为超过阈值的最新会话生成 capsule 和 resume 文件。

## 输出纪律

- 先给出 capsule 文件路径
- 再给出 resume 文件路径
- 最后给出下一步可执行动作（3 条以内）
