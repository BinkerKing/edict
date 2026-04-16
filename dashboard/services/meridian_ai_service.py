from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List


RunCodexFn = Callable[..., Dict[str, Any]]
ExtractJsonFn = Callable[[str], Any]


class MeridianAIService:
    def __init__(
        self,
        run_codex_fn: RunCodexFn,
        extract_json_fn: ExtractJsonFn,
        extract_json_lenient_fn: ExtractJsonFn,
        default_code_paths: List[str],
    ):
        self.run_codex_fn = run_codex_fn
        self.extract_json_fn = extract_json_fn
        self.extract_json_lenient_fn = extract_json_lenient_fn
        self.default_code_paths = [str(x).strip() for x in (default_code_paths or []) if str(x).strip()]

    def _normalize_code_paths(self, code_paths) -> List[str]:
        cp = code_paths if isinstance(code_paths, list) else []
        out = []
        for p in cp[:12]:
            s = str(p or "").strip()
            if s:
                out.append(s)
        return out if out else list(self.default_code_paths)

    @staticmethod
    def _task_scope(prefix: str, session_key: str) -> str:
        sk = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(session_key or "").strip()).strip("_")
        if not sk:
            sk = "architecture"
        return f"{prefix}-{sk}", sk

    @staticmethod
    def _safe_json_dumps(obj: Any, max_len: int) -> str:
        try:
            s = json.dumps(obj, ensure_ascii=False)
        except Exception:
            s = "{}"
        return s[:max_len] if len(s) > max_len else s

    def tongmai_decision(
        self,
        node_title: str,
        node_path: str,
        feedback_text: str,
        agent_id: str = "codex",
        code_paths=None,
        tree_snapshot=None,
        node_snapshot=None,
        detail_snapshot: str = "",
        details_snapshot_map=None,
        feedback_thread=None,
        constraints=None,
        session_key: str = "",
        context_turns: int = 10,
    ) -> Dict[str, Any]:
        title = str(node_title or "").strip()
        path = str(node_path or "").strip()
        feedback = str(feedback_text or "").strip()
        aid = str(agent_id or "").strip() or "codex"
        if not feedback:
            return {"ok": False, "error": "feedbackText required"}

        ts = tree_snapshot if isinstance(tree_snapshot, dict) else {}
        ns = node_snapshot if isinstance(node_snapshot, dict) else {}
        ds = str(detail_snapshot or "").strip()[:2600]
        dsm = details_snapshot_map if isinstance(details_snapshot_map, dict) else {}
        ft = feedback_thread if isinstance(feedback_thread, list) else []
        cs = [str(x).strip() for x in ((constraints if isinstance(constraints, list) else [])[:12]) if str(x).strip()]
        cp = self._normalize_code_paths(code_paths)
        task_scope_id, sk = self._task_scope("MERIDIAN-TONGMAI", session_key)

        ts_text = self._safe_json_dumps(ts, 22000)
        dsm_text = self._safe_json_dumps(dsm, 26000)

        prompt = (
            "你是系统剖析器（System Analyzer）。请根据用户反馈，输出结构化 JSON 决策，不要输出其他文本。\n"
            "目标：结合经络树当前结构、节点详情与反馈信息，分析代码逻辑后，给出需要新增或更改的经络树改动详情。\n"
            "你只负责思考与决策，程序负责执行动作与记录。\n\n"
            "重要约束：你必须基于最新代码进行分析，以下菜单/模块/按钮/功能都由这些代码实现。\n"
            "请先以代码实现为准再输出建议，禁止脱离代码臆测。\n"
            f"代码路径(JSON): {json.dumps(cp, ensure_ascii=False)}\n\n"
            f"节点标题: {title or '-'}\n"
            f"节点路径: {path or '-'}\n"
            f"用户反馈: {feedback}\n\n"
            f"经络树全量快照(JSON, 可能截断): {ts_text}\n\n"
            f"节点快照(JSON): {json.dumps(ns, ensure_ascii=False)}\n\n"
            f"详情快照(截断): {ds or '-'}\n\n"
            f"节点详情映射(JSON, 可能截断): {dsm_text}\n\n"
            f"反馈线程(JSON): {json.dumps(ft[:6], ensure_ascii=False)}\n\n"
            f"硬规则(JSON): {json.dumps(cs, ensure_ascii=False)}\n\n"
            "输出 JSON schema:\n"
            "{\n"
            '  "summary": "string",\n'
            '  "status": "pending_action|need_clarification|no_change",\n'
            '  "reply": "string",\n'
            '  "changeReport": {\n'
            '    "tree": "string, 结构树改动信息；无改动写“无”",\n'
            '    "detail": "string, 详情页改动信息；无改动写“无”"\n'
            "  },\n"
            '  "actions": [\n'
            "    {\n"
            '      "type": "add_button|update_detail|replace_detail|ask_clarification|no_change",\n'
            '      "buttonName": "string (when add_button)",\n'
            '      "placement": "child|sibling (when add_button, default child)",\n'
            '      "targetTitle": "string (when update_detail/replace_detail, optional)",\n'
            '      "content": "string (when add_button/update_detail/replace_detail/ask_clarification)"\n'
            "    }\n"
            "  ]\n"
            "}\n\n"
            "规则:\n"
            "1) 若反馈缺少具体按钮名且无法推断，status=need_clarification，并给 ask_clarification。\n"
            "2) 若能明确按钮名，给 add_button 动作，buttonName 必须具体。\n"
            "3) reply 必须与 actions 一致，不能声称已完成却无对应动作。\n"
            "4) 不允许删除节点。\n"
            "5) 优先复用现有节点名称风格，避免名称漂移。\n\n"
            "6) changeReport.tree 与 changeReport.detail 必须始终填写；无改动写“无”。\n\n"
            "7) changeReport 只是说明，不能替代 actions；若 status=pending_action，必须给出至少一条可执行变更 action。\n\n"
            "8) 建议必须可落地到上述代码路径中的实现；若无法从代码确认，请返回 need_clarification。\n\n"
            "9) 必须优先依据“本次反馈文本”决策，不得把历史轮次里的按钮名直接套用到本轮。\n"
            "   若本次反馈未明确改动对象/按钮名/位置，则必须返回 need_clarification，禁止猜测。\n"
        )

        ai = self.run_codex_fn(
            task_id=task_scope_id,
            prompt=prompt,
            agent_id=aid,
            timeout_sec=240,
            context_turns=context_turns,
            output_mode="json",
        )
        if not ai.get("ok"):
            return {"ok": False, "error": str(ai.get("error") or "codex unavailable")[:300]}

        raw = str(ai.get("raw") or "").strip()
        raw_preview = raw[:800]
        if not raw:
            return {
                "ok": True,
                "decision": {
                    "summary": "模型未返回内容，已回退澄清模式",
                    "status": "need_clarification",
                    "reply": "本次通脉未收到有效输出，请重试。",
                    "actions": [{"type": "ask_clarification", "content": "未收到模型输出，请重试一次。"}],
                },
                "source": aid,
                "meta": {"parserStatus": "empty_output", "rawPreview": "", "sessionKey": sk, "taskScopeId": task_scope_id},
            }

        parsed = self.extract_json_fn(raw)
        parser_status = "strict_ok" if isinstance(parsed, dict) else "strict_failed"
        if not isinstance(parsed, dict):
            parsed = self.extract_json_lenient_fn(raw)
            parser_status = "lenient_ok" if isinstance(parsed, dict) else "invalid_json"
        if not isinstance(parsed, dict):
            return {
                "ok": True,
                "decision": {
                    "summary": "模型未返回可解析 JSON，已回退澄清模式",
                    "status": "need_clarification",
                    "reply": "我无法稳定解析这条反馈，请补充更具体信息（例如按钮名称）。",
                    "actions": [{"type": "ask_clarification", "content": "请补充需要新增的具体按钮名称。"}],
                },
                "source": aid,
                "raw": raw[:1200],
                "meta": {"parserStatus": parser_status, "rawPreview": raw_preview, "sessionKey": sk, "taskScopeId": task_scope_id},
            }

        actions = parsed.get("actions")
        actions = actions if isinstance(actions, list) else []
        norm_actions = []
        for it in actions:
            if not isinstance(it, dict):
                continue
            t = str(it.get("type") or "").strip().lower()
            if t not in {"add_button", "update_detail", "replace_detail", "ask_clarification", "no_change"}:
                continue
            row = {"type": t}
            for k in ("buttonName", "placement", "content", "targetTitle"):
                v = str(it.get(k) or "").strip()
                if v:
                    row[k] = v
            norm_actions.append(row)

        status = str(parsed.get("status") or "").strip().lower()
        if status == "resolved":
            status = "pending_action"
        if status not in {"pending_action", "need_clarification", "no_change"}:
            status = "no_change"

        decision = {
            "summary": str(parsed.get("summary") or "").strip() or "通脉决策完成",
            "status": status,
            "reply": str(parsed.get("reply") or "").strip() or "已完成本轮决策。",
            "changeReport": {
                "tree": str(((parsed.get("changeReport") or {}).get("tree") if isinstance(parsed.get("changeReport"), dict) else "") or "").strip() or "无",
                "detail": str(((parsed.get("changeReport") or {}).get("detail") if isinstance(parsed.get("changeReport"), dict) else "") or "").strip() or "无",
            },
            "actions": norm_actions,
        }
        meta = {"parserStatus": parser_status, "sessionKey": sk, "taskScopeId": task_scope_id}
        if parser_status != "strict_ok":
            meta["rawPreview"] = raw_preview
        return {"ok": True, "decision": decision, "source": aid, "meta": meta}

    def openxue_detail(
        self,
        node_title: str,
        node_path: str,
        current_detail: str = "",
        agent_id: str = "codex",
        code_paths=None,
        tree_snapshot=None,
        node_snapshot=None,
        details_snapshot_map=None,
        feedback_thread=None,
        session_key: str = "",
        context_turns: int = 10,
    ) -> Dict[str, Any]:
        title = str(node_title or "").strip()
        path = str(node_path or "").strip()
        detail = str(current_detail or "").strip()
        aid = str(agent_id or "").strip() or "codex"
        cp = self._normalize_code_paths(code_paths)
        ts = tree_snapshot if isinstance(tree_snapshot, dict) else {}
        ns = node_snapshot if isinstance(node_snapshot, dict) else {}
        dsm = details_snapshot_map if isinstance(details_snapshot_map, dict) else {}
        ft = feedback_thread if isinstance(feedback_thread, list) else []
        task_scope_id, sk = self._task_scope("MERIDIAN-OPENXUE", session_key)
        ts_text = self._safe_json_dumps(ts, 22000)
        dsm_text = self._safe_json_dumps(dsm, 26000)

        prompt = (
            "你是系统剖析器（System Analyzer），当前执行“开穴”模式。\n"
            "目标：基于最新代码与当前节点上下文，重写该节点的详情页内容，使其结构化、可执行、可核验。\n"
            "你只输出 JSON，不要输出其他文本。\n\n"
            "约束：\n"
            "1) 详情内容必须围绕当前节点，不要写其他节点。\n"
            "2) 禁止臆造不存在的按钮；若信息不足请返回 need_clarification。\n"
            "3) 输出 detailContent 必须是可直接展示的中文结构化文本（换行分段）。\n"
            "4) detailContent 必须严格包含以下六个分区标题（按顺序）：\n"
            "【基本信息】\n【输入与前置条件】\n【执行工作流】\n【设计模式】\n【Agent协作】\n【系统处理与观测】\n"
            "5) 各分区必须具体可执行，不要空洞描述。\n"
            "6) 【基本信息】必须包含：按钮定位、预期结果、成功判定标准、影响范围。\n"
            "7) 【输入与前置条件】必须包含：触发入口、输入字段、拼接字段（至少含{{node_path}}）、前置校验。\n"
            "8) 【执行工作流】必须包含：流程总览、分阶段流程（至少3阶段，每阶段含动作/判断条件/输出）、失败分支、重试策略。\n"
            "9) 【Agent协作】必须包含：接口方式(endpoint/method/payload示例)、提示词模板(System+User)、响应JSON最小字段约束、反馈解析规则、一个输入输出案例。\n"
            "10) 【系统处理与观测】必须包含：日志记录、异常处理、回滚与兜底、超时阈值、监控指标。\n"
            "11) 若信息不足，明确写“需补充”；不得编造代码中不存在的能力。\n"
            f"代码路径(JSON): {json.dumps(cp, ensure_ascii=False)}\n\n"
            f"节点标题: {title or '-'}\n"
            f"节点路径: {path or '-'}\n"
            f"当前详情: {detail[:2600] or '-'}\n\n"
            f"经络树快照(JSON, 可能截断): {ts_text}\n\n"
            f"节点快照(JSON): {json.dumps(ns, ensure_ascii=False)}\n\n"
            f"节点详情映射(JSON, 可能截断): {dsm_text}\n\n"
            f"反馈线程(JSON): {json.dumps(ft[:6], ensure_ascii=False)}\n\n"
            "输出 JSON schema:\n"
            "{\n"
            '  "summary": "string",\n'
            '  "status": "pending_action|need_clarification|no_change",\n'
            '  "reply": "string",\n'
            '  "detailContent": "string, 状态为pending_action时必须非空",\n'
            '  "notes": ["string"]\n'
            "}\n"
        )

        ai = self.run_codex_fn(
            task_id=task_scope_id,
            prompt=prompt,
            agent_id=aid,
            timeout_sec=240,
            context_turns=context_turns,
            output_mode="json",
        )
        if not ai.get("ok"):
            return {"ok": False, "error": str(ai.get("error") or "codex unavailable")[:300]}

        raw = str(ai.get("raw") or "").strip()
        raw_preview = raw[:800]
        if not raw:
            return {
                "ok": True,
                "decision": {
                    "summary": "模型未返回内容",
                    "status": "need_clarification",
                    "reply": "未收到模型输出，请重试。",
                    "detailContent": "",
                    "notes": ["未收到模型输出"],
                },
                "source": aid,
                "meta": {"parserStatus": "empty_output", "rawPreview": "", "sessionKey": sk, "taskScopeId": task_scope_id},
            }

        parsed = self.extract_json_fn(raw)
        parser_status = "strict_ok" if isinstance(parsed, dict) else "strict_failed"
        if not isinstance(parsed, dict):
            parsed = self.extract_json_lenient_fn(raw)
            parser_status = "lenient_ok" if isinstance(parsed, dict) else "invalid_json"
        if not isinstance(parsed, dict):
            return {
                "ok": True,
                "decision": {
                    "summary": "模型未返回可解析 JSON",
                    "status": "need_clarification",
                    "reply": "返回格式不符合要求，请重试。",
                    "detailContent": "",
                    "notes": ["输出不是可解析 JSON"],
                },
                "source": aid,
                "meta": {"parserStatus": parser_status, "rawPreview": raw_preview, "sessionKey": sk, "taskScopeId": task_scope_id},
            }

        status = str(parsed.get("status") or "").strip().lower()
        if status == "resolved":
            status = "pending_action"
        if status not in {"pending_action", "need_clarification", "no_change"}:
            status = "no_change"
        notes = parsed.get("notes") if isinstance(parsed.get("notes"), list) else []
        norm_notes = [str(x).strip() for x in notes if str(x).strip()][:12]

        decision = {
            "summary": str(parsed.get("summary") or "").strip() or "开穴分析完成",
            "status": status,
            "reply": str(parsed.get("reply") or "").strip() or "已完成本轮开穴分析。",
            "detailContent": str(parsed.get("detailContent") or "").strip(),
            "notes": norm_notes,
        }
        if decision["status"] == "pending_action" and not decision["detailContent"]:
            decision["status"] = "need_clarification"
            decision["notes"].append("status 为 pending_action 但 detailContent 为空，已回退待澄清。")

        meta = {"parserStatus": parser_status, "sessionKey": sk, "taskScopeId": task_scope_id}
        if parser_status != "strict_ok":
            meta["rawPreview"] = raw_preview
        return {"ok": True, "decision": decision, "source": aid, "meta": meta}

