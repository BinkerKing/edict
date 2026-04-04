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


def build_prompt(user_prompt: str) -> str:
    return (
        "你是太子的外部智囊（Codex）。\n"
        "请基于以下旨意，输出可执行结论，必须使用中文。\n\n"
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

    prompt = build_prompt(raw_prompt)
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

    print("CODEX_DELEGATE_OK")
    print(f"RUN_FILE: {run_file}")
    print("FINAL_MESSAGE_BEGIN")
    print(final_message)
    print("FINAL_MESSAGE_END")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

