"""Управление несколькими соединениями: лимит, изолированные соединения для задач."""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .connection import ConnectionState, SqlConnection
from .errors import ConnectionLimitError, NotConnectedError
from .profile import ConnectionProfile

log = logging.getLogger("dbworkspace.manager")


class ConnectionManager:
    def __init__(self, max_connections: int = 5,
                 connection_factory: Callable[..., SqlConnection] | None = None):
        self._max = max(1, int(max_connections))
        self._factory = connection_factory or SqlConnection
        self._lock = threading.RLock()
        self._conns: dict[str, SqlConnection] = {}
        self._primary: dict[str, str] = {}  # profile_id -> connection id
        self._reserved = 0
        self._listeners: list[Callable[[SqlConnection, ConnectionState, ConnectionState], None]] = []

    @property
    def max_connections(self) -> int:
        return self._max

    def set_max_connections(self, n: int) -> None:
        with self._lock:
            self._max = max(1, int(n))

    def add_state_listener(self, cb: Callable[[SqlConnection, ConnectionState, ConnectionState], None]) -> None:
        self._listeners.append(cb)

    def _forward(self, conn: SqlConnection, old: ConnectionState, new: ConnectionState) -> None:
        for cb in list(self._listeners):
            try:
                cb(conn, old, new)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка слушателя менеджера соединений")

    def _reserve(self) -> None:
        with self._lock:
            if len(self._conns) + self._reserved >= self._max:
                raise ConnectionLimitError(
                    f"Достигнут предел одновременных подключений ({self._max}). "
                    "Закройте неиспользуемые подключения или увеличьте лимит в настройках.")
            self._reserved += 1

    def _open(self, profile: ConnectionProfile, password: str | None, label: str) -> SqlConnection:
        self._reserve()
        conn = self._factory(profile, password, label=label)
        conn.add_state_listener(self._forward)
        try:
            conn.connect()
        except BaseException:
            conn.close()
            with self._lock:
                self._reserved -= 1
            raise
        with self._lock:
            self._reserved -= 1
            self._conns[conn.id] = conn
        return conn

    def connect_profile(self, profile: ConnectionProfile, password: str | None = None) -> SqlConnection:
        """Основное (долгоживущее) соединение профиля; повторный вызов возвращает его же."""
        with self._lock:
            cid = self._primary.get(profile.id)
            existing = self._conns.get(cid) if cid else None
        if existing is not None:
            if existing.state is ConnectionState.LOST:
                existing.reconnect()
            return existing
        conn = self._open(profile, password, label="primary")
        with self._lock:
            self._primary[profile.id] = conn.id
        return conn

    def open_session(self, profile_id: str, label: str = "editor") -> SqlConnection:
        """Отдельное долгоживущее соединение (окно запроса) на основе активного подключения профиля.

        Закрывать нужно через release(). Требует активного основного подключения профиля.
        """
        primary = self.primary(profile_id)
        if primary is None or primary._password is None and primary.profile.auth == "sql":
            raise NotConnectedError("Подключение закрыто. Подключитесь к серверу на странице «Подключения».")
        return self._open(primary.profile, primary._password, label)

    @contextmanager
    def lease_session(self, profile_id: str, label: str = "task") -> Iterator[SqlConnection]:
        """Временное соединение на основе активного подключения профиля; закрывается всегда."""
        conn = self.open_session(profile_id, label)
        try:
            yield conn
        finally:
            self.release(conn)

    def release(self, conn: SqlConnection) -> None:
        self._close_conn(conn)

    def primary(self, profile_id: str) -> SqlConnection | None:
        with self._lock:
            cid = self._primary.get(profile_id)
            return self._conns.get(cid) if cid else None

    @contextmanager
    def lease(self, profile: ConnectionProfile, password: str | None = None,
              label: str = "task") -> Iterator[SqlConnection]:
        """Отдельное соединение для независимой задачи; закрывается всегда."""
        conn = self._open(profile, password, label)
        try:
            yield conn
        finally:
            self._close_conn(conn)

    def _close_conn(self, conn: SqlConnection) -> None:
        try:
            conn.close()
        finally:
            with self._lock:
                self._conns.pop(conn.id, None)
                for pid, cid in list(self._primary.items()):
                    if cid == conn.id:
                        self._primary.pop(pid, None)

    def disconnect_profile(self, profile_id: str) -> None:
        conn = self.primary(profile_id)
        if conn is not None:
            self._close_conn(conn)

    def close_all(self) -> None:
        with self._lock:
            conns = list(self._conns.values())
        for c in conns:
            self._close_conn(c)

    def connections(self) -> list[SqlConnection]:
        with self._lock:
            return list(self._conns.values())

    def count(self) -> int:
        with self._lock:
            return len(self._conns)
