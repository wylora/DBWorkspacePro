"""Сборка ODBC-строки подключения с корректным экранированием значений."""
from __future__ import annotations

from utils.logging_setup import redact

from .driver import driver_major
from .errors import ConfigError
from .profile import AUTH_SQL, ConnectionProfile

APP_NAME = "DBWorkspacePro"
_SPECIAL = set(';{}="\' ')


def odbc_quote(value: str, always: bool = False) -> str:
    """Оборачивает значение в {…}, экранируя '}' как '}}', когда это необходимо."""
    if always or value == "" or value != value.strip() or any(c in _SPECIAL for c in value):
        return "{" + value.replace("}", "}}") + "}"
    return value


def build_connection_string(profile: ConnectionProfile, password: str | None, driver: str) -> str:
    errors = profile.validate()
    if errors:
        raise ConfigError(" ".join(errors))
    p = profile.normalized()
    if p.encrypt == "strict":
        major = driver_major(driver)
        if major is None or major < 18:
            raise ConfigError("Режим шифрования «strict» требует ODBC Driver 18 for SQL Server или новее.")

    parts = [("DRIVER", odbc_quote(driver, always=True)), ("SERVER", odbc_quote(p.server_spec()))]
    if p.database:
        parts.append(("DATABASE", odbc_quote(p.database)))
    if p.auth == AUTH_SQL:
        if not password:
            raise ConfigError("Не указан пароль для аутентификации SQL Server.")
        parts += [("UID", odbc_quote(p.username)), ("PWD", odbc_quote(password))]
    else:
        parts.append(("Trusted_Connection", "yes"))
    parts += [
        ("Encrypt", p.encrypt),
        ("TrustServerCertificate", "yes" if p.trust_server_certificate else "no"),
        ("APP", APP_NAME),
    ]
    return ";".join(f"{k}={v}" for k, v in parts) + ";"


def redacted(connection_string: str) -> str:
    """Версия строки без пароля — для журналов."""
    return redact(connection_string)
