"""Хранилище профилей подключения (без паролей)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from database.profile import ConnectionProfile

from .db import AppDatabase


class ProfileError(Exception):
    pass


class ProfileRepository:
    def __init__(self, db: AppDatabase):
        self._db = db

    def list(self) -> list[ConnectionProfile]:
        rows = self._db.query("SELECT data FROM connection_profiles ORDER BY name COLLATE NOCASE")
        out = []
        for r in rows:
            try:
                out.append(ConnectionProfile.from_dict(json.loads(r["data"])))
            except (ValueError, TypeError):
                continue
        return out

    def get(self, profile_id: str) -> ConnectionProfile | None:
        rows = self._db.query("SELECT data FROM connection_profiles WHERE id = ?", (profile_id,))
        return ConnectionProfile.from_dict(json.loads(rows[0]["data"])) if rows else None

    def save(self, profile: ConnectionProfile) -> ConnectionProfile:
        errors = profile.validate()
        if errors:
            raise ProfileError(" ".join(errors))
        p = profile.normalized()
        now = datetime.now().isoformat(timespec="seconds")
        data = json.dumps(p.to_dict(), ensure_ascii=False)
        try:
            if self.get(p.id) is None:
                self._db.execute(
                    "INSERT INTO connection_profiles(id, name, data, created_at, updated_at) VALUES(?,?,?,?,?)",
                    (p.id, p.name, data, now, now))
            else:
                self._db.execute(
                    "UPDATE connection_profiles SET name = ?, data = ?, updated_at = ? WHERE id = ?",
                    (p.name, data, now, p.id))
        except sqlite3.IntegrityError as exc:
            raise ProfileError(f"Подключение с названием «{p.name}» уже существует.") from exc
        return p

    def delete(self, profile_id: str) -> None:
        self._db.execute("DELETE FROM connection_profiles WHERE id = ?", (profile_id,))
