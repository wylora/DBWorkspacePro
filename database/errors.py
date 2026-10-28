"""Исключения и понятные сообщения об ошибках SQL Server / ODBC."""
from __future__ import annotations

import re

from utils.logging_setup import redact


class DbWorkspaceError(Exception):
    """Базовое исключение приложения; str(e) — понятное пользователю сообщение."""


class ConfigError(DbWorkspaceError):
    pass


class DriverNotAvailableError(DbWorkspaceError):
    pass


class ConnectionFailedError(DbWorkspaceError):
    pass


class ConnectionLostError(DbWorkspaceError):
    def __init__(self, message: str, batches_delivered: int = 0):
        super().__init__(message)
        self.batches_delivered = batches_delivered


class ConnectionLimitError(DbWorkspaceError):
    pass


class NotConnectedError(DbWorkspaceError):
    pass


class QueryError(DbWorkspaceError):
    pass


class QueryTimeoutError(QueryError):
    pass


class QueryCancelledError(QueryError):
    pass


class MetadataError(DbWorkspaceError):
    pass


class StopExecution(DbWorkspaceError):
    """Служебное: слушатель просит прервать выполнение (например, нужный набор результатов уже получен)."""


NO_DRIVER_MESSAGE = (
    "Не найден ODBC-драйвер для SQL Server. Установите «Microsoft ODBC Driver 18 for SQL Server» "
    "(https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) "
    "и перезапустите приложение."
)

_LOST_STATES = {"08S01", "08003", "08007", "08S02", "08004"}
_LOST_TEXT = (
    "communication link failure",
    "connection is closed",
    "connection not open",
    "closed connection",
    "forcibly closed",
    "connection reset",
    "connection is dead",
    "tcp provider: an existing connection",
)


def odbc_error_parts(exc: BaseException) -> tuple[str, str]:
    """(SQLSTATE, текст) из исключения pyodbc; для прочих — ('', str(exc))."""
    args = getattr(exc, "args", ()) or ()
    state = ""
    text = ""
    if args and isinstance(args[0], str) and re.fullmatch(r"[0-9A-Z]{5}", args[0]):
        state = args[0]
        text = str(args[1]) if len(args) > 1 else ""
    else:
        text = " ".join(str(a) for a in args) if args else str(exc)
    return state, text


def _native_code(text: str) -> int | None:
    m = re.search(r"\((\d{3,6})\)(?:\s*\(SQL\w+\))?\s*$", text.strip()) or re.search(r"\((\d{3,6})\)", text)
    return int(m.group(1)) if m else None


def is_connection_lost(exc: BaseException) -> bool:
    state, text = odbc_error_parts(exc)
    if state in _LOST_STATES:
        return True
    low = text.lower()
    return any(t in low for t in _LOST_TEXT)


def is_timeout(exc: BaseException) -> bool:
    state, text = odbc_error_parts(exc)
    return state in {"HYT00", "HYT01"} or "timeout expired" in text.lower()


def is_cancelled(exc: BaseException) -> bool:
    state, text = odbc_error_parts(exc)
    return state == "HY008" or "operation canceled" in text.lower()


def friendly_message(exc: BaseException) -> str:
    """Понятное русское сообщение без трассировки и без секретов."""
    if isinstance(exc, DbWorkspaceError):
        return str(exc)
    state, text = odbc_error_parts(exc)
    low = text.lower()
    code = _native_code(text)

    if "certificate verify failed" in low or "ssl provider" in low or "certificate chain" in low:
        return ("Не удалось проверить сертификат сервера. Установите доверенный сертификат на этот "
                "компьютер либо (осознанно, если вы доверяете серверу) включите «Доверять сертификату сервера» "
                "в настройках подключения.")
    if code == 18456 or state == "28000":
        return "Не удалось войти: неверный логин или пароль либо у учётной записи нет доступа к серверу."
    if state == "IM002" or "data source name not found" in low:
        return NO_DRIVER_MESSAGE
    if code == 4060:
        return "База данных недоступна для этой учётной записи или не существует."
    if code in (229, 230, 262, 297, 300) or "permission was denied" in low:
        return "Недостаточно прав для выполнения операции."
    if is_cancelled(exc):
        return "Операция отменена."
    if is_timeout(exc):
        return "Превышено время ожидания ответа сервера."
    if is_connection_lost(exc):
        return "Соединение с сервером потеряно."
    if state == "08001" or any(s in low for s in (
            "could not open a connection", "network-related", "named pipes provider",
            "tcp provider", "server does not exist")):
        return ("Не удалось подключиться к серверу. Проверьте имя сервера/экземпляра, порт, "
                "работу службы SQL Server (и SQL Server Browser для именованных экземпляров) и брандмауэр.")
    short = redact(text or exc.__class__.__name__)
    short = re.sub(r"\[[^\]]*\]", "", short).strip() or exc.__class__.__name__
    return f"Ошибка SQL Server: {short[:300]}"
