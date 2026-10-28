import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from automation import backup as bk
from automation.jobs import Outcome, parse_table
from automation.schedule import Schedule
from automation.scripts import Scenario, approve, check_permission, run_scenario, sql_hash
from automation.service import AutomationService
from automation.store import ScheduledTask
from bootstrap import create_services
from database.profile import ConnectionProfile
from reports.model import Block, DataSource, ReportTemplate
from security.credentials import InMemoryCredentialStore
from tests.fakes import FakeOdbcError, FakeServer


def always(err):
    def raiser(cursor):
        raise err
    return raiser


DENIED = FakeOdbcError("42000", "[42000] The permission was denied (229)")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.server = FakeServer()
        self.svc = create_services(self.root / "home", InMemoryCredentialStore(), self.server.make_connection)
        self.profile = self.svc.profiles.save(ConnectionProfile.new("Prod", "srv"))
        self.auto = AutomationService(self.svc, background=False)

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def task(self, kind, config, sched=None, **kw):
        t = ScheduledTask.new(f"Задача {kind}", kind, sched or Schedule("daily", time="08:00"),
                              {"profile_id": self.profile.id, **config})
        for k, v in kw.items():
            setattr(t, k, v)
        errs = self.auto.save_task(t)
        self.assertEqual(errs, [], errs)
        return t

    def run_task(self, t) -> Outcome:
        return self.auto.execute_sync(t.id, "manual")

    def sql(self, substr):
        return [(s, p) for s, p in self.server.executed if substr in s]


class HealthAndStatsTests(Base):
    def test_health_ok_warning_failure_recovery(self):
        t = self.task("health", {"latency_warn_ms": 100000, "notify": "errors"})
        o = self.run_task(t)
        self.assertEqual(o.status, "ok", o.message)
        self.assertEqual(self.svc.manager.count(), 0)                    # временное соединение закрыто
        self.assertEqual([v for _t, v in self.auto.metrics.series(self.profile.id, "available")], [1.0])
        self.server.connect_errors.append(FakeOdbcError("08001", "TCP Provider: No such host is known."))
        o = self.run_task(t)
        self.assertEqual(o.status, "failed")
        self.assertIn("недоступен", o.message)
        self.assertEqual(self.auto.notes.recent()[0].level, "error")
        o = self.run_task(t)
        self.assertIn("снова доступен", o.message)
        self.assertEqual(self.auto.notes.recent()[0].level, "success")   # уведомление о восстановлении
        runs = self.auto.runs.recent(t.id)
        self.assertEqual([r.status for r in runs], ["ok", "failed", "ok"])
        self.assertEqual(self.auto.tasks.get(t.id).last_status, "ok")

    def test_slow_server_is_warning(self):
        t = self.task("health", {"latency_warn_ms": 1})
        import time as _t
        self.server.when("SELECT 1", columns=["c"], rows=[(1,)], on_execute=lambda c: _t.sleep(0.01))
        self.assertEqual(self.run_task(t).status, "warning")

    def test_table_stats_with_fallbacks(self):
        self.server.when("sys.master_files", on_execute=always(DENIED))
        self.server.when("sys.database_files", columns=["d", "l"], rows=[(10.0, 2.0)])
        self.server.when("COUNT(*) FROM [Sales].sys.tables", columns=["n"], rows=[(3,)])
        self.server.when("sys.allocation_units", on_execute=always(DENIED))
        self.server.when("SUM(p.rows)", columns=["s", "t", "r"], rows=[("dbo", "Orders", 1500), ("dbo", "Items", 20)])
        t = self.task("table_stats", {"database": "Sales", "top": 5})
        o = self.run_task(t)
        self.assertEqual(o.status, "ok", o.message)
        self.assertIn("таблиц 3", o.message)
        self.assertIn("размер 12.0 МБ", o.message)
        latest = self.auto.metrics.latest(self.profile.id, "table_rows")
        self.assertEqual(latest["Sales.dbo.Orders"][1], 1500.0)
        self.assertIn("только число строк", o.details)


class BackupTests(Base):
    def setUp(self):
        super().setUp()
        self.folder = self.root / "backups"                       # «папка сервера», доступная и с клиента
        self.folder.mkdir()

    def make_file_on_backup(self):
        def create(cursor):
            name = next(p for p in cursor.server.executed[-1][1] if str(p).endswith(".bak"))
            Path(name).write_bytes(b"x" * 2048)
        return create

    def test_backup_sql_verify_and_file_check(self):
        self.server.when("BACKUP DATABASE", messages=[("01000", "[01000] 10 percent processed.")],
                         on_execute=self.make_file_on_backup())
        t = self.task("backup", {"databases": ["Sales"], "folder": str(self.folder)})
        o = self.run_task(t)
        self.assertEqual(o.status, "ok", o.details)
        sql, params = self.sql("BACKUP DATABASE")[0]
        self.assertIn("COPY_ONLY", sql)
        self.assertIn("CHECKSUM", sql)
        self.assertNotIn("Sales", sql)                            # имя базы и путь — параметрами
        self.assertEqual(params[0], "Sales")
        self.assertTrue(str(params[1]).startswith(str(self.folder)))
        self.assertTrue(self.sql("RESTORE VERIFYONLY"))
        self.assertIn("проверена", o.details)
        self.assertIn("0.0 МБ", o.details)                       # размер прочитан из видимого файла

    def test_backup_failures_get_hints(self):
        for text, hint in (("Operating system error 5(Access is denied.)", "нет прав на запись"),
                           ("Operating system error 3(The system cannot find the path specified.)", "не существует на сервере"),
                           ("BACKUP DATABASE WITH COMPRESSION is not supported on Express Edition", "сжатие")):
            self.server.handlers.clear()
            self.server.when("BACKUP DATABASE", on_execute=always(FakeOdbcError("42000", f"[42000] {text} (3201)")))
            t = self.task("backup", {"databases": ["Sales"], "folder": "D:\\bak"})
            o = self.run_task(t)
            self.assertEqual(o.status, "failed")
            self.assertIn(hint, o.details)

    def test_verify_without_rights_is_warning_not_failure(self):
        self.server.when("BACKUP DATABASE", on_execute=self.make_file_on_backup())
        self.server.when("RESTORE VERIFYONLY", on_execute=always(DENIED))
        o = self.run_task(self.task("backup", {"databases": ["Sales"], "folder": str(self.folder)}))
        self.assertEqual(o.status, "warning")
        self.assertIn("не проверялась", o.details)
        self.assertIn("CREATE DATABASE", o.details)

    def test_cleanup_keeps_newest_and_ignores_foreign_files(self):
        names = [f"Sales_2026010{i}_120000.bak" for i in range(1, 6)] + ["Sales_manual.bak", "Other_20260101_120000.bak",
                                                                         "notes.txt"]
        for n in names:
            (self.folder / n).write_bytes(b"x")
        self.server.when("BACKUP DATABASE", on_execute=self.make_file_on_backup())
        t = self.task("backup", {"databases": ["Sales"], "folder": str(self.folder),
                                 "cleanup": {"enabled": True, "acknowledged": True, "keep_last": 2}})
        o = self.run_task(t)
        self.assertEqual(o.status, "ok", o.details)
        left = sorted(p.name for p in self.folder.iterdir())
        sales = [n for n in left if n.startswith("Sales_2")]
        self.assertEqual(len(sales), 2)                           # новая копия + одна предыдущая
        for keep in ("Sales_manual.bak", "Other_20260101_120000.bak", "notes.txt"):
            self.assertIn(keep, left)                             # чужие файлы не трогаются

    def test_cleanup_never_runs_after_failed_backup_or_without_consent(self):
        for n in ("Sales_20260101_120000.bak", "Sales_20260102_120000.bak", "Sales_20260103_120000.bak"):
            (self.folder / n).write_bytes(b"x")
        self.server.when("BACKUP DATABASE", on_execute=always(FakeOdbcError("42000", "[42000] disk full (3202)")))
        t = self.task("backup", {"databases": ["Sales"], "folder": str(self.folder),
                                 "cleanup": {"enabled": True, "acknowledged": True, "keep_last": 1}})
        self.assertEqual(self.run_task(t).status, "failed")
        self.assertEqual(len(list(self.folder.iterdir())), 3)
        bad = ScheduledTask.new("x", "backup", Schedule("daily"), {"profile_id": self.profile.id, "databases": ["Sales"],
                                                                   "folder": "D:\\b", "cleanup": {"enabled": True, "keep_last": 1}})
        self.assertTrue(any("Подтвердите удаление" in e for e in self.auto.validate(bad)))
        bad.config["cleanup"] = {"enabled": True, "acknowledged": True, "keep_last": 0}
        self.assertTrue(any("не меньше одной" in e for e in self.auto.validate(bad)))

    def test_plan_cleanup_age_rule(self):
        for n in ("DB_20260101_000000.bak", "DB_20260301_000000.bak", "DB_20260310_000000.bak"):
            (self.folder / n).write_bytes(b"x")
        plan = bk.plan_cleanup(self.folder, "DB", keep_last=1, older_than_days=30, now=dt.datetime(2026, 3, 15))
        self.assertEqual([p.name for p in plan], ["DB_20260101_000000.bak"])
        with self.assertRaises(bk.BackupError):
            bk.plan_cleanup(self.root / "nope", "DB", keep_last=1)

    def test_restore_as_new_is_protected(self):
        conn = self.server.make_connection(self.profile)
        conn.connect()
        self.server.when("RESTORE FILELISTONLY", columns=["LogicalName", "PhysicalName", "Type"],
                         rows=[("Sales", "C:\\d\\Sales.mdf", "D"), ("Sales_log", "C:\\d\\Sales_log.ldf", "L")])
        self.server.when("InstanceDefaultDataPath", columns=["d", "l"], rows=[("E:\\data\\", "F:\\log\\")])
        self.server.when("FROM sys.databases WHERE name = ?", columns=["n"], rows=[(0,)])
        with self.assertRaises(bk.BackupError):
            bk.restore_as_new(conn, "D:\\b.bak", "Sales_copy", "Sales_cop")          # подтверждение не совпало
        with self.assertRaises(bk.BackupError):
            bk.restore_as_new(conn, "D:\\b.bak", "master", "master")
        bk.restore_as_new(conn, "D:\\b.bak", "Sales_copy", "Sales_copy")
        sql, params = self.sql("RESTORE DATABASE")[0]
        self.assertNotIn("REPLACE", sql)
        self.assertEqual(list(params[:2]), ["Sales_copy", "D:\\b.bak"])
        self.assertIn("E:\\data\\Sales_copy_Sales.mdf", params)
        self.assertIn("F:\\log\\Sales_copy_Sales_log.ldf", params)
        self.server.handlers.insert(0, {"substr": "FROM sys.databases WHERE name = ?", "columns": ["n"], "rows": [(1,)],
                                        "errors": None, "on_execute": None, "sets": None, "messages": None})
        with self.assertRaises(bk.BackupError) as ctx:
            bk.restore_as_new(conn, "D:\\b.bak", "Sales", "Sales")
        self.assertIn("уже существует", str(ctx.exception))
        text = bk.restore_instructions("Sa]les", "D:\\it's.bak")
        self.assertIn("[Sa]]les]", text)
        self.assertIn("N'D:\\it''s.bak'", text)

    def test_history_from_msdb(self):
        conn = self.server.make_connection(self.profile)
        conn.connect()
        self.server.when("msdb.dbo.backupset", columns=list("abcdefghi"),
                         rows=[("Sales", None, dt.datetime(2026, 1, 1), "D", 1, 1048576, None, 1, "D:\\x.bak")])
        h = bk.history(conn, "Sales")
        self.assertEqual((h[0]["type"], h[0]["size"], h[0]["path"]), ("полная", 1048576, "D:\\x.bak"))
        self.server.handlers.clear()
        self.server.when("msdb.dbo.backupset", on_execute=always(DENIED))
        with self.assertRaises(bk.BackupError):
            bk.history(conn, "Sales")

    def test_tempdb_and_validation(self):
        self.assertTrue(bk.validate_options(bk.BackupOptions("tempdb", "D:\\b")))
        self.assertTrue(bk.validate_options(bk.BackupOptions("Sales", "")))
        self.assertTrue(bk.validate_options(bk.BackupOptions("Sales", "D:\\b|x")))
        self.assertEqual(bk.join_server_path("/var/opt/mssql/", "a.bak"), "/var/opt/mssql/a.bak")


class ExportAndReportTests(Base):
    def test_export_tables(self):
        self.server.when("FROM [Sales].[dbo].[Orders]", columns=["id", "total"], rows=[(1, 2.5), (2, 5.0)])
        self.server.when("FROM [Sales].[hr].[Staff]", columns=["name"], rows=[("Анна",)])
        out = self.root / "exp"
        t = self.task("export", {"database": "Sales", "tables": ["dbo.Orders", "[hr].[Staff]", "dbo.Missing"],
                                 "format": "csv", "output_dir": str(out)})
        self.server.when("FROM [Sales].[dbo].[Missing]", on_execute=always(
            FakeOdbcError("42000", "[42000] Invalid object name 'dbo.Missing'. (208)")))
        o = self.run_task(t)
        self.assertEqual(o.status, "warning", o.details)
        files = sorted(p.name for p in out.iterdir())
        self.assertEqual(len(files), 2)
        self.assertTrue(any(f.startswith("dbo.Orders_") for f in files))
        self.assertIn("Missing", o.details)
        o2 = self.run_task(t)                                     # повторный запуск в ту же минуту — новые имена
        self.assertEqual(len(list(out.iterdir())), 4)
        self.assertEqual(parse_table("Orders"), ("dbo", "Orders"))
        with self.assertRaises(ValueError):
            parse_table("")

    def test_report_with_history_and_comparison(self):
        from reports.service import ReportService
        tpl = ReportTemplate(id="tpl1", name="Продажи", title="Продажи",
                             sources=[DataSource(id="s", name="Заказы", kind="sql", sql="SELECT id, total FROM orders")],
                             blocks=[Block(id="b", type="table", source="s", title="Т")])
        ReportService(self.svc).templates.save(tpl)
        rows = [(1, 10.0), (2, 20.0)]
        self.server.when("FROM orders", columns=["id", "total"], rows=rows)
        out = self.root / "rep"
        t = self.task("report", {"template_id": "tpl1", "formats": ["docx"], "output_dir": str(out), "keep_history": True,
                                 "compare_keys": {"s": ["id"]}, "notify": "always"})
        o1 = self.run_task(t)
        self.assertEqual(o1.status, "ok", o1.details)
        self.assertIn("первый снимок", o1.details)
        rows[:] = [(1, 15.0), (3, 30.0)]
        o2 = self.run_task(t)
        self.assertIn("новых записей: 1; удалённых: 1; изменённых: 1", o2.details)
        self.assertIn("изменились данные: Заказы", o2.message)
        self.assertEqual(len(list(out.glob("*.docx"))), 2)
        self.assertEqual(self.auto.notes.recent()[0].level, "success")      # notify=always
        t.config["compare_keys"] = {}
        self.auto.save_task(t)
        rows.append((4, 1.0))
        o3 = self.auto.execute_sync(t.id)
        self.assertIn("строк 2 → 3 (+1)", o3.details)


class ScenarioTests(Base):
    def scenario(self, sql, **kw):
        s = Scenario.new("Сценарий", sql)
        s.profile_id = self.profile.id
        for k, v in kw.items():
            setattr(s, k, v)
        self.auto.scenarios.save(s)
        return s

    def conn(self):
        c = self.server.make_connection(self.profile)
        c.connect()
        return c

    def test_read_only_runs_with_params(self):
        self.server.when("FROM t WHERE a >", columns=["a"], rows=[(5,), (6,)])
        s = self.scenario("SELECT a FROM t WHERE a > :min", params=[{"name": "min", "type": "int", "default": "4"}])
        run = run_scenario(self.conn(), s, {"min": "4"})
        self.assertEqual(run.status, "ok", run.error)
        self.assertEqual(run.rowsets[0]["count"], 2)
        sql, params = self.sql("FROM t WHERE a >")[0]
        self.assertEqual((params, "?" in sql), ((4,), True))

    def test_modifying_requires_permission_and_exact_approval(self):
        s = self.scenario("UPDATE t SET a = 1 WHERE id = 5")
        self.assertEqual(run_scenario(self.conn(), s).status, "refused")
        s.allow_modifications = True
        ok, reason, _a = check_permission(s)
        self.assertFalse(ok)
        self.assertIn("не подтверждён", reason)
        approve(s)
        self.auto.scenarios.save(s)
        self.assertTrue(self.auto.scenarios.get(s.id).is_approved)
        s.sql = "UPDATE t SET a = 2 WHERE id = 5"                     # текст изменён после подтверждения
        self.auto.scenarios.save(s)
        stored = self.auto.scenarios.get(s.id)
        self.assertEqual(stored.approved_hash, "")
        self.assertEqual(run_scenario(self.conn(), stored).status, "refused")
        self.assertFalse(any(q.startswith("UPDATE") for q, _ in self.server.executed))

    def test_transaction_commit_and_rollback(self):
        self.server.when("UPDATE t", sets=[{"rowcount": 3}])
        s = self.scenario("UPDATE t SET a = 1 WHERE id > 0", allow_modifications=True)
        approve(s)
        c = self.conn()
        run = run_scenario(c, s)
        self.assertEqual((run.status, run.rowcounts), ("ok", [3]))
        self.assertEqual(c._raw.autocommit_at_execute[-1], False)     # выполнялось в транзакции
        self.assertEqual((c._raw.commits, c.autocommit), (1, True))
        self.server.when("DELETE FROM t", on_execute=always(FakeOdbcError("42000", "[42000] constraint (547)")))
        s2 = self.scenario("UPDATE t SET a = 1 WHERE id > 0\nGO\nDELETE FROM t WHERE id = 1", allow_modifications=True)
        approve(s2)
        run2 = run_scenario(c, s2)
        self.assertEqual(run2.status, "failed")
        self.assertEqual(c._raw.rollbacks, 1)
        self.assertTrue(any("откачена" in m for m in run2.messages))

    def test_param_rules_csv_and_import(self):
        s = self.scenario("SELECT 1 WHERE :x = 1\nGO\nSELECT 2", params=[{"name": "x", "type": "int", "default": "1"}])
        self.assertIn("одного пакета", run_scenario(self.conn(), s).error)
        s2 = self.scenario("SELECT :undeclared")
        self.assertIn("не описанные", run_scenario(self.conn(), s2).error)
        self.server.when("FROM r", columns=["a", "b"], rows=[(i, "x") for i in range(30)])
        out = self.root / "res"
        s3 = self.scenario("SELECT a, b FROM r", save_results=True, results_dir=str(out))
        run = run_scenario(self.conn(), s3, preview_rows=5)
        self.assertEqual((len(run.rowsets[0]["preview"]), run.rowsets[0]["count"]), (5, 30))
        csv_text = Path(run.csv_path).read_text(encoding="utf-8-sig")
        self.assertEqual(len(csv_text.strip().splitlines()), 31)
        f = self.root / "x.sql"
        f.write_text("DELETE FROM t WHERE id = :id", encoding="utf-8")
        imp = self.auto.scenarios.import_sql_file(f)
        self.assertEqual((imp.allow_modifications, imp.is_approved, imp.params[0]["name"]), (False, False, "id"))

    def test_script_task_and_validation(self):
        self.server.when("FROM t", columns=["a"], rows=[(1,)])
        s = self.scenario("SELECT a FROM t", database="Sales")
        t = self.task("script", {"scenario_id": s.id})
        o = self.run_task(t)
        self.assertEqual(o.status, "ok", o.message)
        self.assertEqual(self.sql("USE ")[0][0], "USE [Sales]")
        self.assertEqual(self.auto.scenarios.runs(s.id)[0]["status"], "ok")
        m = self.scenario("DELETE FROM t WHERE id = 1")
        bad = ScheduledTask.new("x", "script", Schedule("daily"), {"scenario_id": m.id})
        self.assertTrue(any("не разрешены" in e for e in self.auto.validate(bad)))
        m.allow_modifications = True
        approve(m)
        self.auto.scenarios.save(m)
        self.assertEqual(self.auto.validate(bad), [])
        bad.schedule = Schedule("on_connect")
        self.assertTrue(any("только сценарии на чтение" in e for e in self.auto.validate(bad)))


class ServiceAndCliTests(Base):
    def test_validation_messages(self):
        cases = [("report", {"template_id": "nope", "formats": [], "output_dir": ""}, "шаблон"),
                 ("export", {"tables": [], "output_dir": "x", "database": "d"}, "таблицу"),
                 ("backup", {"databases": [], "folder": ""}, "базы данных"),
                 ("health", {"latency_warn_ms": 0}, "Порог"),
                 ("table_stats", {}, "базу данных")]
        for kind, cfg, text in cases:
            t = ScheduledTask.new("x", kind, Schedule("daily"), {"profile_id": self.profile.id, **cfg})
            self.assertTrue(any(text in e for e in self.auto.validate(t)), (kind, self.auto.validate(t)))
        b = ScheduledTask.new("x", "backup", Schedule("on_connect"), {"profile_id": self.profile.id, "databases": ["a"],
                                                                     "folder": "D:\\b"})
        self.assertTrue(any("только для задач чтения" in e for e in self.auto.validate(b)))
        noprof = ScheduledTask.new("x", "health", Schedule("daily"), {"profile_id": "missing"})
        self.assertTrue(any("подключение" in e for e in self.auto.validate(noprof)))

    def test_background_duplicate_protection_and_journal(self):
        import threading
        gate = threading.Event()
        self.server.when("SELECT 1", columns=["c"], rows=[(1,)], on_execute=lambda c: gate.wait(3))
        auto = AutomationService(self.svc, background=True)
        t = self.task("health", {"latency_warn_ms": 100000})
        self.assertTrue(auto.run_now(t.id))
        self.assertFalse(auto.run_now(t.id))                       # второй запуск, пока идёт первый, отклонён
        gate.set()
        for task in self.svc.tasks.tasks():
            task.wait(5)
        import time as _t
        _t.sleep(0.1)
        self.assertFalse(auto.is_running(t.id))
        self.assertEqual(self.svc.oplog.recent(1, "automation")[0].status, "ok")

    def test_password_required_for_background_sql_auth(self):
        p = ConnectionProfile.new("SqlAuth", "srv")
        p.auth, p.username = "sql", "sa"
        p = self.svc.profiles.save(p)
        t = ScheduledTask.new("h", "health", Schedule("daily"), {"profile_id": p.id})
        self.auto.save_task(t)
        o = self.run_task(t)
        self.assertEqual(o.status, "failed")
        self.assertIn("сохранённый пароль", o.message)

    def test_cli(self):
        import io
        from contextlib import redirect_stdout
        from automation.cli import main as cli
        t = self.task("health", {"latency_warn_ms": 100000})
        home = str(self.root / "home")
        self.svc.shutdown()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli(["--list-tasks"], home=home, credentials=InMemoryCredentialStore(),
                       connection_factory=self.server.make_connection)
        self.assertEqual(code, 0)
        self.assertIn(t.id, buf.getvalue())
        with redirect_stdout(io.StringIO()) as out:
            code = cli(["--run-task", t.id], home=home, credentials=InMemoryCredentialStore(),
                       connection_factory=self.server.make_connection)
        self.assertEqual(code, 0, out.getvalue())
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["--run-task", "nope"], home=home, credentials=InMemoryCredentialStore()), 2)
        self.svc = create_services(home, InMemoryCredentialStore(), self.server.make_connection)   # для tearDown


if __name__ == "__main__":
    unittest.main()
