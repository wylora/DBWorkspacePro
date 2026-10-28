import tempfile
import unittest
from pathlib import Path

from bootstrap import create_services
from reports.generator import ReportCancelled, ReportError
from reports.model import Block, DataSource, ReportTemplate
from reports.service import ReportService
from security.credentials import InMemoryCredentialStore
from tests.fakes import FakeOdbcError, FakeServer
from utils.tasks import CancelToken


def template(name="Отчёт"):
    return ReportTemplate(name=name, title="T", sources=[DataSource(id="s", name="S", kind="sql", sql="SELECT a FROM t1")],
                          blocks=[Block(id="t", type="table", source="s", title="Таблица")])


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        self.server.when("FROM t1", columns=["a"], rows=[(1,), (2,)])
        self.svc = create_services(self.tmp.name, InMemoryCredentialStore(), self.server.make_connection)
        from database.profile import ConnectionProfile
        self.profile = self.svc.profiles.save(ConnectionProfile.new("Dev", "srv"))
        self.svc.profiles.connect(self.profile)
        self.rs = ReportService(self.svc)
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def request(self, **kw):
        return self.rs.build_request(template(), {}, ["docx"], self.profile.id, "", output_dir=self.out,
                                     base_name="my report (1)", **kw)

    def test_request_uses_settings_and_closes_session_connection(self):
        self.svc.settings.set("report_max_rows", 77)
        self.svc.settings.set("export_csv_delimiter", ",")
        req = self.request()
        self.assertEqual((req.max_rows, req.csv_delimiter, req.server_name), (77, ",", "srv"))
        before = self.svc.manager.count()
        res = self.rs.run(req)
        self.assertTrue(res.ok)
        self.assertEqual(self.svc.manager.count(), before)

    def test_journal_and_recent_with_spaces_in_paths(self):
        self.rs.run(self.request())
        self.rs.run(self.request(overwrite="new_version"))
        recent = self.rs.recent()
        self.assertEqual(len(recent), 2)
        self.assertTrue(recent[0].files[0].endswith("my report (1) (2).docx"))
        self.assertEqual(recent[0].name, "Отчёт")
        entry = self.svc.oplog.recent(1, "report")[0]
        self.assertEqual(entry.status, "ok")

    def test_failures_and_cancel_are_logged(self):
        self.server.handlers.insert(0, {"substr": "FROM t1", "columns": [], "rows": [], "sets": None, "messages": None,
                                        "on_execute": None, "errors": [FakeOdbcError("42000", "[42000] boom (208)")]})
        with self.assertRaises(ReportError):
            self.rs.run(self.request())
        self.assertEqual(self.svc.oplog.recent(1, "report")[0].status, "error")
        tok = CancelToken()
        tok.cancel()
        with self.assertRaises(ReportCancelled):
            self.rs.run(self.request(), cancel_token=tok)
        self.assertEqual(self.svc.oplog.recent(1, "report")[0].status, "warning")
        self.assertEqual(self.rs.recent(), [])                          # отменённые и неудачные в «недавние» не попадают

    def test_background_run_without_open_connection_and_deleted_profile(self):
        self.svc.profiles.disconnect(self.profile)                       # Windows-аутентификация: можно подключиться в фоне
        res = self.rs.run(self.request())
        self.assertTrue(res.ok)
        self.assertEqual(self.svc.manager.count(), 0)
        self.svc.profiles.delete(self.profile.id)
        with self.assertRaises(ReportError) as ctx:
            self.request()
        self.assertIn("не найден", str(ctx.exception))

    def test_formats_persist_and_validate(self):
        self.rs.remember_formats(["pdf", "xlsx", "bogus"])
        self.assertEqual(self.rs.saved_formats(), ["pdf", "xlsx"])
        self.svc.settings.set("report_formats", "garbage")
        self.assertEqual(self.rs.saved_formats(), ["docx"])
        for key, bad in (("report_overwrite", "x"), ("report_max_rows", 0), ("report_pdf_timeout", 5)):
            with self.assertRaises(ValueError):
                self.svc.settings.set(key, bad)

    def test_templates_live_in_app_data(self):
        t = template("Мой")
        t.id = "mine"
        path = self.rs.templates.save(t)
        self.assertTrue(str(path).startswith(str(self.svc.paths.data)))


if __name__ == "__main__":
    unittest.main()
