"""Кэш схемы базы для автодополнения (загружается в фоне, читается из любого потока)."""
from __future__ import annotations

import logging
import threading

from .completion import ObjectEntry, SchemaSnapshot
from .errors import MetadataError
from .metadata import MetadataReader

log = logging.getLogger("dbworkspace.schema")


class SchemaCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snaps: dict[tuple[str, str], SchemaSnapshot] = {}

    def get(self, conn_key: str, database: str) -> SchemaSnapshot | None:
        with self._lock:
            return self._snaps.get((conn_key, database))

    def invalidate(self, conn_key: str, database: str | None = None) -> None:
        with self._lock:
            for key in [k for k in self._snaps if k[0] == conn_key and (database is None or k[1] == database)]:
                del self._snaps[key]

    def load(self, reader: MetadataReader, conn_key: str, database: str) -> SchemaSnapshot:
        snap = SchemaSnapshot()
        failures = 0
        for kind in ("table", "view", "procedure", "function"):
            try:
                for o in reader.list_objects(database, kind).items:
                    snap.objects.append(ObjectEntry(o.schema, o.name, kind))
            except MetadataError as exc:
                failures += 1
                log.info("Автодополнение: %s недоступны (%s)", kind, exc)
        try:
            snap.columns, snap.truncated = reader.list_all_columns(database)
        except MetadataError as exc:
            failures += 1
            log.info("Автодополнение: столбцы недоступны (%s)", exc)
        if failures >= 5:
            raise MetadataError("Не удалось получить сведения о схеме для автодополнения.")
        with self._lock:
            self._snaps[(conn_key, database)] = snap
        return snap
