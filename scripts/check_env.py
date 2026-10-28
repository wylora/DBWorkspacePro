"""Проверка окружения: python scripts/check_env.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.env_check import format_report, has_failures, run_checks  # noqa: E402

if __name__ == "__main__":
    results = run_checks()
    print(format_report(results))
    failed = has_failures(results)
    print("\nИтог:", "есть критичные проблемы" if failed else "окружение готово к запуску")
    sys.exit(1 if failed else 0)
