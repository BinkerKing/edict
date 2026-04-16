from __future__ import annotations

import datetime
import uuid
from typing import Dict, List, Tuple

from .sqlite_core import connect


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_type(title: str) -> str:
    s = str(title or "").strip()
    if not s:
        return ""
    for token in ("（菜单）", "（模块）", "（按钮）", "(菜单)", "(模块)", "(按钮)"):
        if s.endswith(token):
            return s[: -len(token)].strip()
    return s


def _node_type_by_depth(depth: int) -> str:
    if depth <= 0:
        return "menu"
    if depth == 1:
        return "module"
    return "button"


def _split_detail_sections(text: str) -> Dict[str, str]:
    sections = {
        "basic_info": [],
        "input_preconditions": [],
        "exec_workflow": [],
        "design_pattern": [],
        "agent_collab": [],
        "system_observability": [],
    }
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return {k: "" for k in sections.keys()}
    current = "basic_info"
    for line in raw.split("\n"):
        v = line.strip()
        if v in {"【基本信息】", "基本信息"}:
            current = "basic_info"
            continue
        if v in {"【输入与前置条件】", "输入与前置条件", "【工作流信息】", "工作流信息"}:
            current = "input_preconditions"
            continue
        if v in {"【执行工作流】", "执行工作流"}:
            current = "exec_workflow"
            continue
        if v in {"【设计模式】", "设计模式"}:
            current = "design_pattern"
            continue
        if v in {"【Agent协作】", "Agent协作"}:
            current = "agent_collab"
            continue
        if v in {"【系统处理与观测】", "系统处理与观测", "【系统处理】", "系统处理"}:
            current = "system_observability"
            continue
        sections[current].append(line)
    out = {k: "\n".join(v).strip() for k, v in sections.items()}
    if not any(out.values()):
        out["system_observability"] = raw
    return out


class MeridianSQLiteRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def replace_snapshot(self, meridian: dict) -> dict:
        if not isinstance(meridian, dict):
            return {"ok": False, "error": "meridian must be object"}
        roots = meridian.get("roots")
        if not isinstance(roots, list):
            return {"ok": False, "error": "meridian.roots must be list"}
        details = meridian.get("details") if isinstance(meridian.get("details"), dict) else {}
        feedbacks = meridian.get("feedbacks") if isinstance(meridian.get("feedbacks"), dict) else {}
        logs = meridian.get("logs") if isinstance(meridian.get("logs"), dict) else {}

        conn = connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute("BEGIN")
            cur.execute("DELETE FROM meridian_feedback_replies")
            cur.execute("DELETE FROM meridian_feedback")
            cur.execute("DELETE FROM meridian_logs")
            cur.execute("DELETE FROM meridian_details")
            cur.execute("DELETE FROM meridian_nodes")

            node_by_key: Dict[str, str] = {}
            node_by_title: Dict[str, str] = {}

            def walk(nodes: List[dict], parent_id: str | None, depth: int) -> None:
                for idx, n in enumerate(nodes):
                    if not isinstance(n, dict):
                        continue
                    node_id = str(n.get("key") or f"node-{uuid.uuid4().hex[:10]}")
                    title = str(n.get("title") or "").strip()
                    if not title:
                        continue
                    cur.execute(
                        """
                        INSERT INTO meridian_nodes(id,parent_id,title,node_type,sort_order,deleted,created_at,updated_at)
                        VALUES(?,?,?,?,?,0,?,?)
                        """,
                        (node_id, parent_id, title, _node_type_by_depth(depth), idx, _utc_now(), _utc_now()),
                    )
                    node_by_key[node_id] = node_id
                    node_by_title[title] = node_id
                    base = _strip_type(title)
                    if base and base not in node_by_title:
                        node_by_title[base] = node_id
                    walk(n.get("children") if isinstance(n.get("children"), list) else [], node_id, depth + 1)

            walk(roots, None, 0)

            for title, text in details.items():
                t = str(title or "").strip()
                if not t:
                    continue
                node_id = node_by_title.get(t) or node_by_title.get(_strip_type(t))
                if not node_id:
                    continue
                split = _split_detail_sections(str(text or ""))
                cur.execute(
                    """
                    INSERT OR REPLACE INTO meridian_details(
                      node_id,basic_info,input_preconditions,exec_workflow,design_pattern,agent_collab,system_observability,version,updated_at
                    ) VALUES(?,?,?,?,?,?,?,COALESCE((SELECT version FROM meridian_details WHERE node_id=?),0)+1,?)
                    """,
                    (
                        node_id,
                        split["basic_info"],
                        split["input_preconditions"],
                        split["exec_workflow"],
                        split["design_pattern"],
                        split["agent_collab"],
                        split["system_observability"],
                        node_id,
                        _utc_now(),
                    ),
                )

            for node_key, rows in feedbacks.items():
                node_id = node_by_key.get(str(node_key or "").strip())
                if not node_id or not isinstance(rows, list):
                    continue
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    fid = str(r.get("id") or f"fb-{uuid.uuid4().hex[:10]}")
                    status = str(r.get("status") or "feedback")
                    if status not in {"feedback", "need_clarification", "pending_verify", "accepted"}:
                        status = "feedback"
                    content = str(r.get("text") or "").strip()
                    cur.execute(
                        """
                        INSERT INTO meridian_feedback(id,node_id,status,content,created_by,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (
                            fid,
                            node_id,
                            status,
                            content,
                            "user",
                            str(r.get("createdAt") or _utc_now()),
                            str(r.get("updatedAt") or _utc_now()),
                        ),
                    )
                    replies = r.get("replies") if isinstance(r.get("replies"), list) else []
                    for rp in replies:
                        if not isinstance(rp, dict):
                            continue
                        rid = str(rp.get("id") or f"fbr-{uuid.uuid4().hex[:10]}")
                        cur.execute(
                            """
                            INSERT INTO meridian_feedback_replies(id,feedback_id,reply_by,content,created_at)
                            VALUES(?,?,?,?,?)
                            """,
                            (
                                rid,
                                fid,
                                str(rp.get("by") or "system"),
                                str(rp.get("text") or ""),
                                str(rp.get("at") or _utc_now()),
                            ),
                        )

            for node_key, rows in logs.items():
                node_id = node_by_key.get(str(node_key or "").strip())
                if not isinstance(rows, list):
                    continue
                for lg in rows:
                    if not isinstance(lg, dict):
                        continue
                    lid = str(lg.get("id") or f"log-{uuid.uuid4().hex[:10]}")
                    cur.execute(
                        """
                        INSERT INTO meridian_logs(id,node_id,action,message,created_by,created_at)
                        VALUES(?,?,?,?,?,?)
                        """,
                        (
                            lid,
                            node_id,
                            str(lg.get("action") or "log"),
                            str(lg.get("message") or ""),
                            str(lg.get("by") or "system"),
                            str(lg.get("at") or _utc_now()),
                        ),
                    )

            conn.commit()
            return self.summary(conn=conn)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            return {"ok": False, "error": str(e)}
        finally:
            conn.close()

    def summary(self, conn=None) -> dict:
        own = False
        if conn is None:
            conn = connect(self.db_path)
            own = True
        try:
            cur = conn.cursor()
            counts = {}
            for table in (
                "meridian_nodes",
                "meridian_details",
                "meridian_feedback",
                "meridian_feedback_replies",
                "meridian_logs",
                "task_runs",
            ):
                row = cur.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
                counts[table] = int(row["c"] if row and "c" in row.keys() else 0)
            return {"ok": True, "counts": counts, "dbPath": self.db_path}
        except Exception as e:
            return {"ok": False, "error": str(e), "dbPath": self.db_path}
        finally:
            if own:
                conn.close()

