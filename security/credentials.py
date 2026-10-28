"""Хранение паролей. Пароли НИКОГДА не пишутся в SQLite, JSON или журналы.

* KeyringCredentialStore — системное хранилище (на Windows: Credential Manager).
* InMemoryCredentialStore — только на время работы программы (запасной вариант,
  если системное хранилище недоступно, и для тестов).
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

SERVICE_NAME = "DBWorkspacePro"
log = logging.getLogger("dbworkspace.security")


class CredentialError(Exception):
    pass


class CredentialStore(Protocol):
    persistent: bool

    def is_available(self) -> bool: ...
    def backend_name(self) -> str: ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, secret: str) -> None: ...
    def delete(self, key: str) -> None: ...


def profile_key(profile_id: str) -> str:
    return f"profile:{profile_id}"


class KeyringCredentialStore:
    persistent = True

    def __init__(self, service: str = SERVICE_NAME):
        self._service = service

    def _keyring(self):
        try:
            import keyring  # noqa: WPS433
            return keyring
        except ImportError:
            return None

    def is_available(self) -> bool:
        kr = self._keyring()
        if kr is None:
            return False
        try:
            backend = kr.get_keyring()
            return getattr(backend, "priority", 0) > 0
        except Exception:  # noqa: BLE001
            return False

    def backend_name(self) -> str:
        kr = self._keyring()
        if kr is None:
            return "keyring не установлен"
        try:
            return type(kr.get_keyring()).__name__
        except Exception:  # noqa: BLE001
            return "недоступно"

    def _call(self, fn, *args):
        kr = self._keyring()
        if kr is None:
            raise CredentialError("Библиотека keyring не установлена")
        try:
            return fn(kr)(*args)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ошибка хранилища учётных данных: %s", exc.__class__.__name__)
            raise CredentialError(
                "Не удалось обратиться к хранилищу учётных данных Windows "
                f"({exc.__class__.__name__})."
            ) from exc

    def get(self, key: str) -> str | None:
        return self._call(lambda kr: kr.get_password, self._service, key)

    def set(self, key: str, secret: str) -> None:
        self._call(lambda kr: kr.set_password, self._service, key, secret)

    def delete(self, key: str) -> None:
        kr = self._keyring()
        if kr is None:
            return
        try:
            kr.delete_password(self._service, key)
        except Exception as exc:  # noqa: BLE001
            # «не найдено» — не ошибка
            if exc.__class__.__name__ != "PasswordDeleteError":
                raise CredentialError("Не удалось удалить пароль из хранилища") from exc


class InMemoryCredentialStore:
    persistent = False

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._lock = threading.Lock()

    def is_available(self) -> bool:
        return True

    def backend_name(self) -> str:
        return "память процесса (не сохраняется на диск)"

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._data.get(key)

    def set(self, key: str, secret: str) -> None:
        with self._lock:
            self._data[key] = secret

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


def default_store() -> CredentialStore:
    """Системное хранилище, если оно есть, иначе — только память процесса."""
    store = KeyringCredentialStore()
    if store.is_available():
        return store
    log.warning("Системное хранилище паролей недоступно: пароли не будут сохраняться")
    return InMemoryCredentialStore()
