"""DB Workspace Pro — точка входа."""
from __future__ import annotations

import logging
import sys


def main() -> int:
    from automation.cli import CLI_FLAGS
    if any(a.split("=", 1)[0] in CLI_FLAGS for a in sys.argv[1:]):
        from automation.cli import main as cli_main          # без окна: задачи, настройки, диагностика
        return cli_main(sys.argv[1:])
    try:
        from PySide6.QtCore import QObject, Qt, Signal
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("Не установлен PySide6. Выполните: pip install -r requirements.txt\n"
              "Проверка окружения: python scripts/check_env.py", file=sys.stderr)
        return 2

    from bootstrap import create_services
    from utils.locks import ProcessLock
    from utils.logging_setup import install_excepthook, setup_logging
    from utils.paths import AppPaths

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName("DB Workspace Pro")
    app.setOrganizationName("DBWorkspacePro")

    instance = ProcessLock(AppPaths().ensure().data / "app.lock")      # одна копия окна на пользователя
    if not instance.try_acquire():
        QMessageBox.information(None, "DB Workspace Pro", "Программа уже запущена. Переключитесь на открытое окно "
                                "(вторая копия выполняла бы задачи по расписанию повторно).")
        return 0

    try:
        services = create_services()
    except Exception as exc:  # noqa: BLE001
        QMessageBox.critical(None, "DB Workspace Pro", f"Не удалось запустить приложение:\n{exc}")
        return 1
    setup_logging(services.paths.logs, services.settings.get("log_level"), console=sys.stderr is not None)
    log = logging.getLogger("dbworkspace")
    log.info("Запуск DB Workspace Pro, данные: %s", services.paths.home)

    class ErrorRelay(QObject):
        """Показывает сообщение об ошибке в GUI-потоке (хук может сработать в любом потоке)."""
        failed = Signal(str)

    relay = ErrorRelay()
    relay.failed.connect(lambda msg: QMessageBox.critical(
        None, "Непредвиденная ошибка",
        f"Произошла непредвиденная ошибка:\n{msg}\n\nПодробности записаны в журнал."))
    install_excepthook(lambda msg: relay.failed.emit(msg))

    from ui.main_window import MainWindow
    window = MainWindow(services)
    window.show()
    try:
        return app.exec()
    finally:
        instance.release()


if __name__ == "__main__":
    sys.exit(main())
