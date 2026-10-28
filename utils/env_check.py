"""Проверка окружения (используется scripts/check_env.py и страницей «Настройки»)."""
from __future__ import annotations

import importlib
import json
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"

REQUIRED = [("PySide6", "графический интерфейс"), ("pyodbc", "подключение к SQL Server"),
            ("keyring", "безопасное хранение паролей"), ("openpyxl", "экспорт в Excel"),
            ("docx", "отчёты Word (python-docx)"), ("matplotlib", "диаграммы в отчётах")]
# Библиотеки следующих этапов: их отсутствие сейчас не мешает работе.
LATER: list[tuple[str, str, int]] = []      # планировщик встроенный — APScheduler не нужен


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


def _find_libreoffice(custom: str = ""):
    from converter.libreoffice import locate_soffice
    return locate_soffice(custom)


def run_checks(app_home: Path | None = None) -> list[CheckResult]:
    res: list[CheckResult] = []
    v = sys.version_info
    res.append(CheckResult("Python", OK if v >= (3, 10) else FAIL,
                           f"{v.major}.{v.minor}.{v.micro}" + ("" if v >= (3, 10) else " — нужен 3.10+ (рекомендуется 3.11)")))
    res.append(CheckResult("SQLite", OK, sqlite3.sqlite_version))

    for mod, purpose in REQUIRED:
        try:
            m = importlib.import_module(mod)
            res.append(CheckResult(mod, OK, f"{getattr(m, '__version__', 'установлен')} — {purpose}"))
        except ImportError:
            res.append(CheckResult(mod, FAIL, f"не установлен ({purpose}): pip install {mod}"))
        except Exception as exc:  # noqa: BLE001
            res.append(CheckResult(mod, FAIL, f"ошибка импорта: {exc}"))

    try:
        import pyodbc
        drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
        if drivers:
            res.append(CheckResult("ODBC-драйверы SQL Server", OK, "; ".join(drivers)))
        else:
            res.append(CheckResult("ODBC-драйверы SQL Server", FAIL,
                                   "не найдены. Установите Microsoft ODBC Driver 18 for SQL Server"))
    except ImportError:
        res.append(CheckResult("ODBC-драйверы SQL Server", WARN, "pyodbc не установлен — проверка невозможна"))

    from security.credentials import KeyringCredentialStore
    ks = KeyringCredentialStore()
    if ks.is_available():
        res.append(CheckResult("Хранилище паролей", OK, ks.backend_name()))
    else:
        res.append(CheckResult("Хранилище паролей", WARN,
                               "системное хранилище недоступно — пароли будут жить только в памяти на время работы"))

    for mod, label, stage in LATER:
        try:
            importlib.import_module(mod)
            res.append(CheckResult(label, OK, f"установлен (нужен на этапе {stage})"))
        except ImportError:
            res.append(CheckResult(label, WARN, f"не установлен — понадобится на этапе {stage}"))

    custom = ""
    try:                                              # путь из настроек программы, если он задан
        import sqlite3 as _sqlite
        from utils.paths import AppPaths as _AP
        db = _AP(app_home).data / "app.sqlite3"
        if db.exists():
            with _sqlite.connect(f"file:{db}?mode=ro", uri=True) as con:
                row = con.execute("SELECT value FROM settings WHERE key = 'libreoffice_path'").fetchone()
                custom = (json.loads(row[0]) if row else "") or ""
    except Exception:  # noqa: BLE001
        custom = ""
    lo = _find_libreoffice(custom)
    if lo.path:
        res.append(CheckResult("LibreOffice", OK, f"{lo.path} (найден: {lo.source})" + (f". {lo.note}" if lo.note else "")))
    else:
        res.append(CheckResult("LibreOffice", WARN,
                               "не найден — без него недоступны PDF и преобразования DOC/ODT/RTF/XLS; остальное работает. "
                               "Установите с libreoffice.org или укажите путь в «Настройках» (можно папку LibreOffice). "
                               + (lo.note + " " if lo.note else "") + "Где искали: " + "; ".join(lo.checked)))

    from utils.paths import AppPaths
    try:
        paths = AppPaths(app_home).ensure()
        with tempfile.NamedTemporaryFile(dir=paths.data, delete=True):
            pass
        res.append(CheckResult("Папка данных", OK, str(paths.home)))
    except OSError as exc:
        res.append(CheckResult("Папка данных", FAIL, f"нет доступа на запись: {exc}"))
    return res


def has_failures(results: list[CheckResult]) -> bool:
    return any(r.status == FAIL for r in results)


def format_report(results: list[CheckResult]) -> str:
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}
    return "\n".join(f"{mark[r.status]} {r.name}: {r.detail}" for r in results)
