"""Потокобезопасное соединение с SQL Server (pyodbc), не зависящее от GUI.

Правила:
* одно соединение — один поток одновременно (RLock);
* автоповтор после потери связи — только при safe_to_retry=True и только если
  потребителю ещё не передано ни одной порции данных;
* изменяющие команды никогда не повторяются автоматически (по умолчанию safe_to_retry=False);
* пароль хранится только в памяти на время жизни соединения.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from .connection_string import build_connection_string, redacted
from .driver import choose_driver, installed_drivers, load_pyodbc, missing_driver_message
from .errors import (
    ConnectionFailedError, ConnectionLostError, DbWorkspaceError, DriverNotAvailableError,
    NotConnectedError, QueryCancelledError, QueryError, QueryTimeoutError,
    friendly_message, is_cancelled, is_connection_lost, is_timeout,
)
from .profile import ConnectionProfile

log = logging.getLogger("dbworkspace.connection")


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"
    ERROR = "error"


STATE_LABELS = {
    ConnectionState.DISCONNECTED: "Отключено",
    ConnectionState.CONNECTING: "Подключение…",
    ConnectionState.CONNECTED: "Подключено",
    ConnectionState.LOST: "Связь потеряна",
    ConnectionState.ERROR: "Ошибка",
}


@dataclass
class ServerInfo:
    version: str = ""
    edition: str = ""
    server_name: str = ""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool = False


@dataclass
class StreamResult:
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False


def _default_connector(connection_string: str, timeout: int, autocommit: bool):
    return load_pyodbc().connect(connection_string, timeout=timeout, autocommit=autocommit)


class SqlConnection:
    def __init__(
        self,
        profile: ConnectionProfile,
        password: str | None = None,
        *,
        connector: Callable[[str, int, bool], Any] | None = None,
        driver_lister: Callable[[], Sequence[str]] | None = None,
        label: str = "",
        autocommit: bool = True,
    ) -> None:
        self.id = uuid.uuid4().hex
        self.profile = profile
        self.label = label
        self._password = password
        self._connector = connector or _default_connector
        self._driver_lister = driver_lister or installed_drivers
        self._autocommit = autocommit
        self._lock = threading.RLock()
        self._raw: Any = None
        self._current_cursor: Any = None
        self._state = ConnectionState.DISCONNECTED
        self._listeners: list[Callable[["SqlConnection", ConnectionState, ConnectionState], None]] = []
        self.last_error: str = ""
        self.connected_at: float | None = None
        self.server_info = ServerInfo()
        self.driver_used: str = ""
        self.generation = 0                 # растёт при каждом (пере)подключении
        self._timeout_override: int | None = None

    # --- состояние ----------------------------------------------------
    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state is ConnectionState.CONNECTED and self._raw is not None

    def add_state_listener(self, cb: Callable[["SqlConnection", ConnectionState, ConnectionState], None]) -> None:
        self._listeners.append(cb)

    def _set_state(self, new: ConnectionState) -> None:
        old, self._state = self._state, new
        if old is new:
            return
        for cb in list(self._listeners):
            try:
                cb(self, old, new)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка слушателя состояния соединения")

    # --- подключение --------------------------------------------------
    def connect(self) -> None:
        with self._lock:
            if self.is_connected:
                return
            self._open_locked()

    def reconnect(self) -> None:
        with self._lock:
            self._discard_raw()
            self._open_locked()

    def _open_locked(self) -> None:
        self._set_state(ConnectionState.CONNECTING)
        try:
            driver = choose_driver(self.profile.driver, self._driver_lister())
            if not driver:
                raise DriverNotAvailableError(missing_driver_message(self.profile.driver))
            cs = build_connection_string(self.profile, self._password, driver)
            log.info("Подключение: %s", redacted(cs))
            self._raw = self._connector(cs, self.profile.connect_timeout, self._autocommit)
            self.driver_used = driver
            self._apply_timeout()
            self._read_server_info()
            self.generation += 1
            self.connected_at = time.time()
            self.last_error = ""
            self._set_state(ConnectionState.CONNECTED)
        except DbWorkspaceError as exc:
            self._fail(str(exc))
            raise
        except Exception as exc:  # noqa: BLE001
            msg = friendly_message(exc)
            self._fail(msg)
            log.warning("Ошибка подключения: %s", msg)
            raise ConnectionFailedError(msg) from exc

    def _apply_timeout(self) -> None:
        value = self._timeout_override if self._timeout_override is not None else self.profile.query_timeout
        try:
            self._raw.timeout = int(value)
        except Exception:  # noqa: BLE001
            log.debug("Не удалось задать тайм-аут запроса")

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        self._discard_raw()
        self._set_state(ConnectionState.ERROR)

    def _read_server_info(self) -> None:
        cur = None
        try:
            cur = self._raw.cursor()
            cur.execute(
                "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128)), "
                "CAST(SERVERPROPERTY('Edition') AS nvarchar(128)), "
                "CAST(SERVERPROPERTY('ServerName') AS nvarchar(256))")
            row = cur.fetchone()
            if row:
                self.server_info = ServerInfo(str(row[0] or ""), str(row[1] or ""), str(row[2] or ""))
        except Exception:  # noqa: BLE001
            log.debug("Не удалось получить сведения о сервере")
        finally:
            self._close_cursor(cur)

    def _discard_raw(self) -> None:
        raw, self._raw = self._raw, None
        self._current_cursor = None
        if raw is not None:
            try:
                raw.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _close_cursor(cur: Any) -> None:
        if cur is not None:
            try:
                cur.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Закрывает соединение. Если выполняется запрос — сначала пытается его отменить."""
        if not self._lock.acquire(timeout=2.0):
            self.cancel_running()
            if not self._lock.acquire(timeout=3.0):
                log.warning("Соединение занято, закрытие отложено")
                return
        try:
            self._discard_raw()
            self._password = None  # после явного закрытия пароль в памяти не держим
            self._set_state(ConnectionState.DISCONNECTED)
        finally:
            self._lock.release()

    def cancel_running(self) -> bool:
        """Пытается отменить выполняющийся запрос (если драйвер поддерживает cancel)."""
        cur = self._current_cursor
        cancel = getattr(cur, "cancel", None)
        if cur is None or cancel is None:
            return False
        try:
            cancel()
            return True
        except Exception:  # noqa: BLE001
            log.exception("Не удалось отменить запрос")
            return False

    # --- выполнение ---------------------------------------------------
    def stream(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        consumer: Callable[[list[tuple]], None],
        batch_size: int = 1000,
        max_rows: int | None = None,
        cancel_token: Any = None,
        safe_to_retry: bool = False,
    ) -> StreamResult:
        """Выполняет запрос и передаёт строки порциями (fetchmany) в consumer."""
        attempts = 2 if safe_to_retry else 1
        for attempt in range(attempts):
            try:
                return self._stream_once(sql, params, consumer, batch_size, max_rows, cancel_token, safe_to_retry)
            except ConnectionLostError as exc:
                if attempt + 1 >= attempts or exc.batches_delivered:
                    raise
                log.info("Связь потеряна, повторное подключение для безопасного повтора чтения")
                with self._lock:
                    self._discard_raw()
                    self._open_locked()
        raise AssertionError("unreachable")  # pragma: no cover

    def fetch_rows(self, sql: str, params: Sequence[Any] = (), *, max_rows: int | None = None,
                   batch_size: int = 1000, cancel_token: Any = None,
                   safe_to_retry: bool = False) -> QueryResult:
        rows: list[tuple] = []

        def consume(batch: list[tuple]) -> None:
            rows.extend(batch)

        def run() -> StreamResult:
            rows.clear()
            return self.stream(sql, params, consumer=consume, batch_size=batch_size, max_rows=max_rows,
                               cancel_token=cancel_token, safe_to_retry=safe_to_retry)

        res = run()
        return QueryResult(res.columns, rows, res.truncated)

    def _stream_once(self, sql, params, consumer, batch_size, max_rows, cancel_token, auto_reconnect) -> StreamResult:
        with self._lock:
            if self._raw is None or self._state is not ConnectionState.CONNECTED:
                if self._state is ConnectionState.LOST and auto_reconnect:
                    self._open_locked()
                elif self._state is ConnectionState.LOST:
                    raise ConnectionLostError("Соединение с сервером потеряно. Переподключитесь.")
                else:
                    raise NotConnectedError("Нет подключения к серверу.")
            cur = None
            delivered = 0
            result = StreamResult()
            try:
                cur = self._raw.cursor()
                self._current_cursor = cur
                if params:
                    cur.execute(sql, *params)
                else:
                    cur.execute(sql)
                desc = getattr(cur, "description", None)
                result.columns = [d[0] for d in desc] if desc else []
                while True:
                    if cancel_token is not None and cancel_token.is_cancelled:
                        raise QueryCancelledError("Операция отменена.")
                    want = batch_size
                    if max_rows is not None:
                        want = min(batch_size, max_rows - result.row_count)
                        if want <= 0:
                            if cur.fetchmany(1):
                                result.truncated = True
                            break
                    if not result.columns:
                        break
                    chunk = cur.fetchmany(want)
                    if not chunk:
                        break
                    batch = [tuple(r) for r in chunk]
                    consumer(batch)
                    delivered += 1
                    result.row_count += len(batch)
                return result
            except DbWorkspaceError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._raise_translated(exc, delivered)
            finally:
                self._current_cursor = None
                self._close_cursor(cur)

    def _raise_translated(self, exc: Exception, delivered: int = 0) -> None:
        if is_cancelled(exc):
            raise QueryCancelledError("Операция отменена.") from exc
        if is_timeout(exc):
            raise QueryTimeoutError(friendly_message(exc)) from exc
        if is_connection_lost(exc):
            self._discard_raw()
            self._set_state(ConnectionState.LOST)
            raise ConnectionLostError(friendly_message(exc), delivered) from exc
        raise QueryError(friendly_message(exc)) from exc

    # --- параметры сеанса и транзакции ---------------------------------
    @property
    def autocommit(self) -> bool:
        return self._autocommit

    def set_autocommit(self, flag: bool) -> None:
        with self._lock:
            self._autocommit = bool(flag)
            if self._raw is not None:
                self._raw.autocommit = bool(flag)

    def commit(self) -> None:
        with self._lock:
            if self._raw is None:
                raise NotConnectedError("Нет подключения к серверу.")
            try:
                self._raw.commit()
            except Exception as exc:  # noqa: BLE001
                self._raise_translated(exc)

    def rollback(self) -> None:
        with self._lock:
            if self._raw is None:
                raise NotConnectedError("Нет подключения к серверу.")
            try:
                self._raw.rollback()
            except Exception as exc:  # noqa: BLE001
                self._raise_translated(exc)

    def set_query_timeout(self, seconds: int) -> None:
        with self._lock:
            self._timeout_override = max(0, int(seconds))
            if self._raw is not None:
                self._apply_timeout()

    # --- несколько наборов результатов ---------------------------------
    def execute_multi(self, sql: str, params: Sequence[Any] = (), *, listener: Any, batch_size: int = 1000,
                      default_max_rows: int | None = None, cancel_token: Any = None) -> None:
        """Выполняет пакет, который может вернуть несколько наборов результатов.

        listener.on_result(index, columns) -> предел строк или None; on_rows(index, rows);
        on_result_done(index, count, truncated); on_rowcount(index, affected); on_messages(list[str]).
        Никогда не повторяется автоматически (пакет может изменять данные).
        """
        with self._lock:
            if self._raw is None or self._state is not ConnectionState.CONNECTED:
                if self._state is ConnectionState.LOST:
                    raise ConnectionLostError("Соединение с сервером потеряно. Переподключитесь.")
                raise NotConnectedError("Нет подключения к серверу.")
            cur = None
            delivered = 0
            seen_msgs: list = []
            try:
                cur = self._raw.cursor()
                self._current_cursor = cur
                if params:
                    cur.execute(sql, *params)
                else:
                    cur.execute(sql)
                idx = 0
                while True:
                    self._emit_messages(cur, seen_msgs, listener)
                    desc = getattr(cur, "description", None)
                    if desc:
                        cap = listener.on_result(idx, [d[0] for d in desc])
                        if cap is None:
                            cap = default_max_rows
                        count, truncated = 0, False
                        while True:
                            if cancel_token is not None and cancel_token.is_cancelled:
                                raise QueryCancelledError("Операция отменена.")
                            want = batch_size
                            if cap is not None:
                                want = min(batch_size, cap - count)
                                if want <= 0:
                                    truncated = bool(cur.fetchmany(1))
                                    break
                            chunk = cur.fetchmany(want)
                            if not chunk:
                                break
                            batch = [tuple(r) for r in chunk]
                            listener.on_rows(idx, batch)
                            count += len(batch)
                            delivered += 1
                        listener.on_result_done(idx, count, truncated)
                    else:
                        rc = getattr(cur, "rowcount", -1)
                        if rc is not None and rc >= 0:
                            listener.on_rowcount(idx, rc)
                    idx += 1
                    if not cur.nextset():
                        break
                self._emit_messages(cur, seen_msgs, listener)
            except DbWorkspaceError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._raise_translated(exc, delivered)
            finally:
                self._current_cursor = None
                self._close_cursor(cur)

    @staticmethod
    def _emit_messages(cur: Any, seen: list, listener: Any) -> None:
        msgs = getattr(cur, "messages", None)
        if not msgs:
            return
        msgs = list(msgs)
        new = msgs[len(seen):] if msgs[:len(seen)] == seen else msgs
        seen[:] = msgs
        texts = []
        for m in new:
            text = m[1] if isinstance(m, (tuple, list)) and len(m) > 1 else str(m)
            texts.append(re.sub(r"^\[[^\]]*\]\s*(\[[^\]]*\]\s*)*", "", str(text)).strip())
        if texts:
            listener.on_messages(texts)

    def ping(self) -> bool:
        try:
            self.fetch_rows("SELECT 1")
            return True
        except DbWorkspaceError:
            return False
