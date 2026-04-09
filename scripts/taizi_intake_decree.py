#!/usr/bin/env python3
"""
太子正式旨意入口（半确定性）：
- 解析“下旨”原文
- 生成任务ID
- 创建任务并推进到 Zhongshu
- 写入太子 -> 中书省 flow
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path


BASE = Path(__file__).resolve().parents[1]


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        cwd=str(BASE),
        capture_output=True,
        text=True,
    )
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def _extract_decree_text(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"^(下旨|传旨|旨意)\s*[:：]\s*", "", text)
    text = text.strip("。 \n\t")
    return text or "未提供旨意正文"


def _build_title(decree: str) -> str:
    cleaned = re.sub(r"\s+", " ", decree).strip()
    cleaned = re.sub(r"[`\"'<>/\\|]", "", cleaned)
    if len(cleaned) <= 30:
        return cleaned
    return cleaned[:30]


def main() -> int:
    ap = argparse.ArgumentParser(description="Intake imperial decree into JJC task")
    ap.add_argument("raw_text", help="raw user message")
    args = ap.parse_args()

    decree = _extract_decree_text(args.raw_text)
    title = _build_title(decree)

    rc, out, err = _run(["python3", "scripts/generate_task_id.py"])
    if rc != 0 or not out:
        print("INTAKE_FAIL")
        print(f"step=generate_task_id rc={rc} err={err or out}")
        return 1
    task_id = out.splitlines()[-1].strip()

    rc, out, err = _run(
        [
            "python3",
            "scripts/kanban_update.py",
            "create",
            task_id,
            title,
            "Zhongshu",
            "中书省",
            "中书令",
            "太子整理旨意",
        ]
    )
    if rc != 0:
        print("INTAKE_FAIL")
        print(f"step=create rc={rc} err={err or out}")
        return 1

    rc, out, err = _run(
        [
            "python3",
            "scripts/kanban_update.py",
            "state",
            task_id,
            "Zhongshu",
            "太子分拣完成，移交中书省起草",
        ]
    )
    if rc != 0:
        print("INTAKE_FAIL")
        print(f"step=state rc={rc} err={err or out}")
        return 1

    remark = f"📋 旨意传达：{decree}"
    rc, out, err = _run(
        [
            "python3",
            "scripts/kanban_update.py",
            "flow",
            task_id,
            "太子",
            "中书省",
            remark,
        ]
    )
    if rc != 0:
        print("INTAKE_FAIL")
        print(f"step=flow rc={rc} err={err or out}")
        return 1

    print("INTAKE_OK")
    print(f"TASK_ID={task_id}")
    print(f"TITLE={title}")
    print(f"DECREE={decree}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
