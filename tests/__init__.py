"""Тесты DB Workspace Pro."""
import logging

# Без обработчиков Python печатает предупреждения и ошибки журнала прямо в консоль (logging.lastResort),
# причём без маскирования паролей. Ожидаемые ошибки, которые тесты вызывают специально, в консоль не нужны.
logging.getLogger().addHandler(logging.NullHandler())
