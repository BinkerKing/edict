from __future__ import annotations

from typing import Any, Callable, Dict, Tuple


Json = Dict[str, Any]
HandlerResult = Tuple[bool, Json, int]


def handle_post(path: str, body: Any, ops: Dict[str, Callable[..., Json]]) -> HandlerResult:
    """
    Handle secretary API POST routes.
    Returns: (handled, payload, status_code)
    """
    p = str(path or "").strip()
    if p not in {"/api/secretary/memory-save", "/api/secretary/task-rate"}:
        return False, {}, 0

    if not isinstance(body, dict):
        return True, {"ok": False, "error": "请求体必须是 JSON 对象"}, 400

    if p == "/api/secretary/memory-save":
        fn = ops["memory_save"]
        return True, fn(
            system_content=body.get("systemContent", None),
            user_pref_content=body.get("userPreferenceContent", None),
        ), 200

    if p == "/api/secretary/task-rate":
        task_id = str(body.get("taskId") or "").strip()
        if not task_id:
            return True, {"ok": False, "error": "taskId required"}, 400
        fn = ops["task_rate"]
        return True, fn(task_id, body.get("rating"), body.get("comment", "")), 200

    return False, {}, 0

