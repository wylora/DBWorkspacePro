"""Запуск тестов со сторожем и файлом отчёта.

    python scripts/run_tests.py                        все тесты, отчёт test-report.txt в корне проекта
    python scripts/run_tests.py --fast                 без полной проверки маршрутов конвертера
    python scripts/run_tests.py МОДУЛЬ …               выбранные модули (например, tests.integration.test_real_server)
    python scripts/run_tests.py --report ФАЙЛ          другой файл отчёта
    python scripts/run_tests.py --dir ПАПКА            тесты из другой папки

В консоли — краткий ход выполнения, в отчёте — версии компонентов, каждый тест с результатом и полные трассировки
ошибок. Если тест выполняется дольше DBWP_TEST_TIMEOUT секунд (по умолчанию 600), в консоль и в отчёт пишется стек
всех потоков и прогон прерывается — вместо бесконечного ожидания (например, открытого модального окна).
Значения переменных окружения (в том числе паролей) в отчёт не записываются.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import os
import platform
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Watchdog:
    """Поток-сторож: если тест не завершился вовремя — стек всех потоков в консоль и отчёт, затем выход."""

    def __init__(self, timeout: int, report):
        self.timeout, self.report = timeout, report
        self.deadline: float | None = None
        self.test_id = ""
        self.lock = threading.Lock()
        threading.Thread(target=self._loop, name="watchdog", daemon=True).start()

    def arm(self, test_id: str) -> None:
        with self.lock:
            self.test_id, self.deadline = test_id, time.monotonic() + self.timeout
        # запасной сторож на уровне интерпретатора (сработает, даже если потоки Python заблокированы)
        if sys.__stderr__ is not None:
            faulthandler.dump_traceback_later(self.timeout + 120, exit=True, file=sys.__stderr__)

    def disarm(self) -> None:
        with self.lock:
            self.deadline = None
        faulthandler.cancel_dump_traceback_later()

    def _loop(self) -> None:
        while True:
            time.sleep(1)
            with self.lock:
                expired = self.deadline is not None and time.monotonic() > self.deadline
                test_id = self.test_id
            if not expired:
                continue
            msg = (f"\n\n!!! Тест {test_id} выполняется дольше {self.timeout} с — прогон прерван.\n"
                   "Ниже стек всех потоков: по нему видно, где тест завис.\n")
            for stream in (sys.__stderr__, self.report):
                if stream is None:
                    continue
                try:
                    stream.write(msg)
                    stream.flush()
                    faulthandler.dump_traceback(file=stream, all_threads=True)
                    stream.flush()
                except Exception:  # noqa: BLE001
                    pass
            os._exit(1)


class ReportResult(unittest.TextTestResult):
    report = None
    watchdog: Watchdog | None = None

    def _line(self, text: str) -> None:
        if self.report is not None:
            self.report.write(text + "\n")
            self.report.flush()

    def startTest(self, test):  # noqa: N802
        if self.report is not None:
            self.report.write(f"{dt.datetime.now():%H:%M:%S} {test.id()} ... ")
            self.report.flush()
        if self.watchdog:
            self.watchdog.arm(test.id())
        super().startTest(test)

    def stopTest(self, test):  # noqa: N802
        if self.watchdog:
            self.watchdog.disarm()
        super().stopTest(test)

    def addSuccess(self, test):  # noqa: N802
        super().addSuccess(test)
        self._line("ok")

    def addError(self, test, err):  # noqa: N802
        super().addError(test, err)
        self._line("ОШИБКА")

    def addFailure(self, test, err):  # noqa: N802
        super().addFailure(test, err)
        self._line("НЕ ПРОЙДЕН")

    def addSkip(self, test, reason):  # noqa: N802
        super().addSkip(test, reason)
        self._line(f"пропущен ({reason})")

    def addExpectedFailure(self, test, err):  # noqa: N802
        super().addExpectedFailure(test, err)
        self._line("ожидаемая ошибка")

    def addUnexpectedSuccess(self, test):  # noqa: N802
        super().addUnexpectedSuccess(test)
        self._line("неожиданный успех")


def _versions() -> list[str]:
    out = [f"Python {sys.version.split()[0]} ({sys.executable})", f"Система: {platform.platform()}"]
    for mod in ("PySide6", "pyodbc", "keyring", "openpyxl", "docx", "matplotlib", "PIL"):
        try:
            m = __import__(mod)
            out.append(f"{mod}: {getattr(m, '__version__', 'установлен')}")
        except Exception:  # noqa: BLE001
            out.append(f"{mod}: нет" + (" (интерфейс проверяется заглушкой Qt)" if mod == "PySide6" else ""))
    try:
        from converter.libreoffice import find_soffice
        out.append(f"LibreOffice: {find_soffice() or 'не найден'}")
    except Exception:  # noqa: BLE001
        pass
    try:
        from version import APP_VERSION_TEXT
        out.insert(0, f"DB Workspace Pro {APP_VERSION_TEXT}")
    except Exception:  # noqa: BLE001
        pass
    flags = [k for k in ("DBWP_SKIP_SLOW", "DBWP_TEST_SERVER", "DBWP_TEST_BACKUP_FOLDER") if os.environ.get(k)]
    out.append("Режимы: " + (", ".join(flags) if flags else "обычный"))
    return out


def main(argv: list[str]) -> int:
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt":
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
    args = list(argv)
    if "--fast" in args:
        os.environ["DBWP_SKIP_SLOW"] = "1"
        args.remove("--fast")
    report_path = ROOT / "test-report.txt"
    if "--report" in args:
        i = args.index("--report")
        report_path = Path(args[i + 1])
        del args[i:i + 2]
    start_dir = None
    if "--dir" in args:
        i = args.index("--dir")
        start_dir = args[i + 1]
        del args[i:i + 2]
    names = [a for a in args if not a.startswith("--")]
    timeout = int(os.environ.get("DBWP_TEST_TIMEOUT", "600"))

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as report:
        report.write("Отчёт о тестах DB Workspace Pro\n" + f"Дата: {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        report.write("\n".join(_versions()) + "\n\n")
        report.flush()
        loader = unittest.defaultTestLoader
        if start_dir:
            suite = loader.discover(start_dir, top_level_dir=start_dir)
        elif names:
            suite = loader.loadTestsFromNames(names)
        else:
            suite = loader.discover("tests", top_level_dir=str(ROOT))
        print(f"Тестов: {suite.countTestCases()}. Зависший тест (дольше {timeout} с) прерывается с выводом стека.\n"
              f"Подробный отчёт: {report_path}", flush=True)
        ReportResult.report = report
        ReportResult.watchdog = Watchdog(timeout, report)
        started = time.monotonic()
        runner = unittest.TextTestRunner(verbosity=1, buffer=True, resultclass=ReportResult)
        result = runner.run(suite)
        report.write(f"\nИтог: тестов {result.testsRun}, ошибок {len(result.errors)}, не пройдено {len(result.failures)}, "
                     f"пропущено {len(result.skipped)}, время {time.monotonic() - started:.0f} с\n")
        for kind, items in (("ОШИБКА", result.errors), ("НЕ ПРОЙДЕН", result.failures)):
            for test, text in items:
                report.write(f"\n{'=' * 70}\n{kind}: {test.id()}\n{'-' * 70}\n{text}\n")
    print(f"\nПодробный отчёт: {report_path}", flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
