from __future__ import annotations

import pathlib
import sqlite3
from typing import Optional


ROOT = pathlib.Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT / "data" / "edict.db"
DEFAULT_SCHEMA_PATH = ROOT / "db" / "schema.sql"


def resolve_db_path(db_path: Optional[str] = None) -> pathlib.Path:
    if db_path:
        return pathlib.Path(db_path).expanduser().resolve()
    return DEFAULT_DB_PATH


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def ensure_schema(db_path: Optional[str] = None, schema_path: Optional[str] = None) -> dict:
    db = resolve_db_path(db_path)
    schema = pathlib.Path(schema_path).expanduser().resolve() if schema_path else DEFAULT_SCHEMA_PATH
    if not schema.exists():
        return {"ok": False, "error": f"schema not found: {schema}"}
    sql = schema.read_text(encoding="utf-8")
    conn = connect(str(db))
    try:
        conn.executescript(sql)
        conn.commit()
        return {"ok": True, "dbPath": str(db), "schemaPath": str(schema)}
    except Exception as e:
        return {"ok": False, "error": str(e), "dbPath": str(db), "schemaPath": str(schema)}
    finally:
        conn.close()

