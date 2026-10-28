import csv
import tempfile
import threading
import unittest
from pathlib import Path

from database.connection import ConnectionState
from database.editor_session import (ConfirmationRequired, EditorSession, ExecutionListener,
                                     ModificationsNotAllowed, ScriptError)
from database.errors import ConnectionLostError, NotConnectedError
from database.manager import ConnectionManager
from database.profile import ConnectionProfile
from tests.fakes import FakeOdbcError, FakeServer
from utils.tasks import CancelToken


def make_session(server=None, preview=1000, batch=500):
    server = server or FakeServer()
    mgr = ConnectionManager(5, server.make_connection)
    profile = ConnectionProfile(id="p", name="n", server="s")
    mgr.connect_profile(profile)
    conn = mgr.open_session("p")
    s = EditorSession(conn, release=mgr.release, sibling_factory=lambda: mgr.open_session("p"),
                      preview_limit=preview, batch_size=batch)
    return server, mgr, s


class ExecuteTests(unittest.TestCase):
    def test_multiple_result_sets_counts_and_messages(self):
        server, mgr, s = make_session()
        server.when("SELECT multi", sets=[
            {"columns": ["a"], "rows": [(1,), (2,)]},
            {"rowcount": 5},
            {"columns": ["b", "c"], "rows": [("x", "y")]},
        ], messages=[("01000", "[01000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Hello from PRINT")])
        rep = s.execute("SELECT multi")
        self.assertIsNone(rep.error)
        self.assertEqual([(r.kind, r.columns, r.row_count) for r in rep.results],
                         [("rows", ["a"], 2), ("count", [], 5), ("rows", ["b", "c"], 1)])
        self.assertEqual([r.rowset_number for r in rep.results if r.kind == "rows"], [1, 2])
        self.assertEqual(rep.messages, ["Hello from PRINT"])
        self.assertGreaterEqual(rep.elapsed, 0)
        self.assertEqual(rep.total_rows, 3)

    def test_empty_result(self):
        server, _, s = make_session()
        server.when("FROM empty", columns=["a"], rows=[])
        rep = s.execute("SELECT a FROM empty")
        self.assertEqual((rep.results[0].row_count, rep.results[0].rows, rep.results[0].truncated), (0, [], False))

    def test_preview_limit_truncates_and_never_loads_everything(self):
        server, _, s = make_session(preview=250, batch=100)
        server.when("FROM big", columns=["n"], rows=[(i,) for i in range(1000)])
        rep = s.execute("SELECT n FROM big")
        r = rep.results[0]
        self.assertEqual((len(r.rows), r.row_count, r.truncated), (250, 250, True))
        self.assertLessEqual(max(server.fetch_calls), 100)

    def test_go_batches_run_in_order_and_stop_on_error(self):
        server, _, s = make_session()
        server.when("SELECT 2", columns=["x"], rows=[(2,)], errors=[FakeOdbcError("42000", "[42000] Incorrect syntax near 'x'. (102)")])
        rep = s.execute("SELECT 1\nGO\nSELECT 2\nGO\nSELECT 3")
        self.assertIn("Ошибка SQL Server", rep.error)
        self.assertEqual((rep.batches_done, rep.batches_total), (1, 3))
        self.assertFalse(any("SELECT 3" in sql for sql, _ in server.executed))   # третий пакет не запускался
        self.assertEqual(len(rep.results), 1)                                     # результат первого сохранён

    def test_empty_script_and_go_count_are_rejected(self):
        _, _, s = make_session()
        with self.assertRaises(ScriptError):
            s.execute("-- nothing")
        with self.assertRaises(ScriptError):
            s.execute("SELECT 1\nGO 3")

    def test_listener_receives_events_in_order(self):
        server, _, s = make_session()
        server.when("FROM t", columns=["a"], rows=[(1,), (2,), (3,)])
        events = []

        class L(ExecutionListener):
            def on_result(self, i, cols):
                events.append(("start", i, cols))

            def on_rows(self, i, rows):
                events.append(("rows", i, len(rows)))

            def on_result_done(self, i, n, t):
                events.append(("done", i, n, t))

        s.execute("SELECT a FROM t", listener=L())
        self.assertEqual(events, [("start", 0, ["a"]), ("rows", 0, 3), ("done", 0, 3, False)])

    def test_indices_continue_across_batches(self):
        server, _, s = make_session()
        server.when("SELECT 1", columns=["a"], rows=[(1,)])
        server.when("SELECT 2", columns=["b"], rows=[(2,)])
        rep = s.execute("SELECT 1\nGO\nSELECT 2")
        self.assertEqual([r.index for r in rep.results], [0, 1])
        self.assertEqual([r.rowset_number for r in rep.results], [1, 2])

    def test_cancel_via_driver(self):
        server, _, s = make_session()
        started = threading.Event()

        def block(cursor):
            started.set()
            cursor.cancel_event.wait(3)
            raise FakeOdbcError("HY008", "Operation canceled")

        server.when("LONG", on_execute=block)
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("rep", s.execute("SELECT LONG")))
        t.start()
        self.assertTrue(started.wait(2))
        self.assertTrue(s.cancel())
        t.join(3)
        self.assertTrue(out["rep"].cancelled)
        self.assertIsNone(out["rep"].error)
        self.assertTrue(s.conn.is_connected)

    def test_cancel_token_stops_between_batches_and_keeps_partial(self):
        server, _, s = make_session(batch=10)
        server.when("FROM t", columns=["n"], rows=[(i,) for i in range(100)])
        tok = CancelToken()

        class L(ExecutionListener):
            def on_rows(self, i, rows):
                tok.cancel()

        rep = s.execute("SELECT n FROM t", cancel_token=tok, listener=L())
        self.assertTrue(rep.cancelled)
        self.assertEqual(len(rep.results[0].rows), 10)

    def test_connection_loss_no_auto_retry_then_manual_reconnect(self):
        server, _, s = make_session()
        server.when("SELECT 5", columns=["a"], rows=[(5,)],
                    errors=[FakeOdbcError("08S01", "Communication link failure")])
        rep = s.execute("SELECT 5")
        self.assertIn("потеряно", rep.error)
        self.assertEqual(sum(1 for q, _ in server.executed if "SELECT 5" in q), 1)   # без автоповтора
        self.assertEqual(s.conn.state, ConnectionState.LOST)
        rep2 = s.execute("SELECT 5")                 # новый запуск пользователем — переподключение
        self.assertIsNone(rep2.error)
        self.assertEqual(rep2.results[0].rows, [(5,)])

    def test_closed_session_reports_not_connected(self):
        _, _, s = make_session()
        s.close()
        rep = s.execute("SELECT 1")
        self.assertIn("Нет подключения", rep.error)


class SafetyTests(unittest.TestCase):
    def test_read_only_by_default_blocks_modifications(self):
        server, _, s = make_session()
        for sql in ("DELETE FROM t WHERE id = 1", "UPDATE t SET a = 1", "DROP TABLE t", "INSERT INTO t VALUES (1)",
                    "EXEC my_proc", "SELECT 1\nGO\nTRUNCATE TABLE t"):
            with self.assertRaises(ModificationsNotAllowed, msg=sql):
                s.execute(sql, confirmed=True)       # даже «подтверждено» — режим чтения сильнее
        self.assertEqual([q for q, _ in server.executed if "SERVERPROPERTY" not in q], [])  # на сервер ничего не ушло

    def test_confirmation_required_when_modifications_allowed(self):
        server, _, s = make_session()
        s.allow_modifications = True
        with self.assertRaises(ConfirmationRequired) as ctx:
            s.execute("DELETE FROM t")
        self.assertIn("ВСЕ строки", str(ctx.exception))
        self.assertEqual([q for q, _ in server.executed if "SERVERPROPERTY" not in q], [])
        rep = s.execute("DELETE FROM t", confirmed=True)
        self.assertIsNone(rep.error)
        self.assertTrue(any("DELETE FROM t" in q for q, _ in server.executed))

    def test_reads_need_no_confirmation(self):
        _, _, s = make_session()
        self.assertIsNone(s.execute("SELECT 1").error)

    def test_modification_not_retried_after_connection_loss(self):
        server, _, s = make_session()
        s.allow_modifications = True
        server.when("UPDATE", errors=[FakeOdbcError("08S01", "Communication link failure")])
        rep = s.execute("UPDATE t SET a = 1 WHERE id = 1", confirmed=True)
        self.assertIn("потеряно", rep.error)
        self.assertEqual(sum(1 for q, _ in server.executed if "UPDATE" in q), 1)


class TransactionTests(unittest.TestCase):
    def test_transaction_mode_commit_and_rollback(self):
        server, _, s = make_session()
        s.allow_modifications = True
        s.configure(transaction=True)
        raw = s.conn._raw
        self.assertFalse(raw.autocommit)
        s.execute("UPDATE t SET a = 1 WHERE id = 1", confirmed=True)
        self.assertTrue(s.in_transaction)
        s.commit()
        self.assertEqual((raw.commits, s.in_transaction), (1, False))
        s.execute("DELETE FROM t WHERE id = 2", confirmed=True)
        s.rollback()
        self.assertEqual((raw.rollbacks, s.in_transaction), (1, False))

    def test_cannot_leave_transaction_mode_with_open_transaction(self):
        server, _, s = make_session()
        s.configure(transaction=True)
        s.execute("SELECT 1")
        self.assertTrue(s.in_transaction)
        with self.assertRaises(Exception) as ctx:
            s.configure(transaction=False)
        self.assertIn("COMMIT", str(ctx.exception))
        s.rollback()
        s.configure(transaction=False)
        self.assertTrue(s.conn._raw.autocommit)

    def test_close_rolls_back_open_transaction(self):
        server, mgr, s = make_session()
        s.configure(transaction=True)
        s.execute("SELECT 1")
        raw = s.conn._raw
        s.close()
        self.assertEqual(raw.rollbacks, 1)
        self.assertTrue(raw.closed)
        self.assertEqual(mgr.count(), 1)             # осталось только основное подключение

    def test_lost_connection_clears_transaction_flag(self):
        server, _, s = make_session()
        s.configure(transaction=True)
        s.execute("SELECT 1")
        server.when("SELECT 9", errors=[FakeOdbcError("08S01", "Communication link failure")])
        rep = s.execute("SELECT 9")
        self.assertFalse(s.in_transaction)
        self.assertTrue(any("Транзакция потеряна" in m for m in rep.messages))

    def test_autocommit_default_has_no_transaction(self):
        _, _, s = make_session()
        s.execute("SELECT 1")
        self.assertFalse(s.in_transaction)


class ContextTests(unittest.TestCase):
    def test_use_database_is_quoted_and_reapplied_after_reconnect(self):
        server, _, s = make_session()
        s.configure(database="Sa]les")
        s.execute("SELECT 1")
        uses = [q for q, _ in server.executed if q.startswith("USE ")]
        self.assertEqual(uses, ["USE [Sa]]les]"])
        s.execute("SELECT 1")
        self.assertEqual(len([q for q, _ in server.executed if q.startswith("USE ")]), 1)   # не повторяется
        s.conn.reconnect()
        s.execute("SELECT 1")
        self.assertEqual(len([q for q, _ in server.executed if q.startswith("USE ")]), 2)   # после переподключения — снова

    def test_use_in_script_updates_current_database(self):
        server, _, s = make_session()
        server.when("DB_NAME()", columns=["d"], rows=[("Other",)])
        s.execute("USE Other; SELECT 1")
        self.assertEqual(s.database, "Other")

    def test_timeout_override_applied_and_kept_after_reconnect(self):
        server, _, s = make_session()
        s.configure(timeout=7)
        self.assertEqual(s.conn._raw.timeout, 7)
        s.conn.reconnect()
        self.assertEqual(s.conn._raw.timeout, 7)


class FullExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_streams_selected_rowset_on_separate_connection(self):
        server, mgr, s = make_session(preview=10)
        server.when("SELECT two", sets=[{"columns": ["a"], "rows": [(0,)]},
                                        {"columns": ["n"], "rows": [(i,) for i in range(2500)]}])
        before = mgr.count()
        res = s.export_full("SELECT two", self.d / "o.csv", "csv", rowset_number=2)
        self.assertEqual(res.rows, 2500)
        with open(self.d / "o.csv", encoding="utf-8-sig") as fh:
            self.assertEqual(sum(1 for _ in fh), 2501)
        self.assertEqual(mgr.count(), before)                    # временное соединение закрыто
        self.assertEqual(len(server.open_raws()), before)

    def test_refuses_non_read_only(self):
        _, _, s = make_session()
        s.allow_modifications = True
        with self.assertRaises(ModificationsNotAllowed):
            s.export_full("DELETE FROM t", self.d / "o.csv", "csv")
        self.assertEqual(list(self.d.iterdir()), [])

    def test_cancel_removes_partial_file_and_closes_connection(self):
        server, mgr, s = make_session()
        server.when("FROM big", columns=["n"], rows=[(i,) for i in range(5000)])
        tok = CancelToken()
        before = mgr.count()

        def progress(n):
            tok.cancel()

        from database.errors import QueryCancelledError
        with self.assertRaises(QueryCancelledError):
            s.export_full("SELECT n FROM big", self.d / "o.csv", "csv", cancel_token=tok, progress=progress)
        self.assertEqual(list(self.d.iterdir()), [])
        self.assertEqual(mgr.count(), before)

    def test_error_midway_cleans_up(self):
        server, mgr, s = make_session()
        server.when("FROM bad", errors=[FakeOdbcError("42000", "[42000] Invalid object name 'bad'. (208)")])
        before = mgr.count()
        with self.assertRaises(Exception):
            s.export_full("SELECT 1 FROM bad", self.d / "o.csv", "csv")
        self.assertEqual(list(self.d.iterdir()), [])
        self.assertEqual(mgr.count(), before)

    def test_no_rowset_is_error(self):
        _, _, s = make_session()
        with self.assertRaises(Exception) as ctx:
            s.export_full("SET NOCOUNT ON", self.d / "o.csv", "csv")
        self.assertIn("не вернул", str(ctx.exception))
        self.assertEqual(list(self.d.iterdir()), [])


class ManagerSessionTests(unittest.TestCase):
    def test_open_session_requires_active_primary_and_counts_toward_limit(self):
        server = FakeServer()
        mgr = ConnectionManager(2, server.make_connection)
        with self.assertRaises(NotConnectedError):
            mgr.open_session("p")
        mgr.connect_profile(ConnectionProfile(id="p", name="n", server="s"))
        c = mgr.open_session("p")
        from database.errors import ConnectionLimitError
        with self.assertRaises(ConnectionLimitError):
            mgr.open_session("p")
        mgr.release(c)
        self.assertEqual(mgr.count(), 1)
        self.assertIsNotNone(mgr.primary("p"))


if __name__ == "__main__":
    unittest.main()
