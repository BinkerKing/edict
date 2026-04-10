#!/usr/bin/env python3
"""OpenClaw 配置辅助（统一使用全局 ~/.openclaw/openclaw.json）。"""

from __future__ import annotations

import json
import pathlib
from typing import Any


BASE = pathlib.Path(__file__).resolve().parent.parent
GLOBAL_OPENCLAW_CFG = pathlib.Path.home() / ".openclaw" / "openclaw.json"


DEFAULT_MODEL_FALLBACK = "anthropic/claude-sonnet-4-6"

AGENT_ORDER = [
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

AGENT_SUBAGENTS: dict[str, list[str]] = {
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


def _build_default_cfg(default_model: str = DEFAULT_MODEL_FALLBACK) -> dict[str, Any]:
    return {
        "generatedBy": "edict-global-runtime",
        "agents": {
            "defaults": {
                "model": {"primary": default_model},
            },
            "list": [
                {
                    "id": agent_id,
                    "workspace": str(pathlib.Path.home() / f".openclaw/workspace-{agent_id}"),
                    "subagents": {"allowAgents": AGENT_SUBAGENTS[agent_id]},
                }
                for agent_id in AGENT_ORDER
            ],
        },
    }


def ensure_openclaw_cfg() -> pathlib.Path:
    if not GLOBAL_OPENCLAW_CFG.exists():
        write_json(GLOBAL_OPENCLAW_CFG, _build_default_cfg())
    return GLOBAL_OPENCLAW_CFG


def load_openclaw_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    cfg_path = ensure_openclaw_cfg()
    return read_json(cfg_path, _build_default_cfg()), cfg_path


def load_global_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    return read_json(GLOBAL_OPENCLAW_CFG, {}), GLOBAL_OPENCLAW_CFG


def project_workspace(agent_id: str) -> pathlib.Path:
    return pathlib.Path.home() / f".openclaw/workspace-{agent_id}"

