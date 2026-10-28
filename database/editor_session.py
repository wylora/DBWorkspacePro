"""Сеанс окна запроса: собственное соединение, режим «только чтение», подтверждения, транзакции.

Правила безопасности (проверяются в самой сессии, а не только в интерфейсе):
* по умолчанию разрешено только чтение — изменяющие скрипты отклоняются;
* изменяющий скрипт выполняется лишь при allow_modifications=True И confirmed=True;
* при ошибке выполнение останавливается (последующие пакеты GO не запускаются);
* потеря связи никогда не приводит к автоповтору команд.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from utils.export import ExportResult, open_writer

from .connection import SqlConnection, ConnectionState
from .errors import (ConnectionLostError, DbWorkspaceError, QueryCancelledError, StopExecution)
from .sql_guard import Analysis, RiskLevel, analyze, describe
from .sqltext import quote_ident

log = logging.getLogger("dbworkspace.editor")


class ScriptError(DbWorkspaceError):
    pass


class ModificationsNotAllowed(DbWorkspaceError):
    def __init__(self, analysis: Analysis | None, message: str = ""):
        super().__init__(message or "Скрипт изменяет данные или схему, а режим «только чтение» включён.")
        self.analysis = analysis


class ConfirmationRequired(DbWorkspaceError):
    def __init__(self, analysis: Analysis):
        super().__init__("Для выполнения изменяющего скрипта требуется подтверждение.\n" + describe(analysis))
        self.analysis = analysis


class ExecutionListener:
    """Слушатель хода выполнения (вызывается из рабочего потока). Методы по умолчанию ничего не делают."""

    def on_result(self, index: int, columns: list[str]) -> int | None:
        return None

    def on_rows(self, index: int, rows: list[tuple]) -> None: ...
    def on_result_done(self, index: int, count: int, truncated: bool) -> None: ...
    def on_rowcount(self, index: int, affected: int) -> None: ...
    def on_messages(self, messages: list[str]) -> None: ...


@dataclass
class ResultSet:
    index: int
    kind: str                      # "rows" | "count"
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    rowset_number: int = 0         # номер набора со строками (1, 2, …) — для полной выгрузки


@dataclass
class ExecutionReport:
    analysis: Analysis
    results: list[ResultSet] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    elapsed: float = 0.0
    error: str | None = None
    cancelled: bool = False
    batches_total: int = 0
    batches_done: int = 0

    @property
    def total_rows(self) -> int:
        return sum(r.row_count for r in self.results if r.kind == "rows")


class _Collector(ExecutionListener):
    def __init__(self, report: ExecutionReport, external: ExecutionListener | None, preview: int | None):
        self.report, self.ext, self.preview = report, external, preview
        self._by_index: dict[int, ResultSet] = {}
        self._offset = 0
        self._rowsets = 0
        self._batch_base = 0

    def start_batch(self) -> None:
        self._batch_base = self._offset

    def _gi(self, i: int) -> int:
        self._offset = max(self._offset, self._batch_base + i + 1)
        return self._batch_base + i

    def on_result(self, index, columns):
        gi = self._gi(index)
        self._rowsets += 1
        rs = ResultSet(gi, "rows", list(columns), rowset_number=self._rowsets)
        self._by_index[gi] = rs
        self.report.results.append(rs)
        if self.ext:
            self.ext.on_result(gi, list(columns))
        return self.preview

    def on_rows(self, index, rows):
        gi = self._batch_base + index
        self._by_index[gi].rows.extend(rows)
        if self.ext:
            self.ext.on_rows(gi, rows)

    def on_result_done(self, index, count, truncated):
        gi = self._batch_base + index
        rs = self._by_index[gi]
        rs.row_count, rs.truncated = count, truncated
        if self.ext:
            self.ext.on_result_done(gi, count, truncated)

    def on_rowcount(self, index, affected):
        gi = self._gi(index)
        rs = ResultSet(gi, "count", row_count=affected)
        self.report.results.append(rs)
        if self.ext:
            self.ext.on_rowcount(gi, affected)

    def on_messages(self, messages):
        self.report.messages.extend(messages)
        if self.ext:
            self.ext.on_messages(messages)


class _Null(ExecutionListener):
    pass


class EditorSession:
    def __init__(self, connection: SqlConnection, *, release: Callable[[SqlConnection], None] | None = None,
                 sibling_factory: Callable[[], SqlConnection] | None = None,
                 preview_limit: int = 1000, batch_size: int = 1000):
        self.conn = connection
        self._release = release
        self._sibling_factory = sibling_factory
        self.preview_limit = preview_limit
        self.batch_size = batch_size
        self.allow_modifications = False          # безопасное значение по умолчанию
        self.database: str | None = None
        self.in_transaction = False
        self._applied_db: str | None = None
        self._applied_gen = -1

    # ---- настройка ---------------------------------------------------
    @property
    def transaction_mode(self) -> bool:
        return not self.conn.autocommit

    def set_transaction_mode(self, on: bool) -> None:
        if not on and self.in_transaction:
            raise DbWorkspaceError("Сначала зафиксируйте (COMMIT) или откатите (ROLLBACK) транзакцию.")
        self.conn.set_autocommit(not on)

    def configure(self, *, database: str | None = None, transaction: bool | None = None,
                  timeout: int | None = None) -> None:
        if database is not None:
            self.database = database or None
        if transaction is not None and transaction != self.transaction_mode:
            self.set_transaction_mode(transaction)
        if timeout is not None:
            self.conn.set_query_timeout(timeout)

    def commit(self) -> None:
        self.conn.commit()
        self.in_transaction = False

    def rollback(self) -> None:
        self.conn.rollback()
        self.in_transaction = False

    def cancel(self) -> bool:
        return self.conn.cancel_running()

    def close(self) -> None:
        """Закрывает соединение; незавершённая транзакция откатывается."""
        try:
            if self.in_transaction and self.conn.is_connected:
                self.conn.rollback()
        except DbWorkspaceError:
            log.warning("Не удалось откатить транзакцию при закрытии сеанса")
        self.in_transaction = False
        if self._release:
            self._release(self.conn)
        else:
            self.conn.close()

    # ---- контекст базы -----------------------------------------------
    @staticmethod
    def _say(report: ExecutionReport, listener: ExecutionListener | None, text: str) -> None:
        report.messages.append(text)
        if listener:
            listener.on_messages([text])

    def _ensure_connected(self, report: ExecutionReport | None = None,
                          listener: ExecutionListener | None = None) -> None:
        if self.conn.state is ConnectionState.LOST:
            self.conn.reconnect()
            if self.in_transaction and report is not None:
                self._say(report, listener, "Соединение восстановлено. Предыдущая транзакция потеряна "
                                            "(сервер её откатил).")
            self.in_transaction = False

    def _apply_context(self) -> None:
        if self.database and (self.database != self._applied_db or self.conn.generation != self._applied_gen):
            self.conn.execute_multi("USE " + quote_ident(self.database), listener=_Null())
            self._applied_db, self._applied_gen = self.database, self.conn.generation
        elif not self.database:
            self._applied_gen = self.conn.generation

    def current_database(self) -> str | None:
        res = self.conn.fetch_rows("SELECT DB_NAME()")
        return str(res.rows[0][0]) if res.rows and res.rows[0][0] else None

    # ---- выполнение --------------------------------------------------
    def execute(self, sql: str, *, confirmed: bool = False, cancel_token=None,
                listener: ExecutionListener | None = None, preview_limit: int | None = None) -> ExecutionReport:
        analysis = analyze(sql)
        if analysis.errors:
            raise ScriptError(" ".join(analysis.errors))
        if not analysis.batches:
            raise ScriptError("Нет команд для выполнения.")
        if analysis.level > RiskLevel.READ:
            if not self.allow_modifications:
                raise ModificationsNotAllowed(analysis)
            if not confirmed:
                raise ConfirmationRequired(analysis)

        report = ExecutionReport(analysis, batches_total=len(analysis.batches))
        limit = self.preview_limit if preview_limit is None else preview_limit
        collector = _Collector(report, listener, limit)
        started = time.perf_counter()
        try:
            self._ensure_connected(report, listener)
            self._apply_context()
            for batch in analysis.batches:
                if cancel_token is not None and cancel_token.is_cancelled:
                    report.cancelled = True
                    break
                collector.start_batch()
                self.conn.execute_multi(batch.text, listener=collector, batch_size=self.batch_size,
                                        default_max_rows=limit, cancel_token=cancel_token)
                report.batches_done += 1
        except QueryCancelledError:
            report.cancelled = True
        except ConnectionLostError as exc:
            report.error = str(exc)
            if self.in_transaction:
                self._say(report, listener, "Транзакция потеряна: сервер откатит все её изменения.")
                self.in_transaction = False
        except DbWorkspaceError as exc:
            report.error = str(exc)
        finally:
            report.elapsed = time.perf_counter() - started
            if self.transaction_mode and self.conn.is_connected:
                self.in_transaction = True
        if analysis.has_use and not report.error and not report.cancelled:
            try:
                self.database = self._applied_db = self.current_database()
            except DbWorkspaceError:
                pass
        return report

    # ---- полная выгрузка ---------------------------------------------
    def export_full(self, sql: str, path, fmt: str, *, rowset_number: int = 1, cancel_token=None,
                    progress: Callable[[int], None] | None = None, **options) -> ExportResult:
        """Повторно выполняет запрос на ОТДЕЛЬНОМ соединении и пишет выбранный набор строк в файл потоком.

        Только для запросов на чтение: повторное выполнение изменяющего скрипта могло бы повторить изменения.
        """
        analysis = analyze(sql)
        if analysis.errors:
            raise ScriptError(" ".join(analysis.errors))
        if not analysis.is_read_only:
            raise ModificationsNotAllowed(
                analysis, "Полная выгрузка повторно выполняет запрос и поэтому доступна только для запросов "
                          "на чтение. Выгрузите показанные строки или выполните выгрузку отдельным запросом SELECT.")
        if self._sibling_factory is None:
            raise DbWorkspaceError("Полная выгрузка недоступна: нет возможности открыть отдельное соединение.")
        conn = self._sibling_factory()
        side = EditorSession(conn, release=self._release, batch_size=self.batch_size)
        side.database = self.database
        writer_box: list = []

        class _L(ExecutionListener):
            def __init__(self):
                self.n = 0
                self.active = False

            def on_result(self, index, columns):
                self.n += 1
                self.active = self.n == rowset_number
                if self.active:
                    writer_box.append(open_writer(path, fmt, columns, **options))
                    return None
                return 0

            def on_rows(self, index, rows):
                if self.active:
                    writer_box[0].write_rows(rows)
                    if progress:
                        progress(writer_box[0].result.rows)

            def on_result_done(self, index, count, truncated):
                if self.active:
                    raise StopExecution()

        listener = _L()
        try:
            side._apply_context()
            try:
                for batch in analysis.batches:
                    conn.execute_multi(batch.text, listener=listener, batch_size=self.batch_size,
                                       cancel_token=cancel_token)
            except StopExecution:
                pass
            if not writer_box:
                raise DbWorkspaceError("Запрос не вернул набора строк для выгрузки.")
            return writer_box[0].close()
        except BaseException:
            for w in writer_box:
                w.abort()
            raise
        finally:
            side.close()
