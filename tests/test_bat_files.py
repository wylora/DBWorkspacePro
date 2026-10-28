"""Статическая проверка батников (cmd.exe в среде разработки нет) и тесты команд, которые они вызывают."""
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BATS = sorted((ROOT / "bat").glob("*.bat")) + [ROOT / "menu.bat"]
USER_BATS = [b for b in BATS if b.name not in ("_env.bat", "menu.bat")]


def lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8").split("\r\n")


class BatFileTests(unittest.TestCase):
    def test_encoding_and_line_endings(self):
        self.assertGreaterEqual(len(BATS), 18)
        for b in BATS:
            raw = b.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), f"{b.name}: BOM ломает первую строку @echo off")
            self.assertNotIn(b"\n", raw.replace(b"\r\n", b""), f"{b.name}: перевод строки без CR")
            self.assertTrue(raw.startswith(b"@echo off"), b.name)
            raw.decode("utf-8")

    def test_codepage_and_setlocal(self):
        for b in BATS:
            if b.name == "_env.bat":
                continue
            ls = lines(b)
            self.assertEqual(ls[1], "chcp 65001 >nul", b.name)
            self.assertEqual(ls[2], "setlocal", b.name)

    def test_labels_exist(self):
        for b in BATS:
            ls = lines(b)
            labels = {l.strip()[1:].lower() for l in ls if l.strip().startswith(":") and not l.strip().startswith("::")}
            for l in ls:
                for target in re.findall(r"\b(?:goto|call)\s+:?([A-Za-z_][\w]*)", l, re.I):
                    if target.lower() == "eof" or "call" in l.lower() and '"' in l.split(target)[0][-3:]:
                        continue
                    if re.search(r"\bcall\s+\"", l, re.I):
                        continue
                    self.assertIn(target.lower(), labels, f"{b.name}: нет метки :{target}")

    def test_no_fragile_constructs(self):
        for b in BATS:
            for n, l in enumerate(lines(b), 1):
                s = l.strip()
                self.assertFalse(s.endswith("(") or s.startswith(")"), f"{b.name}:{n}: многострочный блок в скобках")
                self.assertIsNone(re.match(r"^exit(\s*$|\s+(?!/b))", s, re.I), f"{b.name}:{n}: exit без /b закроет окно")
                if s.lower().startswith("echo ") or s.lower().startswith("if ") and " echo " in s.lower():
                    text = s.split("echo", 1)[1]
                    self.assertIsNone(re.search(r"[&|<>]", text), f"{b.name}:{n}: спецсимвол в echo: {s}")
                self.assertNotIn("!", s.replace("%", ""), f"{b.name}:{n}: «!» опасен при отложенном раскрытии")

    def test_every_bat_checks_environment_or_is_standalone(self):
        for b in USER_BATS:
            text = b.read_text(encoding="utf-8")
            self.assertIn('call "%~dp0_env.bat"', text, b.name)
            if "%VENV_PY%" in text and "--no-check" not in text:
                self.assertIn("|| goto :fail_env", text, b.name)
                self.assertIn(":fail_env", text, b.name)

    def test_referenced_project_files_exist(self):
        for b in BATS:
            for rel in re.findall(r"%ROOT%\\([\w\\.-]+)", b.read_text(encoding="utf-8")):
                if rel.split("\\")[0] in (".venv", ".pytest_cache", "build", "dist") or rel.startswith("test-report"):
                    continue                                     # создаются при работе, в проекте их нет
                self.assertTrue((ROOT / rel.replace("\\", "/")).exists(), f"{b.name}: нет файла {rel}")

    def test_menu_covers_every_tool(self):
        menu = (ROOT / "menu.bat").read_text(encoding="utf-8")
        called = set(re.findall(r'"%B%\\(\w+\.bat)"', menu))
        self.assertEqual(called, {b.name for b in USER_BATS})
        self.assertEqual(len(re.findall(r'^if "%N%"=="(\d+)"', menu, re.M)), 17)

    def test_cli_flags_used_by_bats_exist(self):
        from automation.cli import CLI_FLAGS
        used = set()
        for b in BATS:
            used |= set(re.findall(r"main\.py\"\s+(--[\w-]+)", b.read_text(encoding="utf-8")))
        self.assertTrue(used)
        self.assertTrue(used <= set(CLI_FLAGS), used - set(CLI_FLAGS))

    def test_build_ps1_has_bom_and_checks_steps(self):
        raw = (ROOT / "packaging" / "build.ps1").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "PowerShell 5.1 без BOM исказит русский текст")
        text = raw.decode("utf-8-sig")
        self.assertIn("LASTEXITCODE", text)
        self.assertIn("run_tests.py", text)
        self.assertIn("SkipTests", text)
        self.assertTrue(text.lstrip("\ufeff").splitlines()[2].startswith("param("), "param() должен идти первой командой")
        self.assertIn("py -3 -m venv", text)


class WatchdogRunnerTests(unittest.TestCase):
    def run_runner(self, d, *extra, timeout_s="2"):
        env = {**__import__("os").environ, "DBWP_TEST_TIMEOUT": timeout_s, "PYTHONUTF8": "1",
               "DBWP_TEST_PASSWORD": "Секрет-в-окружении"}
        report = Path(d) / "report.txt"
        r = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_tests.py"), "--dir", d, "--report", str(report),
                            *extra], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, env=env)
        return r, report.read_text(encoding="utf-8")

    def test_hanging_test_is_interrupted_with_stack_in_console_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "test_hang.py").write_text(textwrap.dedent("""
                import time, unittest
                class Hang(unittest.TestCase):
                    def test_a_ok(self):
                        print("шумный вывод успешного теста")
                    def test_b_waits_for_dialog(self):
                        time.sleep(60)
            """), encoding="utf-8")
            r, report = self.run_runner(d)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("test_b_waits_for_dialog", r.stderr)              # видно, где зависло — в консоли
            self.assertIn("прогон прерван", report)                         # и в отчёте
            self.assertIn("test_b_waits_for_dialog", report.split("прогон прерван")[1])
            self.assertIn("test_hang.Hang.test_a_ok ... ok", report)
            self.assertNotIn("шумный вывод", r.stdout + r.stderr)          # вывод успешных тестов скрыт
            self.assertNotIn("Секрет-в-окружении", report)                 # значения окружения в отчёт не попадают

    def test_report_lists_results_and_failures(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "test_mix.py").write_text(textwrap.dedent("""
                import unittest
                class Mix(unittest.TestCase):
                    def test_ok(self):
                        pass
                    def test_fail(self):
                        print("вывод упавшего теста")
                        self.assertEqual(1, 2)
                    @unittest.skip("нет сервера")
                    def test_skip(self):
                        pass
            """), encoding="utf-8")
            r, report = self.run_runner(d, timeout_s="60")
            self.assertEqual(r.returncode, 1)
            self.assertIn("Тестов: 3", r.stdout)
            self.assertIn("test_mix.Mix.test_ok ... ok", report)
            self.assertIn("test_mix.Mix.test_skip ... пропущен (нет сервера)", report)
            self.assertIn("НЕ ПРОЙДЕН: test_mix.Mix.test_fail", report)
            self.assertIn("вывод упавшего теста", report)                  # перехваченный вывод — в отчёте
            self.assertIn("Итог: тестов 3", report)
            self.assertIn("Python ", report)

    def test_runner_options_used_by_bats_are_supported(self):
        runner = (ROOT / "scripts" / "run_tests.py").read_text(encoding="utf-8")
        texts = [b.read_text(encoding="utf-8") for b in BATS] + [(ROOT / "packaging" / "build.ps1").read_text(encoding="utf-8-sig")]
        used = set()
        for t in texts:
            for line in t.splitlines():
                if "run_tests.py" in line:
                    used |= set(re.findall(r"(--[a-z-]+)", line.split("run_tests.py", 1)[1]))
        self.assertTrue(used)
        for opt in used:
            self.assertIn(f'"{opt}"', runner, f"run_tests.py не поддерживает {opt}")


class LastResortLoggingTests(unittest.TestCase):
    def test_unconfigured_logging_still_masks_passwords(self):
        import io
        import logging
        import utils.logging_setup  # noqa: F401 — заменяет logging.lastResort
        log = logging.getLogger("dbwp.test.lastresort")
        log.propagate = False                                               # нет обработчиков — сработает lastResort
        saved = sys.stderr
        sys.stderr = buf = io.StringIO()
        try:
            try:
                raise RuntimeError("fail PWD=abc123; Password=qwerty")
            except RuntimeError:
                log.exception("Задача завершилась ошибкой")
        finally:
            sys.stderr = saved
        out = buf.getvalue()
        self.assertIn("Задача завершилась ошибкой", out)
        self.assertIn("PWD=***", out)
        self.assertNotIn("abc123", out)
        self.assertNotIn("qwerty", out)


class CliCommandTests(unittest.TestCase):
    def run_cli(self, home, *args):
        return subprocess.run([sys.executable, str(ROOT / "main.py"), *args], capture_output=True, text=True, timeout=120,
                              encoding="utf-8", errors="replace",
                              env={**__import__("os").environ, "DBWP_HOME": str(home), "PYTHONUTF8": "1"})

    def test_paths_export_import_diagnostics(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            r = self.run_cli(d / "a", "--paths")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn(str(d / "a" / "data"), r.stdout)
            out = d / "export"
            out.mkdir()
            r = self.run_cli(d / "a", "--export-config", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            files = list(out.glob("DBWorkspacePro_настройки_*.zip"))
            self.assertEqual(len(files), 1)
            r = self.run_cli(d / "b", "--import-config", str(files[0]), "--no-overwrite")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = self.run_cli(d / "b", "--import-config", str(files[0]), "--sections", "settings,bogus")
            self.assertEqual(r.returncode, 2)
            r = self.run_cli(d / "b", "--import-config", str(d / "missing.zip"))
            self.assertEqual(r.returncode, 2)
            r = self.run_cli(d / "a", "--diagnostics", str(out))
            self.assertEqual(r.returncode, 0, r.stderr)
            diag = list(out.glob("DBWorkspacePro_диагностика_*.zip"))
            self.assertEqual(len(diag), 1)
            with zipfile.ZipFile(diag[0]) as z:
                self.assertIn("environment.txt", z.namelist())

    def test_import_refused_while_window_is_open(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            out = d / "x"
            out.mkdir()
            self.run_cli(d / "a", "--export-config", str(out))
            cfg = next(out.glob("*.zip"))
            holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(ROOT)!r})
                from utils.locks import ProcessLock
                l = ProcessLock({str(d / 'b' / 'data' / 'app.lock')!r})
                print("held" if l.try_acquire() else "busy", flush=True)
                time.sleep(20)
            """)], stdout=subprocess.PIPE, text=True, encoding="utf-8")
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")      # «окно программы» открыто
                r = self.run_cli(d / "b", "--import-config", str(cfg))
                self.assertEqual(r.returncode, 3)
                self.assertIn("Закройте программу", r.stdout)
            finally:
                holder.kill()
                holder.wait(10)
                holder.stdout.close()
            r = self.run_cli(d / "b", "--import-config", str(cfg))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
