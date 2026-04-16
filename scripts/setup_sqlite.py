#!/usr/bin/env python3
"""
Initialize SQLite database for EDICT project.

Usage:
  python3 scripts/setup_sqlite.py
  python3 scripts/setup_sqlite.py --db /abs/path/to/edict.db
  python3 scripts/setup_sqlite.py --schema /abs/path/to/schema.sql
"""
from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys


ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "edict.db"
DEFAULT_SCHEMA = ROOT / "db" / "schema.sql"


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Setup SQLite for EDICT")
  p.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB, help="SQLite DB file path")
  p.add_argument("--schema", type=pathlib.Path, default=DEFAULT_SCHEMA, help="Schema SQL file path")
  return p.parse_args()


def main() -> int:
  args = parse_args()
  db_path = pathlib.Path(args.db).expanduser().resolve()
  schema_path = pathlib.Path(args.schema).expanduser().resolve()

  if not schema_path.exists():
    print(f"[ERR] schema not found: {schema_path}", file=sys.stderr)
    return 1

  db_path.parent.mkdir(parents=True, exist_ok=True)
  schema_sql = schema_path.read_text(encoding="utf-8")

  conn = sqlite3.connect(str(db_path))
  try:
    conn.executescript(schema_sql)
    conn.commit()

    # Validate key pragmas (best effort).
    jm = conn.execute("PRAGMA journal_mode").fetchone()
    fk = conn.execute("PRAGMA foreign_keys").fetchone()
    bt = conn.execute("PRAGMA busy_timeout").fetchone()
    tables = conn.execute(
      "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
  finally:
    conn.close()

  print(f"[OK] sqlite initialized: {db_path}")
  print(f"[OK] schema: {schema_path}")
  print(f"[OK] journal_mode={jm[0] if jm else 'unknown'}, foreign_keys={fk[0] if fk else 'unknown'}, busy_timeout={bt[0] if bt else 'unknown'}")
  print("[OK] tables:")
  for t in tables:
    print(f"  - {t[0]}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
