"""Диагностический архив для поддержки: проверка окружения, версии, настройки, последние ошибки, журналы.

Пароли маскируются. Список подключений и история запросов не включаются (только количества), но журналы и тексты
ошибок могут содержать имена серверов и баз данных — архив стоит просмотреть перед отправкой.
"""
from __future__ import annotations

import datetime as dt
import json
import platform
import sys
import zipfile
from pathlib import Path

from bootstrap import AppServices
from version import APP_VERSION_TEXT
from utils.env_check import format_report, run_checks
from utils.logging_setup import redact

LOG_TAIL = 5 * 1048576


def _versions() -> dict[str, str]:
    out = {"python": sys.version.split()[0], "platform": platform.platform(), "app": APP_VERSION_TEXT}
    for mod in ("PySide6", "pyodbc", "keyring", "openpyxl", "docx", "matplotlib", "PIL", "lxml"):
        try:
            m = __import__(mod)
            out[mod] = getattr(m, "__version__", getattr(m, "version", "есть"))
        except Exception:  # noqa: BLE001
            out[mod] = "нет"
    return {k: str(v) for k, v in out.items()}


def collect_diagnostics(svc: AppServices, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    summary = {
        "created": dt.datetime.now().isoformat(timespec="seconds"), "versions": _versions(),
        "paths": {"data": str(svc.paths.data), "logs": str(svc.paths.logs)},
        "counts": {"profiles": len(svc.profiles.repo.list()), "favorites": len(svc.favorites.list()),
                   "errors_24h": svc.oplog.count_since(24, "error")},
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        z.writestr("environment.txt", format_report(run_checks(svc.paths.home)))
        z.writestr("settings.json", json.dumps(svc.settings.all(), ensure_ascii=False, indent=2, default=str))
        errors = [f"{r.ts} [{r.category}] {r.action}: {r.message}" for r in svc.oplog.recent(500) if r.status == "error"]
        z.writestr("recent_errors.txt", redact("\n".join(errors)))
        for log in sorted(svc.paths.logs.glob("*.log*"))[-5:]:
            data = log.read_bytes()[-LOG_TAIL:]
            z.writestr(f"logs/{log.name}", redact(data.decode("utf-8", errors="replace")))
    svc.oplog.add("diagnostics", "Диагностический архив", "ok", str(path))
    return path
