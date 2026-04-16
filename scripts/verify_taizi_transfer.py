#!/usr/bin/env python3
import json
import pathlib
import re
from datetime import datetime, timezone

from file_lock import atomic_json_read, atomic_json_write
from openclaw_config import OPENCLAW_AGENTS_HOME

BASE = pathlib.Path(__file__).resolve().parent.parent
TASKS_FILE = BASE / "data" / "tasks_source.json"
TAIZI_SESSIONS = OPENCLAW_AGENTS_HOME / "taizi" / "sessions"
ZHONGSHU_SESSIONS = OPENCLAW_AGENTS_HOME / "zhongshu" / "sessions"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _scan_files_for_task(root: pathlib.Path, task_id: str) -> bool:
    if not root.exists():
        return False
    for p in root.glob("*.jsonl"):
        try:
            if task_id in p.read_text(encoding="utf-8", errors="ignore"):
                return True
        except Exception:
            continue
    return False


def _latest_taizi_session_for_task(task_id: str) -> pathlib.Path | None:
    if not TAIZI_SESSIONS.exists():
        return None
    files = sorted(TAIZI_SESSIONS.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if task_id in text:
            return p
    return None


def _extract_actions_and_claims(session_file: pathlib.Path, task_id: str):
    actions = []
    claims_transfer = False
    if not session_file or not session_file.exists():
        return actions, claims_transfer

    for line in session_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("type") != "message":
            continue
        msg = obj.get("message") or {}
        role = msg.get("role")
        content = msg.get("content") or []

        if role == "assistant":
            for c in content:
                if c.get("type") == "toolCall":
                    name = c.get("name", "")
                    args = c.get("arguments", {})
                    cmd = ""
                    if isinstance(args, dict):
                        cmd = str(args.get("command", ""))
                    if task_id in cmd or name in ("sessions_spawn", "sessions_send", "exec"):
                        actions.append({"tool": name, "command": cmd[:220]})
                if c.get("type") == "text":
                    txt = str(c.get("text", ""))
                    if task_id in txt and re.search(r"转交.*中书省|中书省.*起草", txt):
                        claims_transfer = True
    return actions, claims_transfer


def _append_flow_once(task: dict, remark: str):
    flow = task.setdefault("flow_log", [])
    if flow and flow[-1].get("remark") == remark:
        return
    flow.append({
        "at": now_iso(),
        "from": "太子调度",
        "to": task.get("org", "太子"),
        "remark": remark,
    })


def main():
    tasks = atomic_json_read(TASKS_FILE, [])
    if not isinstance(tasks, list):
        return

    changed = False

    for t in tasks:
        task_id = str(t.get("id", ""))
        if not task_id.startswith("JJC-"):
            continue
        if t.get("state") != "Taizi":
            continue

        taizi_session = _latest_taizi_session_for_task(task_id)
        actions, claims_transfer = _extract_actions_and_claims(taizi_session, task_id)
        zhongshu_has_task = _scan_files_for_task(ZHONGSHU_SESSIONS, task_id)

        audit = {
            "checkedAt": now_iso(),
            "sessionFile": str(taizi_session) if taizi_session else "",
            "actions": actions,
            "claimsTransfer": claims_transfer,
            "zhongshuSessionHasTask": zhongshu_has_task,
        }
        t["_taiziAudit"] = audit
        changed = True

        if claims_transfer and not zhongshu_has_task:
            t["now"] = "太子口头称已转交，但未检测到中书省会话接单"
            t["block"] = "未完成真实转交中书省"
            _append_flow_once(t, "🧪 验真失败：太子宣称已转交，但中书省无接单会话")
        elif actions:
            # 把最新动作写到 now，便于看板直接看到“做了什么”
            last = actions[-1]
            tool = last.get("tool", "")
            cmd = last.get("command", "")
            t["now"] = f"太子动作: {tool} {cmd[:100]}".strip()
        else:
            t["now"] = "太子暂无可核验动作（未发现有效 toolCall）"

        t["updatedAt"] = now_iso()

    if changed:
        atomic_json_write(TASKS_FILE, tasks)


if __name__ == "__main__":
    main()
