#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${EDICT_SQLITE_DB:-$ROOT_DIR/data/edict.db}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "[INFO] Database not found at: $DB_PATH"
  echo "[INFO] Initializing..."
  python3 "$ROOT_DIR/scripts/setup_sqlite.py" --db "$DB_PATH"
fi

if [[ $# -eq 0 ]]; then
  echo "[INFO] Opening sqlite shell: $DB_PATH"
  exec sqlite3 "$DB_PATH"
fi

if [[ "$1" == "tables" ]]; then
  exec sqlite3 "$DB_PATH" ".tables"
fi

if [[ "$1" == "schema" ]]; then
  exec sqlite3 "$DB_PATH" ".schema"
fi

if [[ "$1" == "q" ]]; then
  shift
  if [[ $# -eq 0 ]]; then
    echo "Usage: scripts/sqlite.sh q \"SELECT ...;\""
    exit 1
  fi
  exec sqlite3 -box -header "$DB_PATH" "$*"
fi

echo "Usage:"
echo "  scripts/sqlite.sh              # interactive shell"
echo "  scripts/sqlite.sh tables       # list tables"
echo "  scripts/sqlite.sh schema       # print schema"
echo "  scripts/sqlite.sh q \"SQL...\"   # run SQL query"
exit 1
