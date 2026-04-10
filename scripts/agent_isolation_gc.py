#!/usr/bin/env python3
"""
Agent Isolation GC

统一治理隔离 agent 注册表（list / gc）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess


BASE = pathlib.Path(__file__).resolve().parent.parent
DATA = BASE / "data"
REGISTRY_FILE = DATA / "agent_isolation_registry.json"


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(ts: str | None) -> dt.datetime | None:
    if not ts or not isinstance(ts, str):
        return None
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {"version": 1, "scopes": {}}
    try:
        obj = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "scopes": {}}
    if not isinstance(obj, dict):
        return {"version": 1, "scopes": {}}
    scopes = obj.get("scopes")
    if not isinstance(scopes, dict):
        scopes = {}
    obj["version"] = int(obj.get("version") or 1)
    obj["scopes"] = scopes
    return obj


def save_registry(obj: dict) -> None:
    obj = obj if isinstance(obj, dict) else {"version": 1, "scopes": {}}
    if not isinstance(obj.get("scopes"), dict):
        obj["scopes"] = {}
    obj["updatedAt"] = now_utc().isoformat().replace("+00:00", "Z")
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def list_rows(registry: dict) -> list[dict]:
    rows = []
    for key, rec in (registry.get("scopes") or {}).items():
        if not isinstance(rec, dict):
            continue
        rows.append(
            {
                "scope": key,
                "runtimeAgentId": str(rec.get("runtimeAgentId") or ""),
                "baseAgentId": str(rec.get("baseAgentId") or ""),
                "projectId": str(rec.get("projectId") or ""),
                "action": str(rec.get("action") or ""),
                "lastUsedAt": str(rec.get("lastUsedAt") or ""),
            }
        )
    rows.sort(key=lambda x: x.get("lastUsedAt", ""), reverse=True)
    return rows


def delete_agent(agent_id: str, dry_run: bool) -> tuple[bool, str]:
    cmd = ["openclaw", "agents", "delete", agent_id, "--force", "--json"]
    if dry_run:
        return True, "dry-run"
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except Exception as e:
        return False, f"exec error: {e}"
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "unknown error").strip()
        return False, err[:220]
    return True, "deleted"


def cmd_list(_: argparse.Namespace) -> int:
    reg = load_registry()
    rows = list_rows(reg)
    print(f"registry={REGISTRY_FILE}")
    print(f"count={len(rows)}")
    for r in rows:
        print(
            f"- {r['runtimeAgentId']} | project={r['projectId']} | action={r['action']} | "
            f"lastUsedAt={r['lastUsedAt']} | scope={r['scope']}"
        )
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    reg = load_registry()
    scopes = dict(reg.get("scopes") or {})
    if not scopes:
        print("no scopes found")
        return 0

    cutoff = now_utc() - dt.timedelta(days=max(1, int(args.max_idle_days)))
    removed = 0
    failed = 0

    for key, rec in list(scopes.items()):
        if not isinstance(rec, dict):
            scopes.pop(key, None)
            continue
        runtime_agent_id = str(rec.get("runtimeAgentId") or "").strip()
        if not runtime_agent_id:
            scopes.pop(key, None)
            removed += 1
            continue
        last_used = parse_iso(str(rec.get("lastUsedAt") or rec.get("createdAt") or ""))
        if last_used and last_used > cutoff:
            continue
        ok, note = delete_agent(runtime_agent_id, args.dry_run)
        if ok:
            scopes.pop(key, None)
            removed += 1
            print(f"[GC] removed {runtime_agent_id} ({note})")
        else:
            failed += 1
            print(f"[GC] failed {runtime_agent_id}: {note}")

    reg["scopes"] = scopes
    save_registry(reg)
    print(f"[GC] done removed={removed} failed={failed} dry_run={bool(args.dry_run)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Agent isolation registry management.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("list", help="List isolation scopes.")
    s1.set_defaults(func=cmd_list)

    s2 = sub.add_parser("gc", help="Delete stale isolated agents and clean registry.")
    s2.add_argument("--max-idle-days", type=int, default=7, help="Idle days threshold.")
    s2.add_argument("--dry-run", action="store_true", help="Print actions without deleting agents.")
    s2.set_defaults(func=cmd_gc)
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
