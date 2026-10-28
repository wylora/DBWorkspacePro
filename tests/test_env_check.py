import tempfile
import unittest
from pathlib import Path

from utils.env_check import FAIL, OK, format_report, has_failures, run_checks


class EnvCheckTests(unittest.TestCase):
    def test_runs_and_reports_everything(self):
        with tempfile.TemporaryDirectory() as d:
            results = run_checks(Path(d))
        names = {r.name for r in results}
        for expected in ("Python", "PySide6", "pyodbc", "keyring", "Папка данных", "LibreOffice"):
            self.assertIn(expected, names)
        self.assertEqual(next(r for r in results if r.name == "Папка данных").status, OK)
        self.assertIn("[", format_report(results))

    def test_missing_required_is_failure(self):
        from utils.env_check import CheckResult
        self.assertTrue(has_failures([CheckResult("x", FAIL, "")]))
        self.assertFalse(has_failures([CheckResult("x", OK, "")]))


if __name__ == "__main__":
    unittest.main()
