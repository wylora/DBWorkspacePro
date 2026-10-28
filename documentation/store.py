"""Снимки структуры базы для раздела «Изменения с прошлой версии документации»."""
from __future__ import annotations

import datetime as dt
import gzip
from pathlib import Path

from comparison.schema import SchemaModel
from utils.files import atomic_write_bytes, sanitize_filename


class SchemaSnapshotStore:
    def __init__(self, root: Path, keep: int = 10):
        self.root = Path(root)
        self.keep = keep

    def _dir(self, profile_id: str, database: str) -> Path:
        return self.root / sanitize_filename(profile_id, "profile") / sanitize_filename(database, "db")

    def save(self, profile_id: str, database: str, model: SchemaModel) -> Path:
        d = self._dir(profile_id, database)
        path = d / f"{dt.datetime.now():%Y%m%d_%H%M%S_%f}.json.gz"
        atomic_write_bytes(path, gzip.compress(model.dumps().encode("utf-8")))
        for old in sorted(d.glob("*.json.gz"))[:-self.keep]:
            old.unlink(missing_ok=True)
        return path

    def latest(self, profile_id: str, database: str) -> SchemaModel | None:
        files = sorted(self._dir(profile_id, database).glob("*.json.gz"))
        if not files:
            return None
        try:
            return SchemaModel.loads(gzip.decompress(files[-1].read_bytes()).decode("utf-8"))
        except (OSError, ValueError, KeyError, TypeError):
            return None
