from __future__ import annotations

import datetime
import json
import uuid
from typing import Any, Callable, Dict, List


DecisionFn = Callable[..., Dict[str, Any]]
OpenxueFn = Callable[..., Dict[str, Any]]


class MeridianWorkflowService:
    """
    Orchestrates meridian run workflows.
    Decision engines are injected to keep this service independent from transport layer.
    """

    def __init__(self, tongmai_decision_fn: DecisionFn, openxue_detail_fn: OpenxueFn):
        self.tongmai_decision_fn = tongmai_decision_fn
        self.openxue_detail_fn = openxue_detail_fn

    @staticmethod
    def _strip_type(title: str) -> str:
        s = str(title or "").strip()
        if not s:
            return ""
        for token in ("（菜单）", "（模块）", "（按钮）", "(菜单)", "(模块)", "(按钮)"):
            if s.endswith(token):
                return s[: -len(token)].strip()
        return s

    @staticmethod
    def _type_by_depth(depth: int) -> str:
        if int(depth or 0) <= 0:
            return "菜单"
        if int(depth or 0) == 1:
            return "模块"
        return "按钮"

    def _with_type(self, title: str, depth: int) -> str:
        return f"{self._strip_type(title) or '未命名'}（{self._type_by_depth(depth)}）"

    def _walk_lines(self, roots: List[dict], depth: int = 0, out: List[dict] | None = None) -> List[dict]:
        if out is None:
            out = []
        for n in (roots if isinstance(roots, list) else []):
            if not isinstance(n, dict):
                continue
            out.append(
                {
                    "key": str(n.get("key") or "").strip(),
                    "title": str(n.get("title") or "").strip(),
                    "depth": depth,
                    "node": n,
                }
            )
            self._walk_lines(n.get("children") if isinstance(n.get("children"), list) else [], depth + 1, out)
        return out

    def _find_node(self, roots: List[dict], node_key: str, depth: int = 0, parent_arr=None):
        target = str(node_key or "").strip()
        arr = roots if isinstance(roots, list) else []
        for idx, node in enumerate(arr):
            if not isinstance(node, dict):
                continue
            if str(node.get("key") or "").strip() == target:
                return {"node": node, "depth": depth, "index": idx, "parentArr": parent_arr}
            r = self._find_node(node.get("children") if isinstance(node.get("children"), list) else [], target, depth + 1, node.get("children"))
            if r and isinstance(r.get("node"), dict):
                return r
        return {"node": None, "depth": depth, "index": -1, "parentArr": parent_arr}

    def _path_titles(self, roots: List[dict], node_key: str, path=None):
        p = path if isinstance(path, list) else []
        target = str(node_key or "").strip()
        for n in (roots if isinstance(roots, list) else []):
            if not isinstance(n, dict):
                continue
            title = str(n.get("title") or "").strip()
            key = str(n.get("key") or "").strip()
            nxt = p + ([title] if title else [])
            if key and key == target:
                return nxt
            child = self._path_titles(n.get("children") if isinstance(n.get("children"), list) else [], target, nxt)
            if child:
                return child
        return []

    def _build_tree_snapshot(self, roots: List[dict], max_nodes: int = 240) -> dict:
        cap = max(40, int(max_nodes or 240))
        used = 0

        def walk(nodes, depth=0, path=None):
            nonlocal used
            out = []
            path = path if isinstance(path, list) else []
            for n in (nodes if isinstance(nodes, list) else []):
                if used >= cap:
                    break
                if not isinstance(n, dict):
                    continue
                title = str(n.get("title") or "").strip()
                base = self._strip_type(title)
                used += 1
                out.append(
                    {
                        "key": str(n.get("key") or ""),
                        "title": title,
                        "baseTitle": base,
                        "type": self._type_by_depth(depth),
                        "depth": depth,
                        "path": " / ".join([x for x in (path + [base]) if x]),
                        "children": walk(n.get("children") if isinstance(n.get("children"), list) else [], depth + 1, path + [base]),
                    }
                )
            return out

        return {"tree": walk(roots, 0, []), "truncated": used >= cap, "nodeCount": used}

    def _build_details_snapshot_map(self, details: dict, max_items: int = 260, max_chars: int = 900) -> dict:
        src = details if isinstance(details, dict) else {}
        keys = list(src.keys())[: max(60, int(max_items or 260))]
        out = {str(k): str(src.get(k) or "").strip()[: max(280, int(max_chars or 900))] for k in keys}
        return {"details": out, "truncated": len(src.keys()) > len(keys), "total": len(src.keys())}

    def _build_node_snapshot(self, roots: List[dict], node_key: str) -> dict:
        found = self._find_node(roots, node_key)
        node = found.get("node") if isinstance(found.get("node"), dict) else None
        if not node:
            return {}
        parent_arr = found.get("parentArr") if isinstance(found.get("parentArr"), list) else []
        siblings = [self._strip_type(str(x.get("title") or "")) for x in parent_arr if isinstance(x, dict) and str(x.get("key") or "") != str(node_key)]
        siblings = [x for x in siblings if x][:20]
        children = [self._strip_type(str(x.get("title") or "")) for x in (node.get("children") if isinstance(node.get("children"), list) else []) if isinstance(x, dict)]
        children = [x for x in children if x][:30]
        path = " / ".join([x for x in [self._strip_type(t) for t in self._path_titles(roots, node_key)] if x])
        title = str(node.get("title") or "").strip()
        return {
            "title": self._strip_type(title),
            "typedTitle": title,
            "type": self._type_by_depth(int(found.get("depth") or 0)),
            "depth": int(found.get("depth") or 0),
            "path": path,
            "siblings": siblings,
            "children": children,
        }

    @staticmethod
    def _append_log(meridian: dict, node_key: str, message: str, by: str = "codex") -> None:
        logs = meridian.get("logs")
        if not isinstance(logs, dict):
            logs = {}
            meridian["logs"] = logs
        k = str(node_key or "").strip()
        if not isinstance(logs.get(k), list):
            logs[k] = []
        logs[k].append(
            {
                "id": f"log-{uuid.uuid4().hex[:10]}",
                "message": str(message or "").strip(),
                "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                "by": str(by or "codex") or "codex",
            }
        )

    @staticmethod
    def _set_processor(meridian: dict, node_key: str, by: str, channel: str, action: str, agent: str = "codex") -> None:
        ps = meridian.get("processors")
        if not isinstance(ps, dict):
            ps = {}
            meridian["processors"] = ps
        ps[str(node_key or "").strip()] = {
            "by": by,
            "channel": channel,
            "action": action,
            "agent": str(agent or "codex"),
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        }

    def _resolve_detail_target_title(self, meridian: dict, preferred_title: str, fallback_title: str) -> str:
        details = meridian.get("details")
        details = details if isinstance(details, dict) else {}
        pref = str(preferred_title or "").strip()
        fallback = str(fallback_title or "").strip()
        if pref and pref in details:
            return pref
        base = self._strip_type(pref)
        if base:
            for ln in self._walk_lines(meridian.get("roots") if isinstance(meridian.get("roots"), list) else []):
                t = str(ln.get("title") or "").strip()
                if self._strip_type(t) == base:
                    return t
        return fallback

    def _apply_add_button(self, meridian: dict, node_key: str, action: dict) -> dict:
        roots = meridian.get("roots") if isinstance(meridian.get("roots"), list) else []
        found = self._find_node(roots, node_key)
        node = found.get("node")
        if not isinstance(node, dict):
            return {"ok": False, "note": "目标节点不存在，无法新增按钮"}
        placement = str((action or {}).get("placement") or "child").strip().lower()
        placement = "sibling" if placement == "sibling" else "child"
        base = self._strip_type(str((action or {}).get("buttonName") or "").strip())
        if not base:
            return {"ok": False, "note": "缺少 buttonName，无法新增按钮"}
        if not base.endswith("按钮"):
            base = f"{base}按钮"

        if placement == "sibling":
            target_arr = found.get("parentArr")
            if not isinstance(target_arr, list):
                return {"ok": False, "note": "当前节点无同级容器，无法按同级新增"}
            depth = int(found.get("depth") or 0)
        else:
            if not isinstance(node.get("children"), list):
                node["children"] = []
            target_arr = node.get("children")
            depth = int(found.get("depth") or 0) + 1

        exists = any(self._strip_type(str(x.get("title") or "")) == base for x in target_arr if isinstance(x, dict))
        if exists:
            return {"ok": True, "note": f"结构已存在：{base}"}

        key = f"sm-{uuid.uuid4().hex[:10]}"
        typed = self._with_type(base, depth)
        target_arr.append({"key": key, "title": typed, "children": []})
        details = meridian.get("details")
        if not isinstance(details, dict):
            details = {}
            meridian["details"] = details
        add_content = str((action or {}).get("content") or "").strip()
        if add_content:
            details[typed] = add_content
        elif typed not in details:
            details[typed] = "【基本信息】\n- 节点类型：按钮\n- 按钮定位：需补充\n- 预期结果：需补充"
        return {"ok": True, "note": f"已新增结构节点：{typed}", "addedTitle": typed}

    def _apply_actions(self, meridian: dict, node_key: str, node_title: str, decision: dict) -> dict:
        details = meridian.get("details")
        if not isinstance(details, dict):
            details = {}
            meridian["details"] = details
        actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
        notes, clarify = [], []
        structure_added, executed = 0, 0
        detail_updated = False
        for act in actions:
            if not isinstance(act, dict):
                continue
            t = str(act.get("type") or "").strip().lower()
            if t == "add_button":
                res = self._apply_add_button(meridian, node_key, act)
                notes.append(str(res.get("note") or "").strip() or "新增按钮执行完成")
                if res.get("ok") and "已新增结构节点" in str(res.get("note") or ""):
                    structure_added += 1
                    executed += 1
                continue
            if t in {"update_detail", "replace_detail"}:
                content = str(act.get("content") or "").strip()
                if not content:
                    notes.append(f"{t} 缺少 content，已跳过")
                    continue
                target = self._resolve_detail_target_title(meridian, str(act.get("targetTitle") or ""), node_title)
                if t == "replace_detail":
                    details[target] = content
                    notes.append(f"已覆盖详情内容：{target}")
                else:
                    base = str(details.get(target) or "").strip()
                    block = f"\n\n通脉更新（{datetime.datetime.now():%Y-%m-%d %H:%M:%S}）\n{content}"
                    details[target] = f"{base}{block}" if base else f"通脉更新（{datetime.datetime.now():%Y-%m-%d %H:%M:%S}）\n{content}"
                    notes.append(f"已追加详情更新：{target}")
                detail_updated = True
                executed += 1
                continue
            if t == "ask_clarification":
                c = str(act.get("content") or "").strip()
                if c:
                    clarify.append(c)
                notes.append("已生成澄清问题" if c else "需要补充信息")
                continue
            if t == "no_change":
                notes.append("本轮无结构或详情变更")
        return {
            "notes": notes,
            "clarifyNotes": clarify,
            "detailUpdated": detail_updated,
            "structureAdded": structure_added,
            "executedCount": executed,
        }

    def tongmai_run(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be object"}
        meridian = payload.get("meridian")
        if not isinstance(meridian, dict):
            return {"ok": False, "error": "meridian required"}
        meridian = json.loads(json.dumps(meridian, ensure_ascii=False))
        roots = meridian.get("roots")
        if not isinstance(roots, list) or not roots:
            return {"ok": False, "error": "meridian.roots required"}

        feedbacks = meridian.get("feedbacks")
        if not isinstance(feedbacks, dict):
            feedbacks = {}
            meridian["feedbacks"] = feedbacks
        details = meridian.get("details")
        if not isinstance(details, dict):
            details = {}
            meridian["details"] = details

        agent_id = str(payload.get("agentId") or "codex").strip() or "codex"
        session_key = str(payload.get("sessionKey") or "skilllab_architecture").strip() or "skilllab_architecture"
        context_turns = int(payload.get("contextTurns") or 10)
        code_paths = payload.get("codePaths") if isinstance(payload.get("codePaths"), list) else []
        node_keys = payload.get("nodeKeys")
        if isinstance(node_keys, list) and node_keys:
            key_set = {str(k).strip() for k in node_keys if str(k).strip()}
            lines = [x for x in self._walk_lines(roots) if str(x.get("key") or "").strip() in key_set]
        else:
            lines = self._walk_lines(roots)

        changed_count = 0
        structure_changed_count = 0
        clarify_count = 0
        processed_nodes = 0

        tree_snapshot = self._build_tree_snapshot(roots, 240)
        details_snapshot_map = self._build_details_snapshot_map(details, 260, 900)

        for ln in lines:
            node_key = str(ln.get("key") or "").strip()
            node_title = str(ln.get("title") or "").strip()
            if not node_key or not node_title:
                continue
            rows = feedbacks.get(node_key) if isinstance(feedbacks.get(node_key), list) else []
            pending = [r for r in rows if isinstance(r, dict) and str(r.get("status") or "") in {"feedback", "need_clarification"} and str(r.get("text") or "").strip()]
            if not pending:
                continue
            processed_nodes += 1
            node_path = " / ".join([x for x in [self._strip_type(t) for t in self._path_titles(roots, node_key)] if x])
            node_snapshot = self._build_node_snapshot(roots, node_key)
            now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")

            resolved_count = 0
            ambiguous_count = 0
            for r in pending:
                fb_text = str(r.get("text") or "").strip()
                ai = self.tongmai_decision_fn(
                    node_title=node_title,
                    node_path=node_path,
                    feedback_text=fb_text,
                    agent_id=agent_id,
                    code_paths=code_paths,
                    tree_snapshot=tree_snapshot,
                    node_snapshot=node_snapshot,
                    detail_snapshot=str(details.get(node_title) or "").strip()[:2600],
                    details_snapshot_map=details_snapshot_map,
                    feedback_thread=rows[-6:],
                    constraints=[
                        "只处理 status=feedback/need_clarification 的反馈项",
                        "禁止删除任何节点",
                        "仅允许新增节点或更新详情",
                        "若按钮名称不明确必须返回 need_clarification",
                        "可执行改动必须返回 pending_action",
                    ],
                    session_key=session_key,
                    context_turns=context_turns,
                )
                replies = r.get("replies") if isinstance(r.get("replies"), list) else []
                r["replies"] = replies
                if not ai.get("ok"):
                    replies.append({"id": f"fbr-{uuid.uuid4().hex[:10]}", "by": "codex", "at": now_iso, "text": f"通脉调用失败：{str(ai.get('error') or '未知错误')}"})
                    r["updatedAt"] = now_iso
                    ambiguous_count += 1
                    continue

                decision = ai.get("decision") if isinstance(ai.get("decision"), dict) else {}
                meta = ai.get("meta") if isinstance(ai.get("meta"), dict) else {}
                parser_status = str(meta.get("parserStatus") or "").strip()
                raw_preview = str(meta.get("rawPreview") or "").strip()
                report = decision.get("changeReport") if isinstance(decision.get("changeReport"), dict) else {}
                tree_report = str(report.get("tree") or "").strip() or "无"
                detail_report = str(report.get("detail") or "").strip() or "无"
                apply_res = self._apply_actions(meridian, node_key, node_title, decision)
                structure_changed_count += int(apply_res.get("structureAdded") or 0)
                status = str(decision.get("status") or "").strip().lower()
                summary = str(decision.get("summary") or "通脉决策完成").strip()
                reply = str(decision.get("reply") or "").strip()
                action_text = "；".join([x for x in (apply_res.get("notes") or []) if str(x).strip()])
                clarify = "；".join([x for x in (apply_res.get("clarifyNotes") or []) if str(x).strip()])
                report_claims_change = tree_report != "无" or detail_report != "无"

                txt_parts = [
                    f"通脉决策：{summary}",
                    f"结构树改动：{tree_report}",
                    f"详情页改动：{detail_report}",
                    f"解析状态：{parser_status}" if parser_status and parser_status != "strict_ok" else "",
                    f"原始返回片段：\n{raw_preview}" if parser_status and parser_status != "strict_ok" and raw_preview else "",
                    f"回复：{reply}" if reply else "",
                    f"执行：{action_text}" if action_text else "",
                    f"需补充：{clarify}" if clarify else "",
                ]
                replies.append({"id": f"fbr-{uuid.uuid4().hex[:10]}", "by": "codex", "at": now_iso, "text": "\n".join([x for x in txt_parts if x])})

                if status in {"pending_action", "resolved"}:
                    if int(apply_res.get("executedCount") or 0) > 0:
                        r["status"] = "pending_verify"
                        resolved_count += 1
                    else:
                        r["status"] = "feedback"
                        ambiguous_count += 1
                        replies.append({"id": f"fbr-{uuid.uuid4().hex[:10]}", "by": "system", "at": now_iso, "text": "系统校验：本次返回虽为 pending_action，但未提供可执行动作，已退回反馈中，请补充 actions。"})
                    if report_claims_change and int(apply_res.get("executedCount") or 0) == 0:
                        replies.append({"id": f"fbr-{uuid.uuid4().hex[:10]}", "by": "system", "at": now_iso, "text": "系统校验：changeReport 声称有改动，但 actions 未落地任何变更，已退回反馈中。"})
                elif status == "need_clarification":
                    r["status"] = "need_clarification"
                    ambiguous_count += 1
                else:
                    r["status"] = "feedback"
                r["updatedAt"] = now_iso

            self._set_processor(meridian, node_key, "Codex", "Codex通道决策 + 后端动作执行器", "通脉", agent=agent_id)
            parts = []
            if resolved_count > 0:
                parts.append(f"已调整 {resolved_count} 条并置为待验证")
            if ambiguous_count > 0:
                parts.append(f"待澄清 {ambiguous_count} 条")
            if resolved_count > 0:
                changed_count += 1
            clarify_count += ambiguous_count
            self._append_log(meridian, node_key, f"通脉处理：{'；'.join(parts) if parts else '未发生变更'}", "codex")

        return {
            "ok": True,
            "meridian": meridian,
            "stats": {
                "processedNodes": processed_nodes,
                "changedNodes": changed_count,
                "structureAdded": structure_changed_count,
                "clarifyCount": clarify_count,
            },
        }

    def openxue_run(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"ok": False, "error": "payload must be object"}
        meridian = payload.get("meridian")
        if not isinstance(meridian, dict):
            return {"ok": False, "error": "meridian required"}
        node_key = str(payload.get("nodeKey") or "").strip()
        if not node_key:
            return {"ok": False, "error": "nodeKey required"}
        meridian = json.loads(json.dumps(meridian, ensure_ascii=False))
        roots = meridian.get("roots") if isinstance(meridian.get("roots"), list) else []
        found = self._find_node(roots, node_key)
        node = found.get("node")
        if not isinstance(node, dict):
            return {"ok": False, "error": "node not found"}
        node_title = str(node.get("title") or "").strip()
        if not node_title:
            return {"ok": False, "error": "node title invalid"}
        node_path = " / ".join([x for x in [self._strip_type(t) for t in self._path_titles(roots, node_key)] if x])
        details = meridian.get("details")
        if not isinstance(details, dict):
            details = {}
            meridian["details"] = details
        feedbacks = meridian.get("feedbacks")
        if not isinstance(feedbacks, dict):
            feedbacks = {}
            meridian["feedbacks"] = feedbacks
        feedback_rows = feedbacks.get(node_key) if isinstance(feedbacks.get(node_key), list) else []
        agent_id = str(payload.get("agentId") or "codex").strip() or "codex"
        session_key = str(payload.get("sessionKey") or "skilllab_architecture").strip() or "skilllab_architecture"
        context_turns = int(payload.get("contextTurns") or 10)
        code_paths = payload.get("codePaths") if isinstance(payload.get("codePaths"), list) else []

        ai = self.openxue_detail_fn(
            node_title=node_title,
            node_path=node_path,
            current_detail=str(details.get(node_title) or ""),
            agent_id=agent_id,
            code_paths=code_paths,
            tree_snapshot=self._build_tree_snapshot(roots, 240),
            node_snapshot=self._build_node_snapshot(roots, node_key),
            details_snapshot_map=self._build_details_snapshot_map(details, 260, 900),
            feedback_thread=feedback_rows[-6:],
            session_key=session_key,
            context_turns=context_turns,
        )
        if not ai.get("ok"):
            return {"ok": False, "error": str(ai.get("error") or "openxue failed")[:300]}
        decision = ai.get("decision") if isinstance(ai.get("decision"), dict) else {}
        status = str(decision.get("status") or "").strip().lower()
        summary = str(decision.get("summary") or "开穴分析完成").strip()
        reply = str(decision.get("reply") or "").strip()
        detail_content = str(decision.get("detailContent") or "").strip()
        notes = decision.get("notes")
        notes_text = "；".join([str(x).strip() for x in (notes if isinstance(notes, list) else []) if str(x).strip()])
        parser_status = str((ai.get("meta") or {}).get("parserStatus") or "").strip()
        raw_preview = str((ai.get("meta") or {}).get("rawPreview") or "").strip()

        updated = False
        if status == "pending_action" and detail_content:
            details[node_title] = detail_content
            updated = True
        self._set_processor(meridian, node_key, "Codex", "Codex通道决策 + 后端更新详情", "开穴", agent=agent_id)
        parts = [f"开穴：{summary}", f"状态：{status or 'no_change'}"]
        if reply:
            parts.append(f"回复：{reply}")
        if notes_text:
            parts.append(f"说明：{notes_text}")
        if parser_status and parser_status != "strict_ok":
            parts.append(f"解析：{parser_status}")
        if raw_preview and parser_status and parser_status != "strict_ok":
            parts.append(f"原始片段：{raw_preview[:180].replace(chr(10), ' ')}")
        self._append_log(meridian, node_key, "；".join(parts), "codex")

        return {
            "ok": True,
            "meridian": meridian,
            "result": {
                "status": status or "no_change",
                "summary": summary,
                "reply": reply,
                "updated": updated,
                "parserStatus": parser_status,
            },
        }

