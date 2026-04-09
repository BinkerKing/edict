#!/usr/bin/env python3
import json
import logging
import pathlib
import subprocess
import datetime

from file_lock import atomic_json_read, atomic_json_update

log = logging.getLogger("dispatch")
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(name)s] %(message)s', datefmt='%H:%M:%S')

BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / "data"
TASKS_PATH = DATA / "tasks_source.json"
NUDGE_STATE_PATH = DATA / "dispatch_nudge_state.json"
SESSIONS_ROOT = pathlib.Path.home() / ".openclaw" / "agents"

ORG_AGENT_MAP = {
    "尚书省": "shangshu",
    "礼部": "libu",
    "户部": "hubu",
    "兵部": "bingbu",
    "刑部": "xingbu",
    "工部": "gongbu",
    "吏部": "libu_hr",
}
STATE_AGENT_MAP = {
    "Pending": ("taizi", "太子"),
    "Taizi": ("taizi", "太子"),
    "Zhongshu": ("zhongshu", "中书省"),
    "Menxia": ("menxia", "门下省"),
    "Assigned": ("shangshu", "尚书省"),
    "Review": ("shangshu", "尚书省"),
}
DISPATCH_DEDUP_SECONDS = 300
STALL_NUDGE_SECONDS = 180
NUDGE_COOLDOWN_SECONDS = 900
FAST_STALL_SECONDS = 180
FAST_NUDGE_COOLDOWN_SECONDS = 240
WRAPUP_STALL_SECONDS = 120
WRAPUP_NUDGE_COOLDOWN_SECONDS = 180
ACTIVE_GRACE_SECONDS = 300
# 催办只针对“等待承接/流转”阶段，执行中与复核阶段不催办，避免干扰正常工作。
NUDGE_STATES = {"Pending", "Taizi", "Zhongshu", "Menxia", "Assigned", "Review"}


def load_tasks():
    tasks = atomic_json_read(TASKS_PATH, [])
    return tasks if isinstance(tasks, list) else []


def session_contains_task(agent_id: str, task_id: str) -> bool:
    session_dir = SESSIONS_ROOT / agent_id / "sessions"
    if not session_dir.exists():
        return False
    for path in session_dir.glob("*.jsonl"):
        try:
            if task_id in path.read_text(encoding="utf-8", errors="ignore"):
                return True
        except Exception:
            continue
    return False


def build_message(task: dict, agent_label: str) -> str:
    flow_log = task.get("flow_log") or []
    recent_flow = []
    for item in flow_log[-6:]:
        recent_flow.append(f'- {item.get("from", "-")} -> {item.get("to", "-")}: {item.get("remark", "")}')
    flow_text = "\n".join(recent_flow) if recent_flow else "- 无"

    task_id = task.get("id", "")
    title = task.get("title", "")
    state = task.get("state", "")
    hard_actions = _hard_action_hints(task_id, title, state, task.get("org", ""))

    lines = [
        f"📋 调度层派发 · {agent_label}",
        f"任务ID: {task_id}",
        f"标题: {title}",
        f"当前状态: {state}",
        f"当前承办: {task.get('org', '')}",
        f"当前动态: {task.get('now', '')}",
        f"阻塞: {task.get('block', '无')}",
        "",
        "任务要求:",
        task.get("ac", "") or task.get("title", ""),
        "",
        "最近流转:",
        flow_text,
        "",
        "强制执行规则:",
        "- 先执行工具命令，再回复文本；禁止只写口头进展。",
        "- 回复需包含已执行命令及结果证据（成功/失败）。",
        hard_actions,
    ]
    return "\n".join(lines).strip()


def _hard_action_hints(task_id: str, title: str, state: str, org: str) -> str:
    if state == "Zhongshu":
        return (
            "第一步先执行：\n"
            f"python3 scripts/kanban_update.py progress {task_id} \"中书省已接旨，正在起草并准备提交门下省\" \"接旨✅|起草方案🔄|门下审议|转尚书\"\n"
            "第二步执行（短命令流转，禁止长时间挂起）：\n"
            f"python3 scripts/kanban_update.py flow {task_id} \"中书省\" \"门下省\" \"📋 方案提交审议\"\n"
            f"python3 scripts/kanban_update.py state {task_id} Menxia \"方案提交门下省审议\""
        )
    if state == "Menxia" or org == "门下省":
        return (
            "第一步先执行：\n"
            f"python3 scripts/kanban_update.py progress {task_id} \"门下省审议中，正在给出准奏/封驳\" \"可行性审查🔄|完整性审查|风险评估|出具结论\"\n"
            "然后二选一执行：\n"
            f"A 准奏: python3 scripts/kanban_update.py state {task_id} Assigned \"门下省准奏\"\n"
            f"   + python3 scripts/kanban_update.py flow {task_id} \"门下省\" \"中书省\" \"✅ 准奏\"\n"
            f"B 封驳: python3 scripts/kanban_update.py state {task_id} Zhongshu \"门下省封驳，退回中书省\"\n"
            f"   + python3 scripts/kanban_update.py flow {task_id} \"门下省\" \"中书省\" \"❌ 封驳：<理由>\""
        )
    if state == "Review":
        return (
            "当前处于 Review 收口阶段，请先核对 todos 与产出文件后执行：\n"
            f"python3 scripts/kanban_update.py done {task_id} \"/Users/binkerking/Documents/GitHub/edict/reports/README_flow_report.md\" \"README流程化归并报告已完成\"\n"
            "若 done 被拒绝，请按拒绝原因先补齐 todos/产出路径，再重试。"
        )
    if state == "Assigned" or org == "尚书省":
        return (
            "第一步先执行：\n"
            f"python3 scripts/kanban_update.py state {task_id} Doing \"尚书省派发任务给六部\"\n"
            f"python3 scripts/kanban_update.py flow {task_id} \"尚书省\" \"六部\" \"派发：{title or task_id}\"\n"
            "第二步按需调用六部 agent 执行，完成后：\n"
            f"python3 scripts/kanban_update.py state {task_id} Review \"尚书省汇总完成，提交复核\"\n"
            "若最终结果已齐全，再执行 done。"
        )
    return "请按本省 SOUL 先执行至少 1 条 state/flow/progress 命令，再给回执。"


def _parse_iso(ts: str | None):
    if not ts or not isinstance(ts, str):
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def task_stalled_seconds(task: dict) -> int:
    ts = task.get("updatedAt")
    dt = _parse_iso(ts)
    if not dt:
        return 10**9
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    return max(0, int((now_dt - dt).total_seconds()))


def agent_recently_active_for_task(agent_id: str, task_id: str, within_seconds: int = ACTIVE_GRACE_SECONDS) -> bool:
    """保守判断：若该 agent 最近有与 task 相关会话活动，则视为正在工作，禁止催办。"""
    root = SESSIONS_ROOT / agent_id / "sessions"
    if not root.exists():
        return False
    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    for fp in root.glob("*.jsonl"):
        try:
            text = fp.read_text(encoding="utf-8", errors="ignore")
            if task_id not in text:
                continue
            mtime = fp.stat().st_mtime
            if now_ts - mtime <= within_seconds:
                return True
        except Exception:
            continue
    return False


def _can_nudge(key: str) -> bool:
    state = atomic_json_read(NUDGE_STATE_PATH, {})
    if not isinstance(state, dict):
        state = {}
    last = _parse_iso(state.get(key))
    if not last:
        return True
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    return int((now_dt - last).total_seconds()) >= NUDGE_COOLDOWN_SECONDS


def _has_reject_context(task: dict) -> bool:
    now_text = str(task.get("now", "") or "")
    if any(k in now_text for k in ("封驳", "驳回", "修订", "修正")):
        return True
    flow = task.get("flow_log") or []
    for item in reversed(flow[-4:]):
        remark = str(item.get("remark", "") or "")
        if any(k in remark for k in ("封驳", "驳回")):
            return True
    return False


def _all_todos_completed(task: dict) -> bool:
    todos = task.get("todos")
    if not isinstance(todos, list) or not todos:
        return False
    return all((td or {}).get("status") == "completed" for td in todos)


def _is_doing_wrapup_candidate(task: dict) -> bool:
    # 只在尚书省执行阶段做收口催办，避免干扰六部正常执行。
    if task.get("state") != "Doing":
        return False
    if task.get("org") != "尚书省":
        return False
    return _all_todos_completed(task)


def _stall_nudge_seconds(task: dict, state: str) -> int:
    # 执行态收口：todos 全完成但仍停在 Doing 时，快速催办尚书省收尾。
    if _is_doing_wrapup_candidate(task):
        return WRAPUP_STALL_SECONDS
    # 准奏后 Assigned 是关键衔接点，缩短催办窗口避免卡在“已准奏待执行”。
    if state == "Assigned":
        return FAST_STALL_SECONDS
    # 门下封驳回退后的修订态改为快催办，缩短“停摆可见时间”。
    if state in ("Zhongshu", "Menxia") and _has_reject_context(task):
        return FAST_STALL_SECONDS
    return STALL_NUDGE_SECONDS


def _can_nudge_with_task(key: str, task: dict, state: str) -> bool:
    state_data = atomic_json_read(NUDGE_STATE_PATH, {})
    if not isinstance(state_data, dict):
        state_data = {}
    last = _parse_iso(state_data.get(key))
    if not last:
        return True
    stall_window = _stall_nudge_seconds(task, state)
    if stall_window == WRAPUP_STALL_SECONDS:
        cooldown = WRAPUP_NUDGE_COOLDOWN_SECONDS
    elif stall_window == FAST_STALL_SECONDS:
        cooldown = FAST_NUDGE_COOLDOWN_SECONDS
    else:
        cooldown = NUDGE_COOLDOWN_SECONDS
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    return int((now_dt - last).total_seconds()) >= cooldown


def _mark_nudged(key: str) -> None:
    def modifier(data):
        if not isinstance(data, dict):
            data = {}
        data[key] = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
        return data
    atomic_json_update(NUDGE_STATE_PATH, modifier, {})


def scheduler_says_dispatched_recently(task: dict, agent_id: str, state: str) -> bool:
    sched = task.get("_scheduler")
    if not isinstance(sched, dict):
        sched = task.get("scheduler")
    if not isinstance(sched, dict):
        return False
    if sched.get("lastDispatchAgent") != agent_id:
        return False
    if sched.get("lastDispatchState") != state:
        return False
    if sched.get("lastDispatchStatus") not in ("queued", "success"):
        return False

    last_at = _parse_iso(sched.get("lastDispatchAt"))
    if not last_at:
        return True
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    elapsed = max(0, int((now_dt - last_at).total_seconds()))
    return elapsed < DISPATCH_DEDUP_SECONDS


def maybe_dispatch(task: dict) -> bool:
    state = task.get("state", "")
    agent_id = None
    agent_label = None

    # 统一入口目标：优先由 dashboard/server.py 调度。
    # 对 CLI/SOUL 直接写状态的任务（无 scheduler 派发记录）保留兜底派发能力。
    if state in STATE_AGENT_MAP:
        agent_id, agent_label = STATE_AGENT_MAP[state]
    elif state in ("Doing", "Next"):
        org = task.get("org", "")
        agent_id = ORG_AGENT_MAP.get(org)
        agent_label = org

    if not agent_id:
        return False

    task_id = task.get("id", "")
    if not task_id or task_id.startswith("OC-"):
        return False

    if scheduler_says_dispatched_recently(task, agent_id, state):
        return False

    has_session = session_contains_task(agent_id, task_id)
    if has_session:
        # 程序纠偏：任务卡住时，仅做“催办唤醒”，不代替执行
        should_nudge = state in NUDGE_STATES or _is_doing_wrapup_candidate(task)
        if should_nudge:
            stalled = task_stalled_seconds(task)
            nudge_key = f"{task_id}:{agent_id}:{state}"
            stall_threshold = _stall_nudge_seconds(task, state)
            if stalled < stall_threshold:
                return False
            # Assigned/Review 为关键收口点：超过阈值后不再受“最近活跃”保护阻塞，避免假活跃卡死。
            if state not in ("Assigned", "Review"):
                if agent_recently_active_for_task(agent_id, task_id):
                    return False
            if scheduler_says_dispatched_recently(task, agent_id, state):
                return False
            if not _can_nudge_with_task(nudge_key, task, state):
                return False
            msg = (
                f"📢 调度催办\n"
                f"任务ID: {task_id}\n"
                f"当前状态: {state}\n"
                f"当前动态: {task.get('now', '')}\n"
                f"已停滞约 {stalled} 秒（阈值 {stall_threshold} 秒），请继续推进并更新看板。\n"
                f"执行前请先核对看板实时状态；若已不在 {state}，仅回报现状，禁止写回旧阶段进展。\n"
                f"本轮必须先真实执行命令（state/flow/progress 之一）再回复。"
            )
            if _is_doing_wrapup_candidate(task):
                msg += "\n检测到 todos 已全部完成但任务仍在 Doing，请立即执行收口（转 Review 或 Done）。"
            else:
                msg += "\n" + _hard_action_hints(task_id, task.get("title", ""), state, task.get("org", ""))
            cmd = ["openclaw", "agent", "--agent", agent_id, "-m", msg, "--timeout", "240"]
            result = subprocess.run(
                cmd,
                cwd=str(BASE),
                capture_output=True,
                text=True,
                timeout=260,
            )
            if result.returncode != 0:
                log.warning("nudge failed for %s -> %s: %s", task_id, agent_id, result.stderr.strip())
                return False
            _mark_nudged(nudge_key)
            log.info("nudged stalled task %s -> %s (state=%s stalled=%ss)", task_id, agent_id, state, stalled)
            return True
        return False

    msg = build_message(task, agent_label or agent_id)
    cmd = [
        "openclaw",
        "agent",
        "--agent",
        agent_id,
        "-m",
        msg,
        "--timeout",
        "240",
    ]
    result = subprocess.run(
        cmd,
        cwd=str(BASE),
        capture_output=True,
        text=True,
        timeout=260,
    )
    if result.returncode != 0:
        log.warning("dispatch failed for %s -> %s: %s", task_id, agent_id, result.stderr.strip())
        return False

    log.info("dispatched %s -> %s", task_id, agent_id)
    return True


def main():
    dispatched = 0
    for task in load_tasks():
        if maybe_dispatch(task):
            dispatched += 1
    log.info("dispatch scan done, dispatched=%s", dispatched)


if __name__ == "__main__":
    main()
