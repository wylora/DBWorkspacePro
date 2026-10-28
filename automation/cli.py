"""Режим без окна: задачи (для Планировщика Windows), настройки, диагностика.

    main.py --list-tasks
    main.py --run-task ID
    main.py --export-config ПУТЬ           (ПУТЬ — файл .zip или папка: имя файла будет с датой)
    main.py --import-config ФАЙЛ [--no-overwrite] [--sections settings,profiles,...]
    main.py --diagnostics ПУТЬ             (файл .zip или папка)
    main.py --paths
    main.py --find-libreoffice             (где программа ищет LibreOffice и что нашла)

Коды выхода: 0 — успешно, 1 — с замечаниями, 2 — ошибка, 3 — открыто окно программы (импорт невозможен).
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

CLI_FLAGS = ("--run-task", "--list-tasks", "--export-config", "--import-config", "--diagnostics", "--paths",
             "--find-libreoffice",
             "-h", "--help")


def _target(path: str, prefix: str) -> Path:
    p = Path(path)
    if p.is_dir() or path.endswith(("\\", "/")):
        return p / f"{prefix}_{dt.datetime.now():%Y%m%d_%H%M%S}.zip"
    return p


def main(argv: list[str] | None = None, home: str | None = None, credentials=None, connection_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="DBWorkspacePro", description="DB Workspace Pro — режим без окна")
    parser.add_argument("--run-task", metavar="ID", help="выполнить задачу автоматизации и выйти")
    parser.add_argument("--list-tasks", action="store_true", help="показать задачи автоматизации")
    parser.add_argument("--export-config", metavar="ПУТЬ", help="экспорт настроек (без паролей) в файл или папку")
    parser.add_argument("--import-config", metavar="ФАЙЛ", help="импорт настроек из файла")
    parser.add_argument("--no-overwrite", action="store_true", help="при импорте не заменять существующие объекты")
    parser.add_argument("--sections", metavar="СПИСОК", help="разделы через запятую: settings,profiles,favorites,"
                                                            "templates,scenarios,tasks")
    parser.add_argument("--diagnostics", metavar="ПУТЬ", help="собрать диагностический архив в файл или папку")
    parser.add_argument("--paths", action="store_true", help="показать папки данных и журналов")
    parser.add_argument("--find-libreoffice", action="store_true", help="найти LibreOffice и показать, где искали")
    args, _unknown = parser.parse_known_args(argv)

    from bootstrap import create_services
    from utils.logging_setup import setup_logging, teardown_logging
    from .service import STATUS_RU, AutomationService
    from .schedule import describe

    services = create_services(home, credentials, connection_factory)
    setup_logging(services.paths.logs, services.settings.get("log_level"), console=False)   # подробности — в файле журнала
    # у сборки без консоли (pythonw / .exe) нет stdout — вывод пишется в журнал cli-output.txt
    out = sys.stdout if sys.stdout is not None else open(services.paths.logs / "cli-output.txt", "a", encoding="utf-8")

    def say(text: str) -> None:
        print(text, file=out, flush=True)

    try:
        if args.find_libreoffice:
            from converter.libreoffice import locate_soffice
            lo = locate_soffice(services.settings.get("libreoffice_path"))
            say(f"LibreOffice: {lo.path} (найден: {lo.source})" if lo.path else "LibreOffice НЕ найден.")
            if lo.note:
                say(lo.note)
            say("Где искали:")
            for c in lo.checked:
                say("  " + c)
            return 0 if lo.path else 2
        if args.paths:
            say(f"Данные:  {services.paths.data}\nЖурналы: {services.paths.logs}")
            return 0
        if args.export_config:
            from storage.config_backup import export_config
            path = _target(args.export_config, "DBWorkspacePro_настройки")
            counts = export_config(services, path)
            say(f"Настройки сохранены: {path}")
            say("Содержимое: " + ", ".join(f"{k} — {v}" for k, v in counts.items()) + ". Пароли не включены.")
            return 0
        if args.import_config:
            from storage.config_backup import SECTIONS, ConfigBackupError, import_config
            from utils.locks import ProcessLock
            gui = ProcessLock(services.paths.data / "app.lock")
            if not gui.try_acquire():
                say("Окно программы открыто. Закройте программу и повторите импорт.")
                return 3
            try:
                sections = None
                if args.sections:
                    sections = {s.strip() for s in args.sections.split(",") if s.strip()}
                    unknown = sections - set(SECTIONS)
                    if unknown:
                        say("Неизвестные разделы: " + ", ".join(sorted(unknown)))
                        return 2
                rep = import_config(services, Path(args.import_config), sections, overwrite=not args.no_overwrite)
            except ConfigBackupError as exc:
                say(f"Ошибка: {exc}")
                return 2
            finally:
                gui.release()
            say(rep.summary())
            for w in rep.warnings:
                say("  • " + w)
            return 1 if rep.warnings else 0
        if args.diagnostics:
            from utils.diagnostics import collect_diagnostics
            path = collect_diagnostics(services, _target(args.diagnostics, "DBWorkspacePro_диагностика"))
            say(f"Диагностический архив: {path}")
            say("Пароли в нём замаскированы. Журналы могут содержать имена серверов — просмотрите архив перед отправкой.")
            return 0
        auto = AutomationService(services, background=False)
        if args.list_tasks:
            tasks = auto.tasks.list()
            if not tasks:
                say("Задач автоматизации нет.")
            for t in tasks:
                state = "вкл" if t.enabled else "выкл"
                say(f"{t.id}  [{state}]  {t.name}  ·  {describe(t.schedule)}  ·  последний: {t.last_status or '—'}")
            return 0
        if args.run_task:
            outcome = auto.execute_sync(args.run_task, trigger="windows")
            say(f"{STATUS_RU.get(outcome.status, outcome.status)}: {outcome.message}")
            return {"ok": 0, "warning": 1}.get(outcome.status, 2)
        parser.print_help(out)
        return 0
    except Exception as exc:  # noqa: BLE001 — код 2 и понятное сообщение вместо трассировки
        import logging
        logging.getLogger("dbworkspace.cli").exception("Сбой режима командной строки")
        say(f"Ошибка: {exc.__class__.__name__}: {exc}")
        say(f"Подробности в журнале: {services.paths.logs}")
        return 2
    finally:
        if out is not sys.stdout:
            out.close()
        services.shutdown()
        teardown_logging()                 # освобождает файл журнала (важно на Windows и при вызове из тестов)


if __name__ == "__main__":
    sys.exit(main())
