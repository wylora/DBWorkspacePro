"""Журналирование с маскированием секретов."""
from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

# Значение может быть в {фигурных скобках} (ODBC, '}}' — экранированная скобка),
# в кавычках или простым словом до ';', '&', пробела или запятой.
_SECRET_RE = re.compile(
    r"(?i)\b(pwd|password|passwd|token|secret|api[_-]?key|access[_-]?key)\b"
    r"(\s*[=:]\s*)"
    r"(\{(?:[^}]|\}\})*\}|\"[^\"]*\"|'[^']*'|[^;&\s,]+)"
)

_HANDLER_FLAG = "_dbwp_handler"


def redact(text: str) -> str:
    """Заменяет значения паролей/токенов на ***."""
    if not text:
        return text
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


class RedactingFormatter(logging.Formatter):
    """Форматтер, который маскирует секреты и в сообщении, и в трассировке."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class _RedactingLastResort(logging.Handler):
    """Замена logging.lastResort: печатает в текущий sys.stderr, но с маскированием секретов.

    Срабатывает, когда журнал ещё не настроен (например, до setup_logging или в тестах).
    """

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.setFormatter(RedactingFormatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr
        if stream is None:
            return
        try:
            stream.write(self.format(record) + "\n")
        except Exception:  # noqa: BLE001
            self.handleError(record)


logging.lastResort = _RedactingLastResort()


def setup_logging(log_dir: str | Path, level: str = "INFO", console: bool = True) -> logging.Logger:
    """Настраивает корневой логгер приложения. Повторный вызов безопасен."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
            h.close()

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = RedactingFormatter("%(asctime)s %(levelname)-8s %(threadName)s %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        log_dir / "dbworkspace.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    setattr(file_handler, _HANDLER_FLAG, True)
    root.addHandler(file_handler)

    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        setattr(sh, _HANDLER_FLAG, True)
        root.addHandler(sh)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    return logging.getLogger("dbworkspace")


def teardown_logging() -> None:
    """Снимает и закрывает обработчики, добавленные setup_logging (файл журнала освобождается)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, _HANDLER_FLAG, False):
            root.removeHandler(h)
            h.close()


def install_excepthook(on_error: Callable[[str], None] | None = None) -> None:
    """Логирует необработанные исключения (главного и фоновых потоков)."""
    log = logging.getLogger("dbworkspace.unhandled")

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Необработанное исключение", exc_info=(exc_type, exc, tb))
        if on_error:
            try:
                on_error(redact(f"{exc_type.__name__}: {exc}"))
            except Exception:  # noqa: BLE001
                pass

    sys.excepthook = _hook

    def _thread_hook(args):
        _hook(args.exc_type, args.exc_value, args.exc_traceback)

    threading.excepthook = _thread_hook
