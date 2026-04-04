#!/usr/bin/env python3
import argparse
import pathlib
import subprocess
import sys


BASE = pathlib.Path(__file__).resolve().parent.parent


def run_cmd(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True)
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    msg = out if out else err
    return p.returncode, msg


def main() -> int:
    parser = argparse.ArgumentParser(description="中书省一键转交门下省（状态+流转+派发+校验）")
    parser.add_argument("task_id", help="任务ID，例如 JJC-20260403-001")
    parser.add_argument("title", help="任务标题或简述")
    parser.add_argument("--remark", default="📋 方案提交审议", help="flow 备注")
    parser.add_argument("--timeout", type=int, default=300, help="openclaw agent 超时秒数")
    args = parser.parse_args()

    task_id = args.task_id.strip()
    title = args.title.strip()
    remark = args.remark.strip() or "📋 方案提交审议"
    if not task_id:
        print("HANDOFF_FAIL")
        print("reason=empty-task-id")
        return 1

    # 1) 状态推进到门下
    rc, msg = run_cmd([
        "python3", "scripts/kanban_update.py", "state",
        task_id, "Menxia", "方案提交门下省审议",
    ])
    if rc != 0:
        print("HANDOFF_FAIL")
        print(f"reason=state-failed:{msg[:300]}")
        return 1

    # 2) 写 flow
    rc, msg = run_cmd([
        "python3", "scripts/kanban_update.py", "flow",
        task_id, "中书省", "门下省", remark,
    ])
    if rc != 0:
        print("HANDOFF_FAIL")
        print(f"reason=flow-failed:{msg[:300]}")
        return 1

    # 3) 派发门下省
    dispatch_msg = (
        f"📋 中书省提交审议\n"
        f"任务ID: {task_id}\n"
        f"任务: {title}\n"
        f"请按门下省职责给出准奏或封驳，并更新看板状态。"
    )
    rc, msg = run_cmd([
        "openclaw", "agent", "--agent", "menxia",
        "-m", dispatch_msg, "--timeout", str(max(60, int(args.timeout))),
    ])
    if rc != 0:
        print("HANDOFF_FAIL")
        print(f"reason=dispatch-failed:{msg[:300]}")
        return 1

    # 4) 校验门下会话是否接单
    check = subprocess.run(
        ["python3", "scripts/check_transfer_to_menxia.py", task_id, "--timeout", "20", "--interval-ms", "1000"],
        cwd=str(BASE),
        capture_output=True,
        text=True,
    )
    output = (check.stdout or "").strip()
    print(output)
    if check.returncode != 0:
        print("HANDOFF_FAIL")
        print("reason=verify-failed")
        return 1

    print("HANDOFF_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
