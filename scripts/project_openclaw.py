#!/usr/bin/env python3
"""Edict 项目级 OpenClaw 配置辅助。

目标：
- 旧看板优先读取项目自己的 openclaw 配置，避免被本机全局 ~/.openclaw 直接影响
- 若项目级配置不存在，则按 Edict 默认 11 部门骨架自动初始化
- 仅在必要时回退读取全局配置（例如继承默认模型）
"""

from __future__ import annotations

import json
import pathlib
from typing import Any


BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / "data"
PROJECT_OPENCLAW_CFG = DATA / "openclaw_project.json"
GLOBAL_OPENCLAW_CFG = pathlib.Path.home() / ".openclaw" / "openclaw.json"


DEFAULT_MODEL_FALLBACK = "anthropic/claude-sonnet-4-6"

PROJECT_AGENT_ORDER = [
    "taizi",
    "zhongshu",
    "menxia",
    "shangshu",
    "libu",
    "hubu",
    "bingbu",
    "xingbu",
    "gongbu",
    "libu_hr",
    "zaochao",
]

PROJECT_AGENT_SUBAGENTS: dict[str, list[str]] = {
    "taizi": ["zhongshu"],
    "zhongshu": ["menxia", "shangshu"],
    "menxia": ["shangshu", "zhongshu"],
    "shangshu": ["zhongshu", "menxia", "hubu", "libu", "bingbu", "xingbu", "gongbu", "libu_hr"],
    "libu": ["shangshu"],
    "hubu": ["shangshu"],
    "bingbu": ["shangshu"],
    "xingbu": ["shangshu"],
    "gongbu": ["shangshu"],
    "libu_hr": ["shangshu"],
    "zaochao": [],
}


def read_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: pathlib.Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_model(model_value: Any, fallback: str = DEFAULT_MODEL_FALLBACK) -> str:
    if isinstance(model_value, str) and model_value:
        return model_value
    if isinstance(model_value, dict):
        return model_value.get("primary") or model_value.get("id") or fallback
    return fallback


def _infer_default_model() -> str:
    global_cfg = read_json(GLOBAL_OPENCLAW_CFG, {})
    default_model = normalize_model(
        ((global_cfg.get("agents") or {}).get("defaults") or {}).get("model"),
        "",
    )
    if default_model:
        return default_model

    agent_cfg = read_json(DATA / "agent_config.json", {})
    default_model = normalize_model(agent_cfg.get("defaultModel"), "")
    if default_model:
        return default_model

    return DEFAULT_MODEL_FALLBACK


def _build_project_cfg(default_model: str | None = None) -> dict[str, Any]:
    model_id = default_model or _infer_default_model()
    return {
        "generatedBy": "edict-project-runtime",
        "agents": {
            "defaults": {
                "model": {"primary": model_id},
            },
            "list": [
                {
                    "id": agent_id,
                    "workspace": str(pathlib.Path.home() / f".openclaw/workspace-{agent_id}"),
                    "subagents": {"allowAgents": PROJECT_AGENT_SUBAGENTS[agent_id]},
                }
                for agent_id in PROJECT_AGENT_ORDER
            ],
        },
    }


def ensure_project_openclaw_cfg() -> pathlib.Path:
    """确保项目级配置存在。"""
    if not PROJECT_OPENCLAW_CFG.exists():
        write_json(PROJECT_OPENCLAW_CFG, _build_project_cfg())
    return PROJECT_OPENCLAW_CFG


def load_project_preferred_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    """优先读取项目级配置；若不存在则初始化并返回。"""
    cfg_path = ensure_project_openclaw_cfg()
    return read_json(cfg_path, _build_project_cfg()), cfg_path


def load_global_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    return read_json(GLOBAL_OPENCLAW_CFG, {}), GLOBAL_OPENCLAW_CFG


def project_workspace(agent_id: str) -> pathlib.Path:
    return pathlib.Path.home() / f".openclaw/workspace-{agent_id}"

