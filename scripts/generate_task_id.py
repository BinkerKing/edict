#!/usr/bin/env python3
"""Generate unique JJC task id.

Format:
  JJC-YYYYMMDD-HHMMSSmmm

Properties:
  - Monotonic across process restarts
  - Not affected by clearing tasks_source.json
"""

import datetime
import os
import pathlib
import time

from file_lock import atomic_json_update


_BASE = pathlib.Path(os.environ["EDICT_HOME"]) if "EDICT_HOME" in os.environ else pathlib.Path(__file__).resolve().parent.parent
_STATE_FILE = _BASE / "data" / "task_id_state.json"


def next_id() -> str:
    now_ms = int(time.time() * 1000)
    holder = {"ms": now_ms}

    def modifier(data):
        if not isinstance(data, dict):
            data = {}
        last_ms = int(data.get("last_ms") or 0)
        new_ms = max(now_ms, last_ms + 1)
        data["last_ms"] = new_ms
        holder["ms"] = new_ms
        return data

    atomic_json_update(_STATE_FILE, modifier, default={})
    ms = holder["ms"]
    dt = datetime.datetime.fromtimestamp(ms / 1000.0)
    return f"JJC-{dt:%Y%m%d}-{dt:%H%M%S}{ms % 1000:03d}"


if __name__ == "__main__":
    print(next_id())
