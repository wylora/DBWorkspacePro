import unittest

from database.profile import ConnectionProfile
from monitoring.collect import collect, metrics_from
from tests.fakes import FakeOdbcError, FakeServer

DENIED = FakeOdbcError("42000", "[42000] The permission was denied (300)")


def always(err):
    def raiser(cursor):
        raise err
    return raiser


class MonitoringTests(unittest.TestCase):
    def conn(self, server):
        c = server.make_connection(ConnectionProfile(id="p", name="n", server="s"))
        c.connect()
        return c

    def full_server(self, perms=1):
        s = FakeServer()
        s.when("HAS_PERMS_BY_NAME", columns=["p"], rows=[(perms,)])
        s.when("sys.master_files", columns=["d", "a", "b"], rows=[("Sales", 100.0, 20.0), ("master", 5.0, 1.0)])
        s.when("COUNT(*) FROM [Sales].sys.tables", columns=["n"], rows=[(42,)])
        s.when("sys.allocation_units", columns=["s", "t", "r", "m"], rows=[("dbo", "Orders", 1500, 12.5), ("dbo", "Items", 30, 0.1)])
        s.when("sys.dm_exec_requests", columns=list("abcdefghijkl"),
               rows=[(55, "running", "SELECT", "Sales", None, 0, 1200, 0, "u", "h", "app", "SELECT 1"),
                     (56, "suspended", "UPDATE", "Sales", "LCK_M_X", 900, 950, 55, "u2", "h2", "app", "UPDATE t")])
        s.when("sys.dm_tran_locks", columns=list("abcdef"), rows=[(56, "KEY", "Sales", "X", "WAIT", 1)])
        return s

    def test_full_rights(self):
        snap = collect(self.conn(self.full_server()), "Sales")
        self.assertTrue(snap.probe.ok)
        self.assertIsNotNone(snap.probe.latency_ms)
        self.assertEqual(snap.db_sizes[0], ("Sales", 100.0, 20.0))
        self.assertEqual(snap.table_count, 42)
        self.assertEqual((snap.tables[0].name, snap.tables[0].rows, snap.tables[0].size_mb), ("Orders", 1500, 12.5))
        self.assertEqual(len(snap.requests), 2)
        self.assertEqual(snap.requests[1]["blocked_by"], 55)
        self.assertEqual(snap.locks[0]["status"], "WAIT")
        self.assertEqual(snap.notes, [])
        m = {(k, key): v for k, key, v in metrics_from(snap)}
        self.assertEqual(m[("db_size_mb", "Sales")], 120.0)
        self.assertEqual(m[("table_rows", "Sales.dbo.Orders")], 1500.0)
        self.assertEqual(m[("blocked_requests", "")], 1.0)
        self.assertEqual(m[("available", "")], 1.0)

    def test_without_server_state_activity_is_not_shown(self):
        snap = collect(self.conn(self.full_server(perms=0)), "Sales")
        self.assertIsNone(snap.requests)
        self.assertIsNone(snap.locks)
        self.assertTrue(any("VIEW SERVER STATE" in n for n in snap.notes))
        self.assertFalse(any(k == "active_requests" for k, _key, _v in metrics_from(snap)))

    def test_fallbacks_when_catalog_views_are_denied(self):
        s = FakeServer()
        s.when("HAS_PERMS_BY_NAME", columns=["p"], rows=[(0,)])
        s.when("sys.master_files", on_execute=always(DENIED))
        s.when("sys.database_files", columns=["d", "l"], rows=[(8.0, 2.0)])
        s.when("sys.allocation_units", on_execute=always(DENIED))
        s.when("SUM(p.rows)", columns=["s", "t", "r"], rows=[("dbo", "Orders", 7)])
        snap = collect(self.conn(s), "Sales")
        self.assertEqual(snap.db_sizes, [("Sales", 8.0, 2.0)])
        self.assertEqual((snap.tables[0].rows, snap.tables[0].size_mb), (7, None))
        self.assertTrue(any("VIEW ANY DEFINITION" in n for n in snap.notes))
        self.assertTrue(any("только число строк" in n for n in snap.notes))
        self.assertFalse(any(k == "table_mb" for k, _key, _v in metrics_from(snap)))

    def test_everything_denied_and_empty_answers(self):
        s = FakeServer()
        for q in ("sys.master_files", "sys.database_files", "sys.allocation_units", "SUM(p.rows)", "COUNT(*) FROM"):
            s.when(q, on_execute=always(DENIED))
        snap = collect(self.conn(s), "Sales")                    # HAS_PERMS без ответа -> не известно
        self.assertIsNone(snap.db_sizes)
        self.assertIsNone(snap.tables)
        self.assertIsNone(snap.server_state)
        self.assertGreaterEqual(len(snap.notes), 3)

    def test_unavailable_server_and_no_database(self):
        s = FakeServer()
        c = self.conn(s)
        s.when("SELECT 1", on_execute=always(FakeOdbcError("08S01", "Communication link failure")))
        snap = collect(c, "Sales")
        self.assertFalse(snap.probe.ok)
        self.assertEqual(metrics_from(snap), [("available", "", 0.0)])
        self.assertIn("недоступен", snap.notes[0])
        snap2 = collect(self.conn(self.full_server()), "")
        self.assertIsNone(snap2.tables)
        self.assertTrue(any("Выберите базу" in n for n in snap2.notes))


if __name__ == "__main__":
    unittest.main()
