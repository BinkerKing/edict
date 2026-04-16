#!/usr/bin/env python3
"""
Codex Delegate Tool

让三省六部中的 agent（先是太子）通过本机 codex CLI 做一次非交互式深度思考，
并把最终结果结构化落盘，便于后续看板/flow/审计追踪。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sanitize_task_id(task_id: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in task_id.strip())
    return safe or "unknown"


def sanitize_agent_id(agent_id: str) -> str:
    safe = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in agent_id.strip().lower())
    return safe or "unknown-agent"


def resolve_codex_bin(explicit: str | None) -> str:
    if explicit:
        p = pathlib.Path(explicit).expanduser()
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"codex binary not found: {explicit}")

    env_bin = os.environ.get("CODEX_BIN", "").strip()
    if env_bin:
        p = pathlib.Path(env_bin).expanduser()
        if p.exists():
            return str(p)

    app_bin = pathlib.Path("/Applications/Codex.app/Contents/Resources/codex")
    if app_bin.exists():
        return str(app_bin)

    which = shutil.which("codex")
    if which:
        return which

    raise FileNotFoundError("codex binary not found (checked CODEX_BIN, app bundle, PATH)")


def _history_path(root: pathlib.Path, agent_id: str, task_id: str) -> pathlib.Path:
    return (
        root
        / "data"
        / "codex_delegate_context"
        / sanitize_agent_id(agent_id)
        / f"{sanitize_task_id(task_id)}.json"
    )


def load_history(root: pathlib.Path, agent_id: str, task_id: str) -> list[dict[str, str]]:
    hp = _history_path(root, agent_id, task_id)
    if not hp.exists():
        return []
    try:
        obj = json.loads(hp.read_text(encoding="utf-8"))
    except Exception:
        return []
    turns = obj.get("turns", [])
    return turns if isinstance(turns, list) else []


def save_history(
    root: pathlib.Path,
    agent_id: str,
    task_id: str,
    model: str,
    user_prompt: str,
    final_message: str,
) -> None:
    hp = _history_path(root, agent_id, task_id)
    hp.parent.mkdir(parents=True, exist_ok=True)
    turns = load_history(root, agent_id, task_id)
    turns.append(
        {
            "at": now_iso(),
            "agent_id": agent_id,
            "model": model,
            "input": user_prompt.strip(),
            "output": final_message.strip(),
        }
    )
    # 只保留最近 12 轮，防止上下文无限膨胀
    turns = turns[-12:]
    payload = {
        "task_id": task_id,
        "agent_id": sanitize_agent_id(agent_id),
        "updated_at": now_iso(),
        "turns": turns,
    }
    hp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_prompt(
    user_prompt: str,
    task_id: str,
    agent_id: str,
    history: list[dict[str, str]],
    context_turns: int,
    output_mode: str = "legacy",
) -> str:
    context_lines: list[str] = []
    if history:
        trimmed = history[-max(0, context_turns):]
        for idx, t in enumerate(trimmed, start=1):
            q = str(t.get("input", "")).strip()
            a = str(t.get("output", "")).strip()
            context_lines.append(f"[历史{idx}·输入]\n{q}")
            context_lines.append(f"[历史{idx}·输出]\n{a}")
    context_block = "\n\n".join(context_lines) if context_lines else "无历史上下文。"

    if output_mode == "json":
        return (
            "你是太子的外部智囊（Codex）。\n"
            "请基于以下旨意输出结果，必须使用中文。\n\n"
            f"任务ID: {task_id}\n"
            f"当前调用方: {agent_id}\n\n"
            "如果存在同任务历史上下文，请继承并保持口径一致；"
            "若发现本次输入和历史冲突，要先指出冲突再给出当前建议。\n\n"
            f"历史上下文（同任务+同agent）:\n{context_block}\n\n"
            "输出约束（强制）：\n"
            "1) 只输出一个 JSON 对象，不要 Markdown，不要解释，不要代码块。\n"
            "2) 严格遵循旨意中给出的 JSON schema 字段与枚举。\n"
            "3) 若信息不足，也必须输出符合 schema 的 JSON（例如用澄清动作表示）。\n\n"
            f"旨意原文：\n{user_prompt.strip()}\n"
        )

    return (
        "你是太子的外部智囊（Codex）。\n"
        "请基于以下旨意，输出可执行结论，必须使用中文。\n\n"
        f"任务ID: {task_id}\n"
        f"当前调用方: {agent_id}\n\n"
        "如果存在同任务历史上下文，请继承并保持口径一致；"
        "若发现本次输入和历史冲突，要先指出冲突再给出当前建议。\n\n"
        f"历史上下文（同任务+同agent）:\n{context_block}\n\n"
        "输出格式（严格按此四段）：\n"
        "【任务判断】\n"
        "【标题建议】\n"
        "【执行要点】\n"
        "【风险与回执建议】\n\n"
        "要求：\n"
        "1) 标题建议 10-30 字，中文，不包含路径/URL/代码片段。\n"
        "2) 执行要点给出 3-6 条动作，短句。\n"
        "3) 风险与回执建议要可落地、可核验。\n\n"
        f"旨意原文：\n{user_prompt.strip()}\n"
    )


def run_delegate(
    codex_bin: str,
    prompt: str,
    cwd: str,
    model: str,
    timeout_sec: int,
) -> tuple[int, str, str, str]:
    with tempfile.NamedTemporaryFile(prefix="codex_delegate_", suffix=".txt", delete=False) as f:
        output_last_message = f.name

    cmd = [
        codex_bin,
        "exec",
        "-",
        "-C",
        cwd,
        "-m",
        model,
        "-o",
        output_last_message,
        "--color",
        "never",
        "-c",
        'approval_policy="never"',
        "-c",
        'sandbox_mode="danger-full-access"',
    ]

    started = time.time()
    proc = subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        timeout=timeout_sec,
    )
    _ = started

    final_message = ""
    try:
        final_message = pathlib.Path(output_last_message).read_text(encoding="utf-8").strip()
    except Exception:
        final_message = ""

    return proc.returncode, proc.stdout, proc.stderr, final_message


def save_run_record(
    task_id: str,
    agent_id: str,
    model: str,
    cwd: str,
    prompt: str,
    return_code: int,
    stdout: str,
    stderr: str,
    final_message: str,
) -> pathlib.Path:
    root = pathlib.Path(__file__).resolve().parents[1]
    out_dir = root / "data" / "codex_delegate"
    out_dir.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_file = out_dir / f"{ts}-{sanitize_task_id(task_id)}.json"
    payload: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": sanitize_agent_id(agent_id),
        "model": model,
        "cwd": cwd,
        "created_at": now_iso(),
        "return_code": return_code,
        "prompt": prompt,
        "stdout": stdout,
        "stderr": stderr,
        "final_message": final_message,
    }
    run_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Codex CLI as a delegate brain for Taizi and persist the result."
    )
    parser.add_argument("task_id", help="Task ID, e.g. JJC-20260404-001")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="",
        help="Raw imperial instruction text. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="Codex model ID (default: gpt-5.4)",
    )
    parser.add_argument(
        "--cwd",
        default="/Users/binkerking/Documents/GitHub/edict",
        help="Working directory for codex exec",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=900,
        help="Timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--codex-bin",
        default="",
        help="Optional codex binary path. Fallback order: CODEX_BIN env -> app bundle -> PATH.",
    )
    parser.add_argument(
        "--agent-id",
        default="taizi",
        help="Caller agent id for task-scoped context memory (default: taizi).",
    )
    parser.add_argument(
        "--context-turns",
        type=int,
        default=4,
        help="How many historical turns to include for same task+agent (default: 4).",
    )
    parser.add_argument(
        "--output-mode",
        default="legacy",
        choices=["legacy", "json"],
        help="Output shaping mode for delegate prompt (default: legacy).",
    )
    args = parser.parse_args()

    raw_prompt = args.prompt.strip()
    if not raw_prompt:
        raw_prompt = sys.stdin.read().strip()
    if not raw_prompt:
        print("CODEX_DELEGATE_FAIL")
        print("REASON: empty_prompt")
        return 2

    try:
        codex_bin = resolve_codex_bin(args.codex_bin.strip() or None)
    except Exception as e:
        print("CODEX_DELEGATE_FAIL")
        print(f"REASON: codex_not_found: {e}")
        return 3

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    agent_id = sanitize_agent_id(args.agent_id)
    history = load_history(repo_root, agent_id, args.task_id)
    prompt = build_prompt(
        user_prompt=raw_prompt,
        task_id=args.task_id,
        agent_id=agent_id,
        history=history,
        context_turns=max(0, int(args.context_turns)),
        output_mode=str(args.output_mode or "legacy").strip().lower(),
    )
    try:
        return_code, stdout, stderr, final_message = run_delegate(
            codex_bin=codex_bin,
            prompt=prompt,
            cwd=args.cwd,
            model=args.model,
            timeout_sec=args.timeout,
        )
    except subprocess.TimeoutExpired:
        print("CODEX_DELEGATE_FAIL")
        print(f"REASON: timeout>{args.timeout}s")
        return 4
    except Exception as e:
        print("CODEX_DELEGATE_FAIL")
        print(f"REASON: exec_error: {e}")
        return 5

    run_file = save_run_record(
        task_id=args.task_id,
        agent_id=agent_id,
        model=args.model,
        cwd=args.cwd,
        prompt=prompt,
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        final_message=final_message,
    )

    if return_code != 0:
        print("CODEX_DELEGATE_FAIL")
        print(f"REASON: codex_exit_{return_code}")
        print(f"RUN_FILE: {run_file}")
        tail = stderr.strip()[-800:]
        if tail:
            print("STDERR_TAIL:")
            print(tail)
        return 6

    if not final_message:
        print("CODEX_DELEGATE_FAIL")
        print("REASON: empty_final_message")
        print(f"RUN_FILE: {run_file}")
        return 7

    save_history(
        root=repo_root,
        agent_id=agent_id,
        task_id=args.task_id,
        model=args.model,
        user_prompt=raw_prompt,
        final_message=final_message,
    )

    print("CODEX_DELEGATE_OK")
    print(f"RUN_FILE: {run_file}")
    print("FINAL_MESSAGE_BEGIN")
    print(final_message)
    print("FINAL_MESSAGE_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
