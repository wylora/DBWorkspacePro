import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path

from automation.schedule import Schedule
from automation.scripts import Scenario, approve
from automation.service import AutomationService
from automation.store import ScheduledTask
from bootstrap import create_services
from database.profile import ConnectionProfile
from reports.model import Block, DataSource, ReportTemplate
from security.credentials import InMemoryCredentialStore
from storage.config_backup import ConfigBackupError, export_config, import_config, read_backup, reset_settings
from tests.fakes import FakeServer
from utils.diagnostics import collect_diagnostics
from utils.locks import ProcessLock
from utils.paths import resource_path, resource_root

ROOT = Path(__file__).resolve().parents[1]


class ProcessLockTests(unittest.TestCase):
    def test_lock_is_exclusive_between_processes_and_released_on_exit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.lock"
            child = textwrap.dedent(f"""
                import sys, time
                sys.path.insert(0, {str(ROOT)!r})
                from utils.locks import ProcessLock
                l = ProcessLock({str(path)!r})
                print("got" if l.try_acquire() else "busy", flush=True)
                time.sleep(float(sys.argv[1]))
            """)
            mine = ProcessLock(path)
            self.assertTrue(mine.try_acquire())
            out = subprocess.run([sys.executable, "-c", child, "0"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
            self.assertEqual(out.strip(), "busy")                       # другой процесс не получил блокировку
            mine.release()
            out = subprocess.run([sys.executable, "-c", child, "0"], capture_output=True, text=True, encoding="utf-8", timeout=30).stdout
            self.assertEqual(out.strip(), "got")
            p = subprocess.Popen([sys.executable, "-c", child, "3"], stdout=subprocess.PIPE, text=True, encoding="utf-8")
            self.assertEqual(p.stdout.readline().strip(), "got")
            self.assertFalse(ProcessLock(path).try_acquire())
            p.kill()                                                     # аварийное завершение — ОС снимает блокировку
            p.wait(10)
            p.stdout.close()
            again = ProcessLock(path)
            self.assertTrue(again.try_acquire())
            again.release()

    def test_reentrant_and_context_manager(self):
        with tempfile.TemporaryDirectory() as d:
            lock = ProcessLock(Path(d) / "a" / "b.lock")
            self.assertTrue(lock.try_acquire())
            self.assertTrue(lock.try_acquire())
            lock.release()
            lock.release()
            with ProcessLock(Path(d) / "c.lock") as held:
                self.assertTrue(held.held)
                with self.assertRaises(BlockingIOError):
                    with ProcessLock(Path(d) / "c.lock"):
                        pass


class TaskLockAcrossInstancesTests(unittest.TestCase):
    def test_same_task_cannot_run_in_two_service_instances(self):
        with tempfile.TemporaryDirectory() as d:
            server = FakeServer()
            svc = create_services(d, InMemoryCredentialStore(), server.make_connection)
            p = svc.profiles.save(ConnectionProfile.new("P", "srv"))
            a, b = AutomationService(svc, background=False), AutomationService(svc, background=False)
            t = ScheduledTask.new("H", "health", Schedule("daily"), {"profile_id": p.id})
            a.save_task(t)
            self.assertTrue(a._acquire(t.id))                           # «окно» выполняет задачу
            o = b.execute_sync(t.id, "windows")                         # «Планировщик Windows» — в это же время
            self.assertEqual(o.status, "failed")
            self.assertIn("уже выполняется", o.message)
            self.assertEqual(b.runs.recent(t.id)[0].status, "skipped")
            a._release(t.id)
            self.assertEqual(b.execute_sync(t.id, "windows").status, "ok")
            svc.shutdown()


class ConfigBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.creds = InMemoryCredentialStore()
        self.creds.persistent = True                    # как системное хранилище Windows: пароль реально сохраняется
        self.a = create_services(self.root / "a", self.creds)
        self.b = create_services(self.root / "b", InMemoryCredentialStore())

    def tearDown(self):
        self.a.shutdown()
        self.b.shutdown()
        self.tmp.cleanup()

    def populate(self):
        s = self.a
        s.settings.set("editor_font_size", 15)
        s.settings.set("export_csv_delimiter", ",")
        p = ConnectionProfile.new("Prod", "srv")
        p.auth, p.username, p.save_password = "sql", "app_user", True
        p = s.profiles.save(p, password="S3cr3t!pass", remember=True)
        s.favorites.add("Топ", "SELECT TOP 10 * FROM t")
        from reports.service import ReportService
        ReportService(s).templates.save(ReportTemplate(id="my-t", name="Мой", sources=[DataSource(id="x", kind="sql", sql="SELECT 1")],
                                                      blocks=[Block(id="b", type="table", source="x")]))
        auto = AutomationService(s, background=False)
        scn = Scenario.new("Чистка", "DELETE FROM log WHERE d < GETDATE() - 30")
        scn.profile_id, scn.allow_modifications = p.id, True
        approve(scn)
        auto.scenarios.save(scn)
        t = ScheduledTask.new("Ночной бэкап", "backup", Schedule("daily", time="02:00"),
                              {"profile_id": p.id, "databases": ["Sales"], "folder": "D:\\bak"})
        auto.save_task(t)
        return p, scn, t

    def test_export_contains_no_passwords(self):
        p, _scn, _t = self.populate()
        self.assertEqual(self.a.profiles.stored_password(p), "S3cr3t!pass")   # пароль есть — но только в хранилище
        path = self.root / "cfg.zip"
        counts = export_config(self.a, path)
        self.assertEqual((counts["profiles"], counts["favorites"], counts["templates"], counts["scenarios"], counts["tasks"]),
                         (1, 1, 1, 1, 1))
        with zipfile.ZipFile(path) as z:
            blob = b"".join(z.read(n) for n in z.namelist())
        self.assertNotIn(b"S3cr3t", blob)
        self.assertNotIn("S3cr3t".encode("utf-16-le"), blob)
        manifest, data = read_backup(path)
        self.assertEqual(manifest["format_version"], 1)
        self.assertNotIn("password", json.dumps(data["profiles"]).lower().replace("save_password", ""))

    def test_import_restores_safely(self):
        p, scn, t = self.populate()
        path = self.root / "cfg.zip"
        export_config(self.a, path)
        rep = import_config(self.b, path)
        b = self.b
        self.assertEqual(b.settings.get("editor_font_size"), 15)
        self.assertEqual(b.profiles.repo.get(p.id).username, "app_user")
        self.assertEqual([f.title for f in b.favorites.list()], ["Топ"])
        auto = AutomationService(b, background=False)
        got_scn = auto.scenarios.get(scn.id)
        self.assertTrue(got_scn.allow_modifications)
        self.assertFalse(got_scn.is_approved)                            # подтверждение снято
        got_t = auto.tasks.get(t.id)
        self.assertFalse(got_t.enabled)                                   # задача выключена
        text = "\n".join(rep.warnings)
        self.assertIn("пароль не переносится", text)
        self.assertIn("подтверждение снято", text)
        self.assertIn("выключенными", text)
        self.assertIn("Подключения (без паролей): добавлено 1", rep.summary())
        rep2 = import_config(b, path, overwrite=False)                   # повторный импорт без замены
        self.assertEqual(rep2.added.get("profiles", 0), 0)
        self.assertEqual(rep2.skipped.get("profiles"), 1)
        self.assertEqual(rep2.skipped.get("favorites"), 1)               # дубликаты избранного не множатся

    def test_selective_import_and_bad_values(self):
        self.populate()
        path = self.root / "cfg.zip"
        export_config(self.a, path, {"settings", "favorites"})
        with zipfile.ZipFile(path) as z:
            entries = {n: z.read(n) for n in z.namelist() if n != "settings.json"}
        with zipfile.ZipFile(path, "w") as z:                            # пересобираем без дубликата имени
            for n, data in entries.items():
                z.writestr(n, data)
            z.writestr("settings.json", json.dumps({"editor_font_size": 999, "unknown_key": 1, "export_csv_delimiter": ","}))
        rep = import_config(self.b, path, {"settings"})
        self.assertEqual(self.b.favorites.list(), [])
        self.assertEqual(self.b.settings.get("export_csv_delimiter"), ",")
        self.assertNotEqual(self.b.settings.get("editor_font_size"), 999)
        self.assertEqual(rep.skipped["settings"], 2)

    def test_rejects_bad_files(self):
        cases = {"nozip.zip": b"not a zip", "empty.zip": None}
        for name, data in cases.items():
            p = self.root / name
            if data is None:
                with zipfile.ZipFile(p, "w") as z:
                    z.writestr("other.txt", "x")
            else:
                p.write_bytes(data)
            with self.assertRaises(ConfigBackupError):
                read_backup(p)
        with self.assertRaises(ConfigBackupError) as ctx:
            read_backup(self.root / "missing.zip")
        self.assertIn("не найден", str(ctx.exception))
        newer = self.root / "newer.zip"
        with zipfile.ZipFile(newer, "w") as z:
            z.writestr("manifest.json", json.dumps({"format": "dbworkspacepro-config", "format_version": 99}))
        with self.assertRaises(ConfigBackupError) as ctx:
            read_backup(newer)
        self.assertIn("более новой версией", str(ctx.exception))

    def test_reset_settings(self):
        self.a.settings.set("editor_font_size", 18)
        self.assertGreaterEqual(reset_settings(self.a), 1)
        from storage.settings import SPECS
        self.assertEqual(self.a.settings.get("editor_font_size"), SPECS["editor_font_size"].default)


class DiagnosticsTests(unittest.TestCase):
    def test_archive_masks_passwords_and_omits_profile_list(self):
        with tempfile.TemporaryDirectory() as d:
            svc = create_services(Path(d) / "h", InMemoryCredentialStore())
            p = ConnectionProfile.new("Secret prod", "very-secret-server.corp")
            svc.profiles.save(p)
            svc.oplog.add("query", "x", "error", "Login failed PWD=hunter2; Server=very-secret-server.corp")
            import logging
            from utils.logging_setup import setup_logging
            setup_logging(svc.paths.logs, "INFO", console=False)
            logging.getLogger("dbworkspace").error("connect failed Password=hunter2")
            for h in logging.getLogger().handlers:
                h.flush()
            out = collect_diagnostics(svc, Path(d) / "diag.zip")
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                blob = "\n".join(z.read(n).decode("utf-8", "replace") for n in names)
                summary = json.loads(z.read("summary.json"))
            self.assertNotIn("very-secret-server", json.dumps(summary))
            for h in list(logging.getLogger().handlers):
                logging.getLogger().removeHandler(h)
                h.close()
            svc.shutdown()
        self.assertIn("environment.txt", names)
        self.assertIn("summary.json", names)
        self.assertNotIn("hunter2", blob)                                  # пароли замаскированы везде
        self.assertTrue(any(n.startswith("logs/") for n in names))
        self.assertNotIn("Secret prod", blob)                              # список подключений не выгружается
        self.assertIn('"profiles": 1', blob)


class PackagingTests(unittest.TestCase):
    def test_resources_and_spec(self):
        self.assertEqual(resource_root(), ROOT)
        self.assertTrue(resource_path("examples", "report_templates").is_dir())
        for f in ("docs/user_guide.md", "docs/admin_guide.md", "docs/CHANGELOG.md", "packaging/app.ico",
                  "packaging/DBWorkspacePro.spec", "packaging/build.ps1", "packaging/make_version_info.py"):
            self.assertTrue((ROOT / f).is_file(), f)
        import ast
        spec = (ROOT / "packaging" / "DBWorkspacePro.spec").read_text(encoding="utf-8")
        ast.parse(spec)
        for needed in ("examples", "docs", "app.ico", "keyring.backends.Windows", "console=False"):
            self.assertIn(needed, spec)

    def test_frozen_resource_root(self):
        sys._MEIPASS = "/tmp/frozen-app"
        try:
            self.assertEqual(resource_path("docs", "a.md"), Path("/tmp/frozen-app/docs/a.md"))
        finally:
            del sys._MEIPASS

    def test_version_info_generation(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("mvi", ROOT / "packaging" / "make_version_info.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        text = mod.TEMPLATE.format(v="0, 7, 0, 0", s="0.7.0")
        self.assertIn("filevers=(0, 7, 0, 0)", text)
        self.assertIn("ProductVersion', '0.7.0'", text)

    def test_cli_without_console_writes_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            code = subprocess.run(
                [sys.executable, "-c", "import sys; sys.stdout = None; sys.path.insert(0, %r); "
                                       "from automation.cli import main; "
                                       "from security.credentials import InMemoryCredentialStore; "
                                       "raise SystemExit(main(['--list-tasks'], home=%r, credentials=InMemoryCredentialStore()))"
                 % (str(ROOT), d)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
            self.assertEqual(code.returncode, 0, code.stderr)
            self.assertTrue((Path(d) / "logs" / "cli-output.txt").exists())

    def test_help_guides_render(self):
        from converter.docmodel import parse_markdown, to_html
        for name in ("user_guide.md", "admin_guide.md", "CHANGELOG.md"):
            html = to_html(parse_markdown((ROOT / "docs" / name).read_text(encoding="utf-8")).blocks)
            self.assertIn("<h1>", html)
            self.assertNotIn("<script", html)


if __name__ == "__main__":
    unittest.main()
