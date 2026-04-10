#!/usr/bin/env python3
"""
Context Continuity Toolkit

通用“长上下文保真”工具：
1) capture: 从 OpenClaw 会话抽取结构化 context capsule（JSON）
2) resume-prompt: 从 capsule 生成新会话续接提示词（Markdown）
3) scan: 扫描高 token 会话并自动生成 capsule + 续接提示词
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
from typing import Any


ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"

MAX_LINE = 220


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def trunc(text: str, n: int = MAX_LINE) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) <= n:
        return text
    return text[: n - 3] + "..."


def uniq_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        it = (raw or "").strip()
        if not it:
            continue
        key = it.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def load_sessions_index(sessions_json_path: pathlib.Path) -> dict[str, dict[str, Any]]:
    if not sessions_json_path.exists():
        return {}
    try:
        data = json.loads(sessions_json_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_agent_index(sessions_root: pathlib.Path, agent_id: str) -> dict[str, dict[str, Any]]:
    # 新结构：~/.openclaw/agents/<agent>/sessions/sessions.json
    per_agent = sessions_root / agent_id / "sessions" / "sessions.json"
    index = load_sessions_index(per_agent)
    if index:
        return index

    # 兼容旧结构：~/.openclaw/agents/sessions.json（全局扁平索引）
    global_index = load_sessions_index(sessions_root / "sessions.json")
    if not global_index:
        return {}
    out: dict[str, dict[str, Any]] = {}
    prefix = f"agent:{agent_id}:"
    for key, row in global_index.items():
        if not isinstance(row, dict):
            continue
        if str(key).startswith(prefix):
            out[key] = row
    return out


def normalize_session_file(path_raw: str) -> pathlib.Path:
    p = pathlib.Path(path_raw)
    if p.exists():
        return p
    # 兼容老机器快照路径
    text = str(path_raw)
    if text.startswith("/Users/binkerking/.openclaw"):
        alt = pathlib.Path(text.replace("/Users/binkerking/.openclaw", f"{pathlib.Path.home()}/.openclaw"))
        if alt.exists():
            return alt
    return p


def extract_text_from_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and c.get("text"):
            parts.append(str(c.get("text")))
    return "\n".join(parts).strip()


def parse_session_jsonl(session_file: pathlib.Path, max_events: int = 4000) -> list[dict[str, str]]:
    if not session_file.exists():
        return []
    rows: list[dict[str, str]] = []
    try:
        lines = session_file.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return rows

    if max_events > 0 and len(lines) > max_events:
        lines = lines[-max_events:]

    for ln in lines:
        try:
            item = json.loads(ln)
        except Exception:
            continue
        msg = item.get("message") or {}
        role = msg.get("role")
        ts = str(item.get("timestamp") or "")

        if role == ROLE_USER:
            text = extract_text_from_content(msg.get("content"))
            if text:
                rows.append({"role": ROLE_USER, "at": ts, "text": text.strip()})
            continue

        if role == ROLE_ASSISTANT:
            text = extract_text_from_content(msg.get("content"))
            text = text.replace("[[reply_to_current]]", "").strip()
            if text:
                rows.append({"role": ROLE_ASSISTANT, "at": ts, "text": text})
            continue

        if role == "toolResult":
            tool_name = str(msg.get("toolName") or "-")
            text = extract_text_from_content(msg.get("content"))
            if not text:
                text = "tool finished"
            rows.append(
                {
                    "role": ROLE_TOOL,
                    "at": ts,
                    "text": f"{tool_name}: {trunc(text, 180)}",
                }
            )
            continue
    return rows


def pick_latest_session(index: dict[str, dict[str, Any]]) -> tuple[str, dict[str, Any]] | None:
    best_key = None
    best_row: dict[str, Any] | None = None
    best_updated = -1
    for k, row in index.items():
        if not isinstance(row, dict):
            continue
        updated = row.get("updatedAt")
        if not isinstance(updated, int):
            updated = 0
        if updated >= best_updated:
            best_updated = updated
            best_key = k
            best_row = row
    if not best_key or best_row is None:
        return None
    return best_key, best_row


def ms_to_iso(ms: int | None) -> str:
    if not ms:
        return ""
    try:
        return dt.datetime.fromtimestamp(ms / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def extract_keywords(lines: list[str], pattern: str, limit: int = 8) -> list[str]:
    rx = re.compile(pattern, re.IGNORECASE)
    out: list[str] = []
    for ln in lines:
        if rx.search(ln):
            out.append(trunc(ln, 180))
    return uniq_keep_order(out)[:limit]


def extract_artifacts(lines: list[str], limit: int = 12) -> list[str]:
    url_rx = re.compile(r"https?://[^\s)>\"]+")
    abs_path_rx = re.compile(r"(/Users/[^\s\"'<>]+)")
    rel_path_rx = re.compile(r"\b([\w./-]+\.(?:py|md|json|yml|yaml|sh|ts|tsx|js|jsx|go|rs|java|sql))\b")
    task_id_rx = re.compile(r"\b([A-Z]{2,6}-\d{6,8}(?:-\d+)?)\b")
    items: list[str] = []
    for ln in lines:
        items.extend(url_rx.findall(ln))
        items.extend(abs_path_rx.findall(ln))
        items.extend(rel_path_rx.findall(ln))
        items.extend(task_id_rx.findall(ln))
    return uniq_keep_order(items)[:limit]


def to_short_timeline(events: list[dict[str, str]], limit: int = 16) -> list[str]:
    if not events:
        return []
    rows = events[-limit:]
    out: list[str] = []
    for e in rows:
        role = e.get("role", "-")
        text = trunc(e.get("text", ""), 140)
        out.append(f"{role}: {text}")
    return out


def summarize_capsule(
    agent_id: str,
    session_key: str,
    session_row: dict[str, Any],
    events: list[dict[str, str]],
    task_id: str = "",
) -> dict[str, Any]:
    user_lines = [e["text"] for e in events if e.get("role") == ROLE_USER]
    assistant_lines = [e["text"] for e in events if e.get("role") == ROLE_ASSISTANT]
    all_lines = user_lines + assistant_lines

    objective = trunc(user_lines[0], 280) if user_lines else "未提取到明确目标。"
    latest_request = trunc(user_lines[-1], 280) if user_lines else ""

    decisions = extract_keywords(
        assistant_lines,
        r"(决定|结论|方案|采用|执行|实现|修复|完成|已处理|准奏|封驳|转交|通过|拒绝|回滚)",
        limit=10,
    )
    completed = extract_keywords(
        assistant_lines,
        r"(已完成|完成了|已实现|已修复|已同步|已更新|通过了|成功)",
        limit=10,
    )
    open_questions = extract_keywords(
        all_lines,
        r"(\?|待确认|需要确认|请确认|未确定|不确定|待澄清|TODO|待办)",
        limit=10,
    )
    risks = extract_keywords(
        all_lines,
        r"(风险|阻塞|失败|报错|异常|冲突|回退|兼容|溢出|超时)",
        limit=8,
    )
    next_steps = extract_keywords(
        all_lines,
        r"(下一步|后续|计划|将会|待办|todo|接下来|建议执行)",
        limit=10,
    )
    artifacts = extract_artifacts(all_lines, limit=16)

    total_tokens = (
        session_row.get("totalTokens")
        if isinstance(session_row.get("totalTokens"), int)
        else session_row.get("contextTokens")
    )
    input_tokens = session_row.get("inputTokens")
    output_tokens = session_row.get("outputTokens")
    updated_at_ms = session_row.get("updatedAt")
    session_file = session_row.get("sessionFile")

    return {
        "capsuleVersion": "1.0",
        "capturedAt": now_iso(),
        "taskId": task_id or "",
        "agentId": agent_id,
        "sessionKey": session_key,
        "sessionMeta": {
            "updatedAtMs": updated_at_ms,
            "updatedAt": ms_to_iso(updated_at_ms if isinstance(updated_at_ms, int) else 0),
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "sessionFile": session_file,
            "eventsParsed": len(events),
        },
        "summary": {
            "objective": objective,
            "latestRequest": latest_request,
            "decisions": decisions,
            "completed": completed,
            "openQuestions": open_questions,
            "risks": risks,
            "nextSteps": next_steps,
            "artifacts": artifacts,
        },
        "timeline": to_short_timeline(events, limit=20),
    }


def render_resume_prompt(capsule: dict[str, Any], timeline_lines: int = 12) -> str:
    sm = capsule.get("summary") or {}
    decisions = sm.get("decisions") or []
    completed = sm.get("completed") or []
    open_q = sm.get("openQuestions") or []
    risks = sm.get("risks") or []
    next_steps = sm.get("nextSteps") or []
    artifacts = sm.get("artifacts") or []
    tl = (capsule.get("timeline") or [])[-max(0, timeline_lines):]

    def as_bullets(items: list[str], fallback: str = "- 无") -> str:
        if not items:
            return fallback
        return "\n".join(f"- {trunc(x, 180)}" for x in items)

    return (
        "# 上下文续接包（请先阅读后执行）\n\n"
        f"- agent: `{capsule.get('agentId', '')}`\n"
        f"- task: `{capsule.get('taskId', '') or '未指定'}`\n"
        f"- sessionKey: `{capsule.get('sessionKey', '')}`\n"
        f"- capturedAt: `{capsule.get('capturedAt', '')}`\n\n"
        "## 目标\n"
        f"{trunc(str(sm.get('objective', '')), 400)}\n\n"
        "## 最新诉求\n"
        f"{trunc(str(sm.get('latestRequest', '')), 400)}\n\n"
        "## 已确定决策\n"
        f"{as_bullets(decisions)}\n\n"
        "## 已完成事项\n"
        f"{as_bullets(completed)}\n\n"
        "## 待澄清问题\n"
        f"{as_bullets(open_q)}\n\n"
        "## 风险与阻塞\n"
        f"{as_bullets(risks)}\n\n"
        "## 建议下一步\n"
        f"{as_bullets(next_steps)}\n\n"
        "## 关键工件/路径\n"
        f"{as_bullets(artifacts)}\n\n"
        "## 最近时间线\n"
        f"{as_bullets(tl)}\n\n"
        "## 执行要求\n"
        "- 先复述你理解的当前状态（不超过 6 行）。\n"
        "- 再给出 3-5 条可执行动作，优先最小闭环。\n"
        "- 如果发现与现状冲突，先标出冲突再执行。\n"
        "- 输出必须包含可核验证据（命令/文件路径/变更点）。\n"
    )


def ensure_parent(path: pathlib.Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def default_capsule_path(repo_root: pathlib.Path, agent_id: str, session_key: str, task_id: str) -> pathlib.Path:
    safe_task = re.sub(r"[^a-zA-Z0-9_-]+", "_", (task_id or "").strip()) or "session"
    safe_key = re.sub(r"[^a-zA-Z0-9_-]+", "_", (session_key or "").strip())[:32] or "unknown"
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return repo_root / "data" / "context_capsules" / agent_id / f"{safe_task}-{safe_key}-{ts}.json"


def cmd_capture(args: argparse.Namespace) -> int:
    repo_root = pathlib.Path(args.repo_root).resolve()
    sessions_root = pathlib.Path(args.sessions_root).expanduser().resolve()
    agent_id = args.agent.strip()
    task_id = (args.task_id or "").strip()
    index = load_agent_index(sessions_root, agent_id)
    if not index:
        print(f"[capture] no sessions index for agent={agent_id}, root={sessions_root}")
        return 2

    if args.session_key:
        session_key = args.session_key
        row = index.get(session_key)
        if not isinstance(row, dict):
            print(f"[capture] session key not found: {session_key}")
            return 3
    else:
        picked = pick_latest_session(index)
        if not picked:
            print(f"[capture] no session rows for agent={agent_id}")
            return 4
        session_key, row = picked

    session_file_raw = row.get("sessionFile")
    if not session_file_raw:
        print(f"[capture] session has no sessionFile: {session_key}")
        return 5
    session_file = normalize_session_file(str(session_file_raw))
    events = parse_session_jsonl(session_file, max_events=int(args.max_events))
    capsule = summarize_capsule(
        agent_id=agent_id,
        session_key=session_key,
        session_row=row,
        events=events,
        task_id=task_id,
    )

    out_path = pathlib.Path(args.output).expanduser().resolve() if args.output else default_capsule_path(
        repo_root, agent_id, session_key, task_id
    )
    ensure_parent(out_path)
    out_path.write_text(json.dumps(capsule, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[capture] written: {out_path}")
    print(f"[capture] events={len(events)} totalTokens={row.get('totalTokens')}")
    return 0


def cmd_resume_prompt(args: argparse.Namespace) -> int:
    cap_path = pathlib.Path(args.capsule).expanduser().resolve()
    if not cap_path.exists():
        print(f"[resume-prompt] capsule not found: {cap_path}")
        return 2
    try:
        capsule = json.loads(cap_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[resume-prompt] invalid json: {e}")
        return 3

    prompt = render_resume_prompt(capsule, timeline_lines=int(args.timeline_lines))
    if args.output:
        out = pathlib.Path(args.output).expanduser().resolve()
        ensure_parent(out)
        out.write_text(prompt, encoding="utf-8")
        print(f"[resume-prompt] written: {out}")
        return 0
    print(prompt)
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    repo_root = pathlib.Path(args.repo_root).resolve()
    sessions_root = pathlib.Path(args.sessions_root).expanduser().resolve()
    threshold = int(args.token_threshold)
    generated = 0

    if not sessions_root.exists():
        print(f"[scan] sessions root not found: {sessions_root}")
        return 2

    agent_ids: set[str] = set()
    for agent_dir in sessions_root.iterdir():
        if agent_dir.is_dir():
            agent_ids.add(agent_dir.name)
    global_index = load_sessions_index(sessions_root / "sessions.json")
    for key in global_index.keys():
        # key 示例：agent:gongbu:main
        parts = str(key).split(":")
        if len(parts) >= 3 and parts[0] == "agent":
            agent_ids.add(parts[1])

    for agent_id in sorted(agent_ids):
        index = load_agent_index(sessions_root, agent_id)
        picked = pick_latest_session(index)
        if not picked:
            continue
        session_key, row = picked
        total_tokens = (
            row.get("totalTokens")
            if isinstance(row.get("totalTokens"), int)
            else row.get("contextTokens") if isinstance(row.get("contextTokens"), int) else 0
        )
        if total_tokens < threshold and not args.force:
            continue

        session_file_raw = row.get("sessionFile")
        if not session_file_raw:
            continue
        events = parse_session_jsonl(normalize_session_file(str(session_file_raw)), max_events=int(args.max_events))
        capsule = summarize_capsule(agent_id, session_key, row, events, task_id="")
        out_capsule = default_capsule_path(repo_root, agent_id, session_key, "")
        ensure_parent(out_capsule)
        out_capsule.write_text(json.dumps(capsule, ensure_ascii=False, indent=2), encoding="utf-8")

        out_prompt = out_capsule.with_suffix(".resume.md")
        out_prompt.write_text(
            render_resume_prompt(capsule, timeline_lines=int(args.timeline_lines)),
            encoding="utf-8",
        )
        generated += 1
        print(
            f"[scan] {agent_id} totalTokens={total_tokens} -> "
            f"{out_capsule.name} + {out_prompt.name}"
        )

    print(f"[scan] done, generated={generated}, threshold={threshold}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Context continuity toolkit for OpenClaw sessions.")
    p.add_argument(
        "--repo-root",
        default=str(pathlib.Path(__file__).resolve().parents[1]),
        help="Repository root path. Default: auto-detect from script path.",
    )
    p.add_argument(
        "--sessions-root",
        default=str(pathlib.Path.home() / ".openclaw" / "agents"),
        help="OpenClaw sessions root directory. Default: ~/.openclaw/agents",
    )

    sub = p.add_subparsers(dest="command", required=True)

    c1 = sub.add_parser("capture", help="Capture one agent session into a context capsule.")
    c1.add_argument("--agent", required=True, help="Agent id, e.g. gongbu.")
    c1.add_argument("--session-key", default="", help="Session key. Default: latest session.")
    c1.add_argument("--task-id", default="", help="Optional task id for naming and trace.")
    c1.add_argument("--max-events", type=int, default=4000, help="Max jsonl lines to parse.")
    c1.add_argument("--output", default="", help="Output capsule JSON path.")
    c1.set_defaults(func=cmd_capture)

    c2 = sub.add_parser("resume-prompt", help="Generate resume prompt from a capsule.")
    c2.add_argument("--capsule", required=True, help="Capsule JSON path.")
    c2.add_argument("--timeline-lines", type=int, default=12, help="How many timeline lines to include.")
    c2.add_argument("--output", default="", help="Output markdown path (stdout if omitted).")
    c2.set_defaults(func=cmd_resume_prompt)

    c3 = sub.add_parser("scan", help="Scan all agents and generate capsule for high-token sessions.")
    c3.add_argument("--token-threshold", type=int, default=120000, help="Trigger threshold for total tokens.")
    c3.add_argument("--max-events", type=int, default=4000, help="Max jsonl lines per session.")
    c3.add_argument("--timeline-lines", type=int, default=12, help="Resume prompt timeline lines.")
    c3.add_argument("--force", action="store_true", help="Generate for all latest sessions regardless of token usage.")
    c3.set_defaults(func=cmd_scan)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
