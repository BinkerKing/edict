from __future__ import annotations

from typing import Any, Callable, Dict, Tuple


Json = Dict[str, Any]
HandlerResult = Tuple[bool, Json, int]


def handle_post(path: str, body: Any, ops: Dict[str, Callable[..., Json]]) -> HandlerResult:
    """
    Handle meridian API POST routes.
    Returns: (handled, payload, status_code)
    """
    p = str(path or "").strip()
    if p not in {
        "/api/meridian/tongmai-decision",
        "/api/meridian/openxue-detail",
        "/api/meridian/tongmai-run",
        "/api/meridian/openxue-run",
    }:
        return False, {}, 0

    if not isinstance(body, dict):
        return True, {"ok": False, "error": "请求体必须是 JSON 对象"}, 400

    if p == "/api/meridian/tongmai-decision":
        feedback_text = str(body.get("feedbackText") or "").strip()
        if not feedback_text:
            return True, {"ok": False, "error": "feedbackText required"}, 400
        fn = ops["tongmai_decision"]
        payload = fn(
            node_title=str(body.get("nodeTitle") or "").strip(),
            node_path=str(body.get("nodePath") or "").strip(),
            feedback_text=feedback_text,
            agent_id=str(body.get("agentId") or "codex").strip() or "codex",
            code_paths=body.get("codePaths", []),
            tree_snapshot=body.get("treeSnapshot", {}),
            node_snapshot=body.get("nodeSnapshot", {}),
            detail_snapshot=body.get("detailSnapshot", ""),
            details_snapshot_map=body.get("detailsSnapshotMap", {}),
            feedback_thread=body.get("feedbackThread", []),
            constraints=body.get("constraints", []),
            session_key=str(body.get("sessionKey") or "").strip(),
            context_turns=int(body.get("contextTurns") or 10),
        )
        return True, payload, 200

    if p == "/api/meridian/openxue-detail":
        fn = ops["openxue_detail"]
        payload = fn(
            node_title=str(body.get("nodeTitle") or "").strip(),
            node_path=str(body.get("nodePath") or "").strip(),
            current_detail=str(body.get("currentDetail") or "").strip(),
            agent_id=str(body.get("agentId") or "codex").strip() or "codex",
            code_paths=body.get("codePaths", []),
            tree_snapshot=body.get("treeSnapshot", {}),
            node_snapshot=body.get("nodeSnapshot", {}),
            details_snapshot_map=body.get("detailsSnapshotMap", {}),
            feedback_thread=body.get("feedbackThread", []),
            session_key=str(body.get("sessionKey") or "").strip(),
            context_turns=int(body.get("contextTurns") or 10),
        )
        return True, payload, 200

    if p == "/api/meridian/tongmai-run":
        fn = ops["tongmai_run"]
        return True, fn(body), 200

    if p == "/api/meridian/openxue-run":
        fn = ops["openxue_run"]
        return True, fn(body), 200

    return False, {}, 0

