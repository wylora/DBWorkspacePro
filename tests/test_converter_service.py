import tempfile
import unittest
from pathlib import Path

from bootstrap import create_services
from converter.engine import Job
from converter.service import ConverterService, default_output_dir
from security.credentials import InMemoryCredentialStore
from utils.tasks import CancelToken


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.svc = create_services(self.root / "home", InMemoryCredentialStore())
        self.cs = ConverterService(self.svc)
        self.src = self.root / "папка с пробелами"
        self.src.mkdir()
        self.out = self.root / "out"
        for i in range(3):
            (self.src / f"док {i}.md").write_text(f"# Док {i}", encoding="utf-8")

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def jobs(self):
        return [Job(p) for p in sorted(self.src.glob("*.md"))] + [Job(self.root / "нет.md")]

    def test_options_follow_settings(self):
        s = self.svc.settings
        o = self.cs.options()
        self.assertEqual((o.out_dir, o.policy, o.same_dir, o.timeout), (default_output_dir(), "new_version", False, 300.0))
        s.set("convert_output_dir", str(self.out))
        s.set("convert_policy", "skip")
        s.set("convert_same_dir", True)
        s.set("convert_timeout", 60)
        s.set("export_csv_delimiter", ",")
        o = self.cs.options()
        self.assertEqual((o.out_dir, o.policy, o.same_dir, o.timeout, o.csv_delimiter), (self.out, "skip", True, 60.0, ","))
        self.assertEqual(self.cs.options(policy="overwrite").policy, "overwrite")
        self.assertEqual(o.temp_root, self.svc.paths.temp)

    def test_settings_validation(self):
        for key, bad in (("convert_policy", "x"), ("convert_workers", 0), ("convert_workers", 9), ("convert_timeout", 5),
                         ("convert_same_dir", "yes")):
            with self.assertRaises(ValueError, msg=key):
                self.svc.settings.set(key, bad)

    def test_run_logs_each_file_and_summary(self):
        opts = self.cs.options(out_dir=self.out)
        summary = self.cs.run(self.jobs(), "html", opts)
        self.assertEqual((summary.ok, summary.failed), (3, 1))
        entries = self.svc.oplog.recent(10, "conversion")
        self.assertEqual(len(entries), 5)                                  # 4 файла + итог пакета
        self.assertTrue(entries[0].action.startswith("Пакет:"))
        self.assertEqual(entries[0].status, "warning")
        self.assertIn("успешно 3, ошибок 1", entries[0].message)
        statuses = sorted(e.status for e in entries[1:])
        self.assertEqual(statuses, ["error", "ok", "ok", "ok"])

    def test_recent_parses_paths_with_spaces_and_skips_batches(self):
        self.cs.run(self.jobs(), "html", self.cs.options(out_dir=self.out))
        recent = self.cs.recent()
        self.assertEqual(len(recent), 4)
        ok = [r for r in recent if r.status == "ok"]
        self.assertEqual(len(ok), 3)
        self.assertTrue(all(Path(r.files[0]).exists() for r in ok))
        self.assertTrue(any("док 0.html" in r.files[0] for r in ok))
        failed = next(r for r in recent if r.status == "error")
        self.assertIn("не найден", failed.message)
        self.assertEqual(failed.files, [])

    def test_cancelled_and_clean_batches_have_right_status(self):
        tok = CancelToken()
        summary = self.cs.run(self.jobs()[:3], "html", self.cs.options(out_dir=self.out), cancel_token=tok,
                              on_result=lambda i, r: tok.cancel() if i == 0 else None)
        self.assertEqual((summary.ok, summary.cancelled), (1, 2))
        self.assertEqual(self.svc.oplog.recent(1, "conversion")[0].status, "warning")
        clean = self.cs.run(self.jobs()[:2], "txt", self.cs.options(out_dir=self.out / "c"))
        self.assertEqual(clean.ok, 2)
        self.assertEqual(self.svc.oplog.recent(1, "conversion")[0].status, "ok")

    def test_workers_setting_is_used_and_caps_follow_path(self):
        self.svc.settings.set("convert_workers", 3)
        self.assertEqual(self.cs.workers(), 3)
        lo_dir = self.root / "LO" / "program"
        lo_dir.mkdir(parents=True)
        (lo_dir / "soffice").write_bytes(b"x")
        self.svc.settings.set("libreoffice_path", str(self.root / "LO"))       # указана папка установки
        self.assertEqual(self.cs.caps().soffice, str(lo_dir / "soffice"))
        from converter.libreoffice import find_soffice, locate_soffice
        self.svc.settings.set("libreoffice_path", str(self.root / "nope"))      # неверный путь не отключает поиск
        self.assertEqual(self.cs.caps().soffice, find_soffice(""))
        self.assertIn("не подходит", locate_soffice(str(self.root / "nope")).note)


if __name__ == "__main__":
    unittest.main()
