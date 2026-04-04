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
DISPATCH_DEDUP_SECONDS = 180
STALL_NUDGE_SECONDS = 120
NUDGE_COOLDOWN_SECONDS = 180
ACTIVE_GRACE_SECONDS = 120
NUDGE_STATES = {"Pending", "Taizi", "Zhongshu", "Menxia", "Assigned", "Doing", "Next", "Review"}


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

    lines = [
        f"📋 调度层派发 · {agent_label}",
        f"任务ID: {task.get('id', '')}",
        f"标题: {task.get('title', '')}",
        f"当前状态: {task.get('state', '')}",
        f"当前承办: {task.get('org', '')}",
        f"当前动态: {task.get('now', '')}",
        f"阻塞: {task.get('block', '无')}",
        "",
        "任务要求:",
        task.get("ac", "") or task.get("title", ""),
        "",
        "最近流转:",
        flow_text,
    ]
    return "\n".join(lines).strip()


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
        if state in NUDGE_STATES:
            stalled = task_stalled_seconds(task)
            nudge_key = f"{task_id}:{agent_id}:{state}"
            if stalled < STALL_NUDGE_SECONDS:
                return False
            if agent_recently_active_for_task(agent_id, task_id):
                return False
            if scheduler_says_dispatched_recently(task, agent_id, state):
                return False
            if not _can_nudge(nudge_key):
                return False
            msg = (
                f"📢 调度催办\n"
                f"任务ID: {task_id}\n"
                f"当前状态: {state}\n"
                f"当前动态: {task.get('now', '')}\n"
                f"已停滞约 {stalled} 秒，请继续推进并更新看板。"
            )
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
