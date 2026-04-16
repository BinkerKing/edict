#!/usr/bin/env python3
import argparse
import json
import pathlib
import time
from openclaw_config import OPENCLAW_AGENTS_HOME


def _scan_for_task(root: pathlib.Path, task_id: str) -> list[str]:
    matches: list[str] = []
    if not root.exists():
        return matches
    for p in root.glob("*.jsonl"):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if task_id in text:
            matches.append(str(p))
    return matches


def _find_codex_delegate_run(repo_root: pathlib.Path, task_id: str) -> tuple[bool, str]:
    """
    校验是否存在该 task 的 codex_delegate 成功记录。
    约束：return_code==0 且 final_message 非空。
    """
    d = repo_root / "data" / "codex_delegate"
    if not d.exists():
        return False, f"missing-dir:{d}"

    candidates = sorted(d.glob(f"*-{task_id}.json"), reverse=True)
    if not candidates:
        return False, f"missing-codex-delegate:{task_id}"

    for p in candidates:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(obj.get("task_id", "")).strip() != task_id:
            continue
        if int(obj.get("return_code", 1)) != 0:
            continue
        if not str(obj.get("final_message", "")).strip():
            continue
        return True, str(p)
    return False, f"invalid-codex-delegate:{task_id}"


def main() -> int:
    parser = argparse.ArgumentParser(description="检查任务是否已转交中书省，并校验 codex_delegate 执行")
    parser.add_argument("task_id", help="任务ID，例如 JJC-20260403-001")
    parser.add_argument("--timeout", type=int, default=15, help="最长等待秒数，默认 15")
    parser.add_argument("--interval-ms", type=int, default=1000, help="轮询间隔毫秒，默认 1000")
    parser.add_argument(
        "--skip-codex-check",
        action="store_true",
        help="跳过 codex_delegate 校验（默认不跳过）",
    )
    args = parser.parse_args()

    task_id = (args.task_id or "").strip()
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    root = OPENCLAW_AGENTS_HOME / "zhongshu" / "sessions"
    if not task_id:
        print("TRANSFER_FAIL")
        print("reason=empty-task-id")
        return 1
    if not root.exists():
        print("TRANSFER_FAIL")
        print(f"reason=missing-dir:{root}")
        return 1

    timeout_sec = max(0, int(args.timeout))
    interval_ms = max(100, int(args.interval_ms))
    deadline = time.time() + timeout_sec

    matches = _scan_for_task(root, task_id)
    while not matches and time.time() < deadline:
        time.sleep(interval_ms / 1000.0)
        matches = _scan_for_task(root, task_id)

    if matches:
        if not args.skip_codex_check:
            ok, evidence = _find_codex_delegate_run(repo_root, task_id)
            if not ok:
                print("TRANSFER_FAIL")
                print(f"reason={evidence}")
                return 1
        print("TRANSFER_OK")
        if not args.skip_codex_check:
            print(f"CODEX_DELEGATE:{evidence}")
        for m in matches[:3]:
            print(f"FOUND:{m}")
        return 0

    print("TRANSFER_FAIL")
    print(f"reason=not-seen-in-zhongshu-session:{task_id}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
