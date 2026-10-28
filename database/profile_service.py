"""Сценарии работы с профилями: сохранение, пароли, подключение, проверка."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

from security.credentials import CredentialError, CredentialStore, profile_key
from storage.history import OperationLog
from storage.profiles import ProfileRepository

from .connection import ServerInfo, SqlConnection
from .errors import ConfigError, DbWorkspaceError
from .manager import ConnectionManager
from .profile import AUTH_SQL, ConnectionProfile

log = logging.getLogger("dbworkspace.profiles")


class ProfileService:
    def __init__(self, repo: ProfileRepository, credentials: CredentialStore,
                 manager: ConnectionManager, oplog: OperationLog | None = None):
        self.repo = repo
        self.credentials = credentials
        self.manager = manager
        self.oplog = oplog

    def _log(self, action: str, status: str, message: str = "", started: float | None = None) -> None:
        if self.oplog:
            dur = int((time.time() - started) * 1000) if started else None
            self.oplog.add("connection", action, status, message, dur)

    def save(self, profile: ConnectionProfile, password: str | None = None,
             remember: bool = False) -> ConnectionProfile:
        """Сохраняет профиль. Пароль уходит только в хранилище учётных данных."""
        profile.save_password = bool(remember and profile.auth == AUTH_SQL and self.credentials.persistent)
        saved = self.repo.save(profile)
        key = profile_key(saved.id)
        if saved.auth == AUTH_SQL and password and saved.save_password:
            self.credentials.set(key, password)
        elif not saved.save_password:
            self.credentials.delete(key)
        return saved

    def delete(self, profile_id: str) -> None:
        self.manager.disconnect_profile(profile_id)
        try:
            self.credentials.delete(profile_key(profile_id))
        except CredentialError:
            log.warning("Не удалось удалить пароль профиля из хранилища")
        self.repo.delete(profile_id)

    def stored_password(self, profile: ConnectionProfile) -> str | None:
        if profile.auth != AUTH_SQL:
            return None
        try:
            return self.credentials.get(profile_key(profile.id))
        except CredentialError:
            return None

    def needs_password(self, profile: ConnectionProfile) -> bool:
        return profile.auth == AUTH_SQL and not self.stored_password(profile)

    def connect(self, profile: ConnectionProfile, password: str | None = None) -> SqlConnection:
        started = time.time()
        pw = password or self.stored_password(profile)
        try:
            conn = self.manager.connect_profile(profile, pw)
        except DbWorkspaceError as exc:
            self._log(f"Подключение к «{profile.name}»", "error", str(exc), started)
            raise
        self._log(f"Подключение к «{profile.name}»", "ok", conn.server_info.version, started)
        return conn

    def disconnect(self, profile: ConnectionProfile) -> None:
        self.manager.disconnect_profile(profile.id)
        self._log(f"Отключение от «{profile.name}»", "info")

    @contextmanager
    def lease(self, profile_id: str, label: str = "task") -> Iterator[SqlConnection]:
        """Отдельное временное соединение для фоновой задачи.

        Если профиль подключён — берём пароль из активного подключения; иначе — из хранилища учётных данных
        (для аутентификации SQL Server пароль должен быть сохранён). Соединение закрывается всегда.
        """
        primary = self.manager.primary(profile_id)
        if primary is not None and primary.is_connected:
            with self.manager.lease_session(profile_id, label) as conn:
                yield conn
            return
        profile = self.repo.get(profile_id)
        if profile is None:
            raise ConfigError("Профиль подключения удалён или не найден.")
        password = self.stored_password(profile)
        if profile.auth == AUTH_SQL and not password:
            raise ConfigError(f"Для фонового выполнения нужен сохранённый пароль профиля «{profile.name}» "
                              "(включите «Сохранить пароль» в настройках подключения).")
        with self.manager.lease(profile, password, label) as conn:
            yield conn

    def test(self, profile: ConnectionProfile, password: str | None = None) -> ServerInfo:
        """Проверка соединения на отдельном временном подключении."""
        errors = profile.validate()
        if errors:
            raise ConfigError(" ".join(errors))
        started = time.time()
        pw = password or self.stored_password(profile)
        try:
            with self.manager.lease(profile.normalized(), pw, label="test") as conn:
                conn.fetch_rows("SELECT 1")
                info = conn.server_info
        except DbWorkspaceError as exc:
            self._log(f"Проверка «{profile.name}»", "error", str(exc), started)
            raise
        self._log(f"Проверка «{profile.name}»", "ok", info.version, started)
        return info
