import threading
import time
import unittest

from database.connection import ConnectionState
from database.errors import (ConnectionFailedError, ConnectionLostError, DriverNotAvailableError,
                             NotConnectedError, QueryCancelledError, QueryError, QueryTimeoutError)
from database.profile import AUTH_SQL, ConnectionProfile
from tests.fakes import FakeOdbcError, FakeServer
from utils.tasks import CancelToken


def make(server=None, **pkw):
    server = server or FakeServer()
    profile = ConnectionProfile(id="p", name="t", server="srv", **pkw)
    return server, server.make_connection(profile)


class ConnectTests(unittest.TestCase):
    def test_connect_ok_sets_state_and_info(self):
        server, c = make()
        states = []
        c.add_state_listener(lambda conn, old, new: states.append(new))
        c.connect()
        self.assertTrue(c.is_connected)
        self.assertEqual(c.server_info.version, "15.0.2000")
        self.assertEqual(states, [ConnectionState.CONNECTING, ConnectionState.CONNECTED])
        self.assertEqual(server.raws[0].timeout, 60)

    def test_missing_sql_server_gives_friendly_error(self):
        server = FakeServer()
        server.connect_errors.append(FakeOdbcError("08001", "[08001] TCP Provider: No such host is known."))
        _, c = make(server)
        with self.assertRaises(ConnectionFailedError) as ctx:
            c.connect()
        self.assertIn("Не удалось подключиться к серверу", str(ctx.exception))
        self.assertEqual(c.state, ConnectionState.ERROR)
        self.assertEqual(server.open_raws(), [])

    def test_no_driver(self):
        from database.connection import SqlConnection
        c = SqlConnection(ConnectionProfile(id="p", name="t", server="s"),
                          connector=FakeServer().connector, driver_lister=lambda: [])
        with self.assertRaises(DriverNotAvailableError):
            c.connect()

    def test_password_not_logged_in_error(self):
        server = FakeServer()
        server.connect_errors.append(FakeOdbcError("28000", "Login failed (18456) PWD=hunter2"))
        profile = ConnectionProfile(id="p", name="t", server="s", auth=AUTH_SQL, username="sa")
        c = server.make_connection(profile, "hunter2")
        with self.assertRaises(ConnectionFailedError) as ctx:
            c.connect()
        self.assertNotIn("hunter2", str(ctx.exception))
        self.assertIn("PWD=hunter2", server.connection_strings[0])  # пароль есть только в строке для драйвера

    def test_close_clears_state_and_password(self):
        server, c = make()
        c.connect()
        c.close()
        self.assertEqual(c.state, ConnectionState.DISCONNECTED)
        self.assertEqual(server.open_raws(), [])
        self.assertIsNone(c._password)


class QueryTests(unittest.TestCase):
    def test_empty_result(self):
        server, c = make()
        server.when("FROM empty", columns=["a"], rows=[])
        c.connect()
        r = c.fetch_rows("SELECT a FROM empty")
        self.assertEqual((r.columns, r.rows, r.truncated), (["a"], [], False))

    def test_non_select_without_columns(self):
        server, c = make()
        c.connect()
        r = c.fetch_rows("UPDATE t SET a=1")
        self.assertEqual(r.rows, [])

    def test_fetchmany_batches_and_max_rows(self):
        server, c = make()
        server.when("FROM big", columns=["n"], rows=[(i,) for i in range(1050)])
        c.connect()
        seen = []
        res = c.stream("SELECT n FROM big", consumer=lambda b: seen.append(len(b)), batch_size=500)
        self.assertEqual(seen, [500, 500, 50])
        self.assertEqual(res.row_count, 1050)
        self.assertFalse(res.truncated)

        r = c.fetch_rows("SELECT n FROM big", max_rows=600, batch_size=500)
        self.assertEqual(len(r.rows), 600)
        self.assertTrue(r.truncated)
        self.assertLessEqual(max(server.fetch_calls), 500)  # никогда не тянем всё сразу

    def test_max_rows_exact_not_truncated(self):
        server, c = make()
        server.when("FROM t", columns=["n"], rows=[(i,) for i in range(10)])
        c.connect()
        r = c.fetch_rows("SELECT n FROM t", max_rows=10)
        self.assertFalse(r.truncated)

    def test_params_passed_separately(self):
        server, c = make()
        c.connect()
        c.fetch_rows("SELECT * FROM t WHERE id = ?", (42,))
        sql, params = server.executed[-1]
        self.assertNotIn("42", sql)
        self.assertEqual(params, (42,))

    def test_not_connected(self):
        _, c = make()
        with self.assertRaises(NotConnectedError):
            c.fetch_rows("SELECT 1")

    def test_cursor_closed_after_error(self):
        server, c = make()
        server.when("BAD", errors=[FakeOdbcError("42000", "[42000] Incorrect syntax")])
        c.connect()
        with self.assertRaises(QueryError):
            c.fetch_rows("SELECT BAD")
        self.assertTrue(all(cur.closed for cur in server.raws[0].cursors))
        self.assertTrue(c.is_connected)  # обычная ошибка запроса не рвёт соединение

    def test_timeout_error(self):
        server, c = make()
        server.when("SLOW", errors=[FakeOdbcError("HYT00", "Query timeout expired")])
        c.connect()
        with self.assertRaises(QueryTimeoutError):
            c.fetch_rows("SELECT SLOW")
        self.assertTrue(c.is_connected)

    def test_cancel_token_between_batches(self):
        server, c = make()
        server.when("FROM t", columns=["n"], rows=[(i,) for i in range(100)])
        c.connect()
        tok = CancelToken()

        def consume(batch):
            tok.cancel()

        with self.assertRaises(QueryCancelledError):
            c.stream("SELECT n FROM t", consumer=consume, batch_size=10, cancel_token=tok)

    def test_cancel_running_query_via_driver(self):
        server, c = make()

        def block(cursor):
            started.set()
            cursor.cancel_event.wait(3)
            raise FakeOdbcError("HY008", "Operation canceled")

        started = threading.Event()
        server.when("LONG", on_execute=block)
        c.connect()
        out = {}

        def run():
            try:
                c.fetch_rows("SELECT LONG")
            except Exception as e:  # noqa: BLE001
                out["e"] = e

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(started.wait(2))
        self.assertTrue(c.cancel_running())
        t.join(3)
        self.assertIsInstance(out.get("e"), QueryCancelledError)
        self.assertTrue(c.is_connected)

    def test_one_connection_not_used_concurrently(self):
        server, c = make()
        active = {"n": 0, "max": 0}
        lock = threading.Lock()

        def slow(cursor):
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
            time.sleep(0.03)
            with lock:
                active["n"] -= 1

        server.when("SELECT w", columns=["a"], rows=[(1,)], on_execute=slow)
        c.connect()
        ts = [threading.Thread(target=lambda: c.fetch_rows("SELECT w")) for _ in range(5)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(active["max"], 1)


class ReconnectTests(unittest.TestCase):
    def lost(self):
        return FakeOdbcError("08S01", "[08S01] Communication link failure")

    def test_safe_read_reconnects_once(self):
        server, c = make()
        server.when("FROM t", columns=["a"], rows=[(1,)], errors=[self.lost()])
        c.connect()
        r = c.fetch_rows("SELECT a FROM t", safe_to_retry=True)
        self.assertEqual(r.rows, [(1,)])
        self.assertEqual(server.connect_calls, 2)
        self.assertTrue(c.is_connected)
        self.assertEqual(len(server.open_raws()), 1)  # старое закрыто

    def test_unsafe_statement_is_never_retried(self):
        server, c = make()
        server.when("UPDATE", errors=[self.lost()])
        c.connect()
        with self.assertRaises(ConnectionLostError):
            c.fetch_rows("UPDATE t SET a = 1")  # safe_to_retry=False по умолчанию
        self.assertEqual(server.connect_calls, 1)
        self.assertEqual(sum(1 for s, _ in server.executed if "UPDATE" in s), 1)
        self.assertEqual(c.state, ConnectionState.LOST)

    def test_lost_then_safe_call_reconnects(self):
        server, c = make()
        server.when("UPDATE", errors=[self.lost()])
        c.connect()
        with self.assertRaises(ConnectionLostError):
            c.fetch_rows("UPDATE t SET a = 1")
        r = c.fetch_rows("SELECT 1", safe_to_retry=True)
        self.assertEqual(r.rows, [(1,)])
        self.assertEqual(c.state, ConnectionState.CONNECTED)

    def test_no_retry_after_partial_delivery(self):
        server, c = make()
        rows = [(i,) for i in range(30)]
        state = {"n": 0}
        server.when("FROM t", columns=["n"], rows=rows)
        c.connect()
        orig = server.raws[0].cursor

        def cursor():
            cur = orig()
            real = cur.fetchmany

            def fm(n):
                state["n"] += 1
                if state["n"] == 2:
                    raise self.lost()
                return real(n)
            cur.fetchmany = fm
            return cur
        server.raws[0].cursor = cursor
        got = []
        with self.assertRaises(ConnectionLostError):
            c.stream("SELECT n FROM t", consumer=got.extend, batch_size=10, safe_to_retry=True)
        self.assertEqual(len(got), 10)  # дубликатов нет, повтор не выполнялся
        self.assertEqual(server.connect_calls, 1)

    def test_retry_gives_up_if_reconnect_fails(self):
        server, c = make()
        server.when("FROM t", errors=[self.lost()])
        c.connect()
        server.connect_errors.append(FakeOdbcError("08001", "TCP Provider: No such host"))
        with self.assertRaises(ConnectionFailedError):
            c.fetch_rows("SELECT a FROM t", safe_to_retry=True)
        self.assertEqual(server.open_raws(), [])


if __name__ == "__main__":
    unittest.main()
