"""Снимки данных отчётов (для сравнения с предыдущим запуском). Хранятся сжатым JSON в папке данных приложения."""
from __future__ import annotations

import datetime as dt
import gzip
import json
from pathlib import Path

from utils.export import json_value
from utils.files import atomic_write_bytes, sanitize_filename


class SnapshotStore:
    def __init__(self, root: Path, keep: int = 20, max_rows: int = 100_000):
        self.root = Path(root)
        self.keep = keep
        self.max_rows = max_rows

    def _dir(self, task_id: str, source_id: str) -> Path:
        return self.root / sanitize_filename(task_id, "task") / sanitize_filename(source_id, "source")

    def save(self, task_id: str, source_id: str, columns: list[str], rows: list[tuple], when: dt.datetime | None = None
             ) -> Path:
        when = when or dt.datetime.now()
        d = self._dir(task_id, source_id)
        payload = {"ts": when.isoformat(timespec="seconds"), "columns": list(columns),
                   "rows": [[json_value(v) for v in r] for r in rows[: self.max_rows]],
                   "truncated": len(rows) > self.max_rows}
        path = d / f"{when:%Y%m%d_%H%M%S_%f}.json.gz"
        atomic_write_bytes(path, gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8")))
        for old in sorted(d.glob("*.json.gz"))[:-self.keep]:
            old.unlink(missing_ok=True)
        return path

    def list(self, task_id: str, source_id: str) -> list[Path]:
        return sorted(self._dir(task_id, source_id).glob("*.json.gz"))

    @staticmethod
    def load(path: Path) -> dict:
        data = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
        data["rows"] = [tuple(r) for r in data["rows"]]
        return data

    def previous_and_latest(self, task_id: str, source_id: str) -> tuple[dict | None, dict | None]:
        files = self.list(task_id, source_id)
        if not files:
            return None, None
        latest = self.load(files[-1])
        prev = self.load(files[-2]) if len(files) > 1 else None
        return prev, latest
