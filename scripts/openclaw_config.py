#!/usr/bin/env python3
"""OpenClaw 配置辅助（优先项目内 .openclaw，兼容回退到 ~/.openclaw）。"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any


BASE = pathlib.Path(__file__).resolve().parent.parent
PROJECT_OPENCLAW_HOME = BASE / ".openclaw"
USER_OPENCLAW_HOME = pathlib.Path.home() / ".openclaw"
LEGACY_AGENTS_SKILLS_HOME = pathlib.Path.home() / ".agents" / "skills"


def _resolve_openclaw_home() -> pathlib.Path:
    """解析运行时目录：
    1) EDICT_OPENCLAW_HOME（显式覆盖）
    2) 项目内 .openclaw（若存在 openclaw.json）
    3) 用户目录 ~/.openclaw（若存在 openclaw.json）
    4) 其余情况下默认项目内 .openclaw
    """
    env_home = (os.environ.get("EDICT_OPENCLAW_HOME") or "").strip()
    if env_home:
        return pathlib.Path(env_home).expanduser()

    project_cfg = PROJECT_OPENCLAW_HOME / "openclaw.json"
    user_cfg = USER_OPENCLAW_HOME / "openclaw.json"
    if project_cfg.exists():
        return PROJECT_OPENCLAW_HOME
    if user_cfg.exists():
        return USER_OPENCLAW_HOME
    if PROJECT_OPENCLAW_HOME.exists():
        return PROJECT_OPENCLAW_HOME
    if USER_OPENCLAW_HOME.exists():
        return USER_OPENCLAW_HOME
    return PROJECT_OPENCLAW_HOME


OPENCLAW_HOME = _resolve_openclaw_home()
OPENCLAW_AGENTS_HOME = OPENCLAW_HOME / "agents"
OPENCLAW_SKILLS_HOME = OPENCLAW_HOME / "skills"
GLOBAL_OPENCLAW_CFG = OPENCLAW_HOME / "openclaw.json"
USER_OPENCLAW_CFG = USER_OPENCLAW_HOME / "openclaw.json"


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
    "rnd",
    "libu_hr",
    "zaochao",
]

AGENT_SUBAGENTS: dict[str, list[str]] = {
    "taizi": ["zhongshu"],
    "zhongshu": ["menxia", "shangshu"],
    "menxia": ["shangshu", "zhongshu"],
    "shangshu": ["zhongshu", "menxia", "hubu", "libu", "bingbu", "xingbu", "rnd", "libu_hr"],
    "libu": ["shangshu"],
    "hubu": ["shangshu"],
    "bingbu": ["shangshu"],
    "xingbu": ["shangshu"],
    "rnd": ["shangshu"],
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
                    "workspace": str(project_workspace(agent_id)),
                    "subagents": {"allowAgents": AGENT_SUBAGENTS[agent_id]},
                }
                for agent_id in AGENT_ORDER
            ],
        },
    }


def ensure_openclaw_cfg() -> pathlib.Path:
    if not GLOBAL_OPENCLAW_CFG.exists():
        # 项目内配置不存在时，优先迁移用户目录已有配置，减少切换成本。
        if OPENCLAW_HOME == PROJECT_OPENCLAW_HOME and USER_OPENCLAW_CFG.exists():
            try:
                GLOBAL_OPENCLAW_CFG.parent.mkdir(parents=True, exist_ok=True)
                GLOBAL_OPENCLAW_CFG.write_text(
                    USER_OPENCLAW_CFG.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                return GLOBAL_OPENCLAW_CFG
            except Exception:
                pass
        write_json(GLOBAL_OPENCLAW_CFG, _build_default_cfg())
    return GLOBAL_OPENCLAW_CFG


def load_openclaw_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    cfg_path = ensure_openclaw_cfg()
    return read_json(cfg_path, _build_default_cfg()), cfg_path


def load_global_cfg() -> tuple[dict[str, Any], pathlib.Path]:
    return read_json(GLOBAL_OPENCLAW_CFG, {}), GLOBAL_OPENCLAW_CFG


def project_workspace(agent_id: str) -> pathlib.Path:
    return OPENCLAW_HOME / f"workspace-{agent_id}"
