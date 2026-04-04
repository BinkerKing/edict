#!/usr/bin/env python3
import argparse
import pathlib
import time


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


def main() -> int:
    parser = argparse.ArgumentParser(description="检查任务是否已在中书省会话中出现")
    parser.add_argument("task_id", help="任务ID，例如 JJC-20260403-001")
    parser.add_argument("--timeout", type=int, default=15, help="最长等待秒数，默认 15")
    parser.add_argument("--interval-ms", type=int, default=1000, help="轮询间隔毫秒，默认 1000")
    args = parser.parse_args()

    task_id = (args.task_id or "").strip()
    root = pathlib.Path.home() / ".openclaw" / "agents" / "zhongshu" / "sessions"
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
        print("TRANSFER_OK")
        for m in matches[:3]:
            print(f"FOUND:{m}")
        return 0

    print("TRANSFER_FAIL")
    print(f"reason=task-not-found:{task_id}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
