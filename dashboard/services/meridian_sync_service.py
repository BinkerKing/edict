from __future__ import annotations

from typing import Optional

from storage.sqlite_core import ensure_schema, resolve_db_path
from storage.meridian_repo import MeridianSQLiteRepository


class MeridianSQLiteSyncService:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(resolve_db_path(db_path))
        self.repo = MeridianSQLiteRepository(self.db_path)

    def ensure_ready(self) -> dict:
        return ensure_schema(self.db_path)

    def health(self) -> dict:
        init = self.ensure_ready()
        if not init.get("ok"):
            return {"ok": False, "error": init.get("error"), "dbPath": self.db_path}
        summary = self.repo.summary()
        if not summary.get("ok"):
            return summary
        return {"ok": True, "dbPath": self.db_path, "summary": summary.get("counts", {})}

    def sync_meridian_snapshot(self, meridian: dict) -> dict:
        init = self.ensure_ready()
        if not init.get("ok"):
            return {"ok": False, "error": init.get("error"), "dbPath": self.db_path}
        res = self.repo.replace_snapshot(meridian)
        if not res.get("ok"):
            return res
        return {"ok": True, "dbPath": self.db_path, "summary": res.get("counts", {})}

    def meridian_summary(self) -> dict:
        init = self.ensure_ready()
        if not init.get("ok"):
            return {"ok": False, "error": init.get("error"), "dbPath": self.db_path}
        return self.repo.summary()

