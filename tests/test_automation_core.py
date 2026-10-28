import datetime as dt
import tempfile
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from pathlib import Path

from automation.schedule import Schedule, describe, next_run, validate
from automation.scheduler import Scheduler
from automation.snapshots import SnapshotStore
from automation.store import MetricStore, Notifications, RunLog, ScheduledTask, TaskStore
from automation.winsched import NS, launch_command, schtasks_create, supported, task_name, task_xml
from comparison.data_diff import DiffError, diff_tables, norm
from storage.db import AppDatabase

D = dt.datetime


class ScheduleTests(unittest.TestCase):
    def test_daily_weekly_monthly(self):
        self.assertEqual(next_run(Schedule("daily", time="08:00"), D(2026, 10, 20, 7, 59)), D(2026, 10, 20, 8, 0))
        self.assertEqual(next_run(Schedule("daily", time="08:00"), D(2026, 10, 20, 8, 0)), D(2026, 10, 21, 8, 0))
        self.assertEqual(next_run(Schedule("daily", time="23:59"), D(2026, 12, 31, 23, 59, 30)), D(2027, 1, 1, 23, 59))
        wk = Schedule("weekly", time="09:30", weekdays=[0, 2])                      # пн, ср
        self.assertEqual(next_run(wk, D(2026, 10, 20, 10, 0)), D(2026, 10, 21, 9, 30))   # вт -> ср
        self.assertEqual(next_run(wk, D(2026, 10, 21, 9, 30)), D(2026, 10, 26, 9, 30))   # ср 9:30 -> пн
        m31 = Schedule("monthly", time="07:00", day=31)
        self.assertEqual(next_run(m31, D(2026, 2, 1)), D(2026, 2, 28, 7, 0))             # нет 31-го — последний день
        self.assertEqual(next_run(m31, D(2028, 2, 1)), D(2028, 2, 29, 7, 0))             # високосный год
        self.assertEqual(next_run(m31, D(2026, 12, 31, 7, 0)), D(2027, 1, 31, 7, 0))
        self.assertEqual(next_run(Schedule("monthly", time="07:00", day=15), D(2026, 10, 15, 6, 0)), D(2026, 10, 15, 7, 0))

    def test_interval_once_on_connect(self):
        iv = Schedule("interval", interval_minutes=15, start="2026-10-20 10:00")
        self.assertEqual(next_run(iv, D(2026, 10, 20, 10, 44)), D(2026, 10, 20, 10, 45))
        self.assertEqual(next_run(iv, D(2026, 10, 20, 10, 45)), D(2026, 10, 20, 11, 0))
        self.assertEqual(next_run(iv, D(2026, 10, 20, 9, 0)), D(2026, 10, 20, 10, 0))
        self.assertEqual(next_run(Schedule("interval", interval_minutes=5), D(2026, 1, 1, 0, 0)), D(2026, 1, 1, 0, 5))
        once = Schedule("once", at="2026-10-21 10:00")
        self.assertEqual(next_run(once, D(2026, 10, 21, 9, 0)), D(2026, 10, 21, 10, 0))
        self.assertIsNone(next_run(once, D(2026, 10, 21, 10, 0)))
        self.assertIsNone(next_run(Schedule("on_connect"), D(2026, 1, 1)))

    def test_validation_and_description(self):
        bad = [Schedule("daily", time="25:00"), Schedule("weekly", weekdays=[]), Schedule("monthly", day=32),
               Schedule("interval", interval_minutes=0), Schedule("once", at="завтра"), Schedule("hourly")]
        for s in bad:
            self.assertTrue(validate(s), s)
        self.assertEqual(validate(Schedule("weekly", weekdays=[4], time="18:05")), [])
        self.assertEqual(describe(Schedule("weekly", weekdays=[4, 0], time="09:00")), "Еженедельно: пн, пт в 09:00")
        self.assertIn("последний день", describe(Schedule("monthly", day=31, time="07:00")))
        self.assertEqual(describe(Schedule("interval", interval_minutes=120)), "Каждые 2 ч")
        self.assertEqual(Schedule.from_dict(Schedule("weekly", weekdays=[1]).to_dict()).weekdays, [1])


class StoresTests(unittest.TestCase):
    def setUp(self):
        self.db = AppDatabase(":memory:")

    def test_tasks_roundtrip(self):
        ts = TaskStore(self.db)
        t = ScheduledTask.new("Отчёт", "report", Schedule("daily", time="07:30"), {"a": [1, "б"]})
        ts.save(t)
        got = ts.get(t.id)
        self.assertEqual((got.name, got.schedule.time, got.config), ("Отчёт", "07:30", {"a": [1, "б"]}))
        got.enabled = False
        ts.save(got)
        self.assertFalse(ts.list()[0].enabled)
        ts.delete(t.id)
        self.assertIsNone(ts.get(t.id))

    def test_runlog_trim_interrupted_and_redaction(self):
        rl = RunLog(self.db, keep_per_task=3)
        for i in range(5):
            rl.add("t1", "ok", "manual", f"запуск {i} PWD=secret{i}")
        recs = rl.recent("t1")
        self.assertEqual([r.message for r in recs], ["запуск 4 PWD=***", "запуск 3 PWD=***", "запуск 2 PWD=***"])
        rl.start("t2", "schedule")
        self.assertEqual(rl.mark_interrupted(), 1)
        self.assertEqual(rl.recent("t2")[0].status, "failed")

    def test_notifications_and_metrics(self):
        n = Notifications(self.db, keep=3)
        seen = []
        n.listeners.append(seen.append)
        for i in range(4):
            n.add("error", f"t{i}", "m")
        self.assertEqual(len(n.recent()), 3)
        self.assertEqual(n.unread_count(), 3)
        n.mark_all_read()
        self.assertEqual(n.unread_count(), 0)
        self.assertEqual(len(seen), 4)
        m = MetricStore(self.db, keep_days=30)
        m.add_many("p", [("latency_ms", "", 10.0), ("db_size_mb", "A", 5.0)], ts="2026-01-01T00:00:00")
        m.add_many("p", [("latency_ms", "", 12.0), ("db_size_mb", "A", 6.0), ("db_size_mb", "B", 1.0)])
        self.assertEqual([v for _t, v in m.series("p", "latency_ms")], [12.0])    # старое удалено (>30 дней)
        self.assertEqual({k: v for k, (_t, v) in m.latest("p", "db_size_mb").items()}, {"A": 6.0, "B": 1.0})


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.db = AppDatabase(":memory:")
        self.store, self.runs, self.notes = TaskStore(self.db), RunLog(self.db), Notifications(self.db)
        self.now = D(2026, 10, 20, 7, 0)
        self.submitted = []
        self.busy = set()

        def submit(t, trigger):
            if t.id in self.busy:
                return False
            self.submitted.append((t.id, trigger))
            return True

        self.sch = Scheduler(self.store, self.runs, self.notes, submit, clock=lambda: self.now)

    def add(self, sched, **kw):
        t = ScheduledTask.new("T", "health", sched, {"profile_id": "p"})
        for k, v in kw.items():
            setattr(t, k, v)
        self.sch.refresh(t)
        self.store.save(t)
        return t

    def test_tick_runs_due_tasks_once_and_reschedules(self):
        t = self.add(Schedule("daily", time="08:00"))
        self.assertEqual(self.sch.tick(), [])
        self.now = D(2026, 10, 20, 8, 0, 20)
        self.assertEqual(self.sch.tick(), [t.id])
        self.assertEqual(self.sch.tick(), [])                                    # повторно в ту же минуту — нет
        self.assertEqual(self.store.get(t.id).next_run, "2026-10-21T08:00:00")
        self.assertEqual(self.submitted, [(t.id, "schedule")])

    def test_busy_task_is_skipped_and_logged(self):
        t = self.add(Schedule("interval", interval_minutes=5))
        self.busy.add(t.id)
        self.now += dt.timedelta(minutes=6)
        self.assertEqual(self.sch.tick(), [])
        self.assertIn("ещё выполняется", self.runs.recent(t.id)[0].message)

    def test_disabled_and_on_connect(self):
        t1 = self.add(Schedule("daily", time="08:00"), enabled=False)
        t2 = self.add(Schedule("on_connect"))
        self.assertIsNone(self.store.get(t1.id).next_run)
        self.now = D(2026, 10, 25)
        self.assertEqual(self.sch.tick(), [])
        self.assertEqual(self.sch.on_connect("p"), [t2.id])
        self.assertEqual(self.sch.on_connect("другой"), [])

    def test_missed_runs_while_closed(self):
        t = self.add(Schedule("daily", time="08:00"))
        c = self.add(Schedule("daily", time="08:00"), catch_up=True)
        self.now = D(2026, 10, 23, 12, 0)                                        # программа была закрыта трое суток
        self.sch.startup()
        self.assertEqual(self.submitted, [(c.id, "catch_up")])                  # только задача с «догнать»
        self.assertEqual(self.runs.recent(t.id)[0].status, "skipped")
        self.assertIn("не работала", self.runs.recent(t.id)[0].message)
        self.assertEqual(self.store.get(t.id).next_run, "2026-10-24T08:00:00")
        self.assertTrue(any("Пропущен запуск" in n.title for n in self.notes.recent()))

    def test_thread_start_stop(self):
        self.sch.start(interval=0.05)
        self.assertTrue(self.sch.running)
        self.sch.stop()
        self.assertFalse(self.sch.running)


class WinSchedTests(unittest.TestCase):
    def task(self, s, **kw):
        t = ScheduledTask.new("Отчёт <&> «продаж»", "report", s)
        for k, v in kw.items():
            setattr(t, k, v)
        return t

    def parse(self, xml):
        return ET.fromstring(xml.split("?>", 1)[1])

    def test_xml_for_each_schedule(self):
        now = D(2026, 10, 20, 12, 0)
        cases = {
            "daily": (Schedule("daily", time="08:05"), ".//{%s}ScheduleByDay" % NS),
            "weekly": (Schedule("weekly", time="09:00", weekdays=[0, 4]), ".//{%s}DaysOfWeek/{%s}Friday" % (NS, NS)),
            "monthly": (Schedule("monthly", time="07:00", day=31), ".//{%s}DaysOfMonth/{%s}Day" % (NS, NS)),
            "interval": (Schedule("interval", interval_minutes=15, start="2026-10-20 10:00"), ".//{%s}Repetition/{%s}Interval" % (NS, NS)),
            "once": (Schedule("once", at="2026-10-21 10:00"), ".//{%s}TimeTrigger" % NS),
        }
        for name, (s, path) in cases.items():
            root = self.parse(task_xml(self.task(s), "C:\\Py\\pythonw.exe", '"C:\\a b\\main.py" --run-task x', "C:\\a b", now))
            self.assertIsNotNone(root.find(path), name)
        root = self.parse(task_xml(self.task(Schedule("monthly", time="07:00", day=31)), "c", "a", "w", now))
        self.assertEqual(root.find(".//{%s}Day" % NS).text, "Last")
        root = self.parse(task_xml(self.task(Schedule("daily", time="08:05"), catch_up=True), "c", '"x" --run-task id', "w", now))
        self.assertEqual(root.find(".//{%s}StartBoundary" % NS).text, "2026-10-20T08:05:00")
        self.assertEqual(root.find(".//{%s}StartWhenAvailable" % NS).text, "true")
        self.assertEqual(root.find(".//{%s}Arguments" % NS).text, '"x" --run-task id')
        self.assertEqual(root.find(".//{%s}LogonType" % NS).text, "InteractiveToken")
        self.assertIn("<&>", root.find(".//{%s}Description" % NS).text)

    def test_unsupported_and_commands(self):
        self.assertFalse(supported(Schedule("on_connect"))[0])
        self.assertFalse(supported(Schedule("monthly", day=30))[0])
        self.assertTrue(supported(Schedule("monthly", day=28))[0])
        with self.assertRaises(ValueError):
            task_xml(self.task(Schedule("on_connect")), "c", "a", "w")
        t = self.task(Schedule("daily"))
        cmd = schtasks_create(t, Path("C:/t.xml"))
        self.assertEqual(cmd[:3], ["schtasks", "/Create", "/TN"])
        self.assertTrue(task_name(t).startswith("DBWorkspacePro\\"))
        self.assertNotIn("<", task_name(t))
        prog, args, wd = launch_command("abc")
        self.assertIn("--run-task abc", args)
        self.assertTrue(args.startswith('"') and "main.py" in args)

    def test_xml_written_as_utf16(self):
        from automation.winsched import write_xml
        with tempfile.TemporaryDirectory() as d:
            p = write_xml(self.task(Schedule("daily")), Path(d) / "t.xml")
            self.assertTrue(p.read_bytes().startswith((b"\xff\xfe", b"\xfe\xff")))


class DiffSnapshotTests(unittest.TestCase):
    def test_diff_by_key(self):
        old = (["id", "name", "sum"], [(1, "a", 10), (2, "b", 20), (3, "c", 30)])
        new = (["id", "name", "sum", "extra"], [(1, "a", 10.0), (2, "B", 20), (4, "d", 5, "x")])
        d = diff_tables(*old, *new, key_columns=["id"])
        self.assertEqual((d.added_total, d.removed_total, d.changed_total, d.unchanged), (1, 1, 1, 1))
        self.assertEqual(d.changed[0].changes, {"name": ("b", "B")})
        self.assertEqual(d.columns_added, ["extra"])
        self.assertEqual(d.totals, {"sum": (Decimal(60), Decimal(35))})        # ключ «id» в итогах не суммируется
        self.assertIn("новых записей: 1", d.summary())

    def test_composite_key_and_type_normalisation(self):
        old = (["y", "m", "v", "d"], [(2024, 1, Decimal("1.50"), dt.date(2024, 1, 1))])
        new = (["y", "m", "v", "d"], [(2024, 1, "1.5", "2024-01-01")])            # как после загрузки снимка из JSON
        d = diff_tables(*old, *new, key_columns=["y", "m"])
        self.assertFalse(d.has_changes)
        self.assertEqual(norm(1), norm(1.0))
        self.assertEqual(norm(Decimal("1.00")), norm("1"))

    def test_unreliable_matching_is_refused(self):
        with self.assertRaises(DiffError) as ctx:
            diff_tables(["id"], [(1,), (1,)], ["id"], [(1,)], ["id"])
        self.assertIn("неуникален", str(ctx.exception))
        with self.assertRaises(DiffError):
            diff_tables(["a"], [], ["b"], [], ["a"])
        with self.assertRaises(DiffError):
            diff_tables(["a"], [], ["a"], [], [])

    def test_max_items_cap(self):
        d = diff_tables(["id"], [], ["id"], [(i,) for i in range(50)], ["id"], max_items=10)
        self.assertEqual((d.added_total, len(d.added)), (50, 10))

    def test_snapshots_keep_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = SnapshotStore(Path(tmp), keep=3, max_rows=2)
            for i in range(5):
                st.save("task/1", "src", ["a", "d"], [(i, Decimal("1.5")), (i + 1, dt.date(2024, 1, 1)), (9, None)],
                        when=D(2026, 1, 1, 0, 0, i))
            self.assertEqual(len(st.list("task/1", "src")), 3)
            prev, latest = st.previous_and_latest("task/1", "src")
            self.assertEqual(latest["rows"][0], (4, "1.5"))
            self.assertTrue(latest["truncated"])
            self.assertEqual(prev["rows"][0][0], 3)
            self.assertEqual(st.previous_and_latest("none", "src"), (None, None))


if __name__ == "__main__":
    unittest.main()
