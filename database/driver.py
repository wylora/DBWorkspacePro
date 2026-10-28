"""Поиск ODBC-драйвера и ленивая загрузка pyodbc."""
from __future__ import annotations

import re
from typing import Iterable

from .errors import NO_DRIVER_MESSAGE, DriverNotAvailableError

_MODERN = re.compile(r"^ODBC Driver (\d+) for SQL Server$")
FALLBACK = "SQL Server Native Client 11.0"


def load_pyodbc():
    try:
        import pyodbc  # noqa: WPS433
        return pyodbc
    except ImportError as exc:
        raise DriverNotAvailableError(
            "Не установлена библиотека pyodbc. Выполните: pip install pyodbc") from exc


def installed_drivers() -> list[str]:
    return list(load_pyodbc().drivers())


def driver_major(name: str) -> int | None:
    m = _MODERN.match(name or "")
    return int(m.group(1)) if m else None


def choose_driver(preferred: str, installed: Iterable[str]) -> str | None:
    """Явно заданный драйвер — только он (без подмены). Иначе — самый новый ODBC Driver N."""
    installed = list(installed)
    if preferred:
        return preferred if preferred in installed else None
    modern = sorted((d for d in installed if driver_major(d) is not None), key=driver_major, reverse=True)
    if modern:
        return modern[0]
    if FALLBACK in installed:
        return FALLBACK
    return None


def missing_driver_message(preferred: str) -> str:
    if preferred:
        return f"Драйвер «{preferred}» не найден среди установленных. " + NO_DRIVER_MESSAGE
    return NO_DRIVER_MESSAGE
