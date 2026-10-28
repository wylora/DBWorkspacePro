"""Связка сравнения с приложением: чтение моделей, сравнение, выполнение скрипта синхронизации."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from bootstrap import AppServices
from database.editor_session import ExecutionListener
from database.errors import DbWorkspaceError, QueryCancelledError
from database.sql_guard import split_batches
from database.sqltext import quote_ident

from .data_compare import DataCompareRequest, DataCompareResult, compare_data
from .schema import SchemaModel, read_schema
from .schema_diff import SchemaDiff, compare_schemas


@dataclass
class ExecResult:
    ok: bool
    batches_done: int
    batches_total: int
    error: str = ""
    messages: list[str] = field(default_factory=list)
    rolled_back: bool = False
    elapsed: float = 0.0


class _Msgs(ExecutionListener):
    def __init__(self):
        self.messages: list[str] = []

    def on_messages(self, messages):
        self.messages.extend(messages)


class CompareService:
    def __init__(self, services: AppServices):
        self.svc = services

    def read_model(self, profile_id: str, database: str, *, progress=lambda s, f: None, cancel_token=None) -> SchemaModel:
        profile = self.svc.profiles.repo.get(profile_id)
        with self.svc.profiles.lease(profile_id, "compare") as conn:
            return read_schema(conn, database, server=profile.server_spec() if profile else "",
                               cancel_token=cancel_token, progress=progress)

    def compare_schema(self, src: tuple[str, str], tgt: tuple[str, str], *, tables: list[str] | None = None,
                       progress=lambda s, f: None, cancel_token=None, **opts) -> SchemaDiff:
        a = self.read_model(*src, progress=lambda s, f: progress("Исходная: " + s, None), cancel_token=cancel_token)
        b = self.read_model(*tgt, progress=lambda s, f: progress("Целевая: " + s, None), cancel_token=cancel_token)
        if tables:
            a, b = a.only(tables), b.only(tables)
            a.modules, b.modules = {}, {}
        d = compare_schemas(a, b, **opts)
        self.svc.oplog.add("compare", f"Сравнение структуры {src[1]} → {tgt[1]}", "ok", d.summary())
        return d

    def compare_data(self, req: DataCompareRequest, *, progress=lambda s, f: None, cancel_token=None) -> DataCompareResult:
        r = compare_data(self.svc.profiles, req, progress=progress, cancel_token=cancel_token)
        self.svc.oplog.add("compare", f"Сравнение данных {req.source.label} → {req.target.label}", "ok", r.diff.summary())
        return r

    def execute_script(self, profile_id: str, database: str, script: str, *, cancel_token=None,
                       progress=lambda s, f: None) -> ExecResult:
        """Выполняет скрипт в одной транзакции: при первой ошибке всё откатывается."""
        started = time.perf_counter()
        batches, errs = split_batches(script)
        if errs:
            return ExecResult(False, 0, 0, " ".join(errs))
        batches = [b for b in batches if b.text.strip() and not all(
            line.strip().startswith("--") or not line.strip() for line in b.text.splitlines())]
        res = ExecResult(False, 0, len(batches))
        lst = _Msgs()
        with self.svc.profiles.lease(profile_id, "sync") as conn:
            conn.set_query_timeout(0)
            conn.execute_multi("USE " + quote_ident(database), listener=ExecutionListener())
            conn.set_autocommit(False)
            try:
                for i, b in enumerate(batches):
                    progress(f"Пакет {i + 1} из {len(batches)}", i / max(1, len(batches)))
                    conn.execute_multi(b.text, listener=lst, cancel_token=cancel_token)
                    res.batches_done += 1
                conn.commit()
                res.ok = True
            except (DbWorkspaceError, QueryCancelledError) as exc:
                res.error = f"Пакет {res.batches_done + 1}: {exc}"
                try:
                    conn.rollback()
                    res.rolled_back = True
                except DbWorkspaceError:
                    res.messages.append("Откат не подтверждён: соединение потеряно (сервер откатит транзакцию сам).")
            finally:
                try:
                    conn.set_autocommit(True)
                except DbWorkspaceError:
                    pass
        res.messages = lst.messages + res.messages
        res.elapsed = time.perf_counter() - started
        status = "ok" if res.ok else "error"
        self.svc.oplog.add("compare", f"Скрипт синхронизации → {database}", status,
                           f"пакетов {res.batches_done}/{res.batches_total}" + (f"; {res.error}" if res.error else "")
                           + ("; откат выполнен" if res.rolled_back else ""), int(res.elapsed * 1000))
        return res
