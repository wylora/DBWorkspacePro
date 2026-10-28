import unittest
from datetime import datetime

from database.connection import ConnectionState
from database.errors import ConnectionFailedError, ConnectionLimitError, MetadataError
from database.manager import ConnectionManager
from database.metadata import MetadataReader
from database.profile import ConnectionProfile
from tests.fakes import FakeOdbcError, FakeServer


def profile(pid="p1"):
    return ConnectionProfile(id=pid, name=pid, server="srv")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.mgr = ConnectionManager(2, self.server.make_connection)

    def test_primary_reused(self):
        a = self.mgr.connect_profile(profile())
        b = self.mgr.connect_profile(profile())
        self.assertIs(a, b)
        self.assertEqual(self.server.connect_calls, 1)

    def test_limit_enforced(self):
        self.mgr.connect_profile(profile("a"))
        self.mgr.connect_profile(profile("b"))
        with self.assertRaises(ConnectionLimitError):
            self.mgr.connect_profile(profile("c"))
        self.assertEqual(self.mgr.count(), 2)

    def test_failed_connect_frees_slot(self):
        self.server.connect_errors.append(FakeOdbcError("08001", "TCP Provider: no host"))
        with self.assertRaises(ConnectionFailedError):
            self.mgr.connect_profile(profile("a"))
        self.assertEqual(self.mgr.count(), 0)
        self.mgr.connect_profile(profile("b"))
        self.mgr.connect_profile(profile("c"))  # слот не «утёк»
        self.assertEqual(self.mgr.count(), 2)

    def test_lease_closes_after_success_and_error(self):
        with self.mgr.lease(profile()) as c:
            self.assertEqual(self.mgr.count(), 1)
        self.assertEqual(self.mgr.count(), 0)
        self.assertEqual(c.state, ConnectionState.DISCONNECTED)
        with self.assertRaises(RuntimeError):
            with self.mgr.lease(profile()):
                raise RuntimeError("boom")
        self.assertEqual(self.mgr.count(), 0)
        self.assertEqual(self.server.open_raws(), [])

    def test_leases_are_separate_connections(self):
        with self.mgr.lease(profile()) as a, self.mgr.lease(profile()) as b:
            self.assertIsNot(a, b)
            with self.assertRaises(ConnectionLimitError):
                with self.mgr.lease(profile()):
                    pass

    def test_disconnect_and_close_all(self):
        self.mgr.connect_profile(profile("a"))
        self.mgr.connect_profile(profile("b"))
        self.mgr.disconnect_profile("a")
        self.assertEqual(self.mgr.count(), 1)
        self.mgr.close_all()
        self.assertEqual(self.mgr.count(), 0)
        self.assertEqual(self.server.open_raws(), [])

    def test_reconnect_lost_primary(self):
        c = self.mgr.connect_profile(profile())
        self.server.raws[0].closed = True
        c._raw = None
        c._state = ConnectionState.LOST
        again = self.mgr.connect_profile(profile())
        self.assertIs(c, again)
        self.assertTrue(again.is_connected)

    def test_state_listener_forwarded(self):
        events = []
        self.mgr.add_state_listener(lambda c, o, n: events.append(n))
        self.mgr.connect_profile(profile())
        self.assertIn(ConnectionState.CONNECTED, events)


class MetadataTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.conn = self.server.make_connection(profile())
        self.conn.connect()
        self.reader = MetadataReader(self.conn)

    def test_databases(self):
        self.server.when("sys.databases", columns=["name"], rows=[("Sales",), ("HR",)])
        self.assertEqual(self.reader.list_databases(), ["Sales", "HR"])

    def test_tables_use_quoted_db_name(self):
        self.server.when("sys.tables", columns=list("abcdef"), rows=[
            ("dbo", "Orders", 101, "USER_TABLE", datetime(2024, 1, 1), "")])
        res = self.reader.list_objects("Sa]les", "table")
        sql = self.server.executed[-1][0]
        self.assertIn("[Sa]]les].sys.tables", sql)
        self.assertEqual(res.items[0].full_name, "dbo.Orders")
        self.assertEqual(res.items[0].object_id, 101)

    def test_injection_attempt_in_db_name_is_quoted(self):
        self.server.when("sys.views", columns=list("abcdef"), rows=[])
        self.reader.list_objects("x].sys.views; DROP TABLE t;--", "view")
        sql = self.server.executed[-1][0]
        self.assertIn("[x]].sys.views; DROP TABLE t;--]", sql)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            self.reader.list_objects("db", "bogus")

    def test_table_details(self):
        self.server.when("FROM [db].sys.columns c", columns=list("0123456789"), rows=[
            (1, "Id", "int", 4, 10, 0, 0, 1, 0, None),
            (2, "Name", "nvarchar", 100, 0, 0, 1, 0, 0, None),
            (3, "Total", "decimal", 9, 18, 2, 0, 0, 0, "((0))")])
        self.server.when("sys.indexes", columns=list("012345678"), rows=[
            (1, "PK_Orders", "CLUSTERED", 1, 1, 0, 0, 0, "Id"),
            (2, "IX_Name", "NONCLUSTERED", 0, 0, 0, 0, 1, "Name"),
            (2, "IX_Name", "NONCLUSTERED", 0, 0, 0, 1, 0, "Total")])
        self.server.when("sys.foreign_keys", columns=list("012345678"), rows=[
            (500, "FK_Orders_Cust", "NO_ACTION", "NO_ACTION", 0, "CustId", "dbo", "Customers", "Id")])
        self.server.when("sys.partitions", columns=["n"], rows=[(1234,)])
        d = self.reader.get_table_details("db", 101)
        self.assertEqual([c.data_type for c in d.columns], ["int", "nvarchar(50)", "decimal(18,2)"])
        self.assertTrue(d.columns[0].is_primary_key)
        self.assertFalse(d.columns[1].is_primary_key)
        self.assertEqual(d.columns[2].default, "((0))")
        self.assertEqual(d.indexes[0].columns, ("Id",))
        self.assertEqual(d.indexes[1].columns, ("Name DESC",))
        self.assertEqual(d.indexes[1].included_columns, ("Total",))
        self.assertEqual(d.foreign_keys[0].ref_table, "Customers")
        self.assertEqual(d.row_estimate, 1234)
        self.assertEqual(d.warnings, [])
        # object_id передан параметром, а не склеен в SQL
        self.assertTrue(all(p == (101,) for s, p in self.server.executed if "sys.columns c" in s))

    def test_permission_error_on_optional_parts_becomes_warning(self):
        self.server.when("FROM [db].sys.columns c", columns=list("0123456789"), rows=[
            (1, "Id", "int", 4, 10, 0, 0, 0, 0, None)])
        denied = FakeOdbcError("42000", "[42000] The SELECT permission was denied (229)")
        self.server.when("sys.indexes", errors=[denied])
        self.server.when("sys.foreign_keys", errors=[FakeOdbcError("42000", "permission was denied (229)")])
        d = self.reader.get_table_details("db", 1)
        self.assertEqual(len(d.columns), 1)
        self.assertEqual(len(d.warnings), 2)
        self.assertIn("Недостаточно прав", d.warnings[0])

    def test_failure_on_main_query_raises_metadata_error(self):
        self.server.when("sys.databases", errors=[FakeOdbcError("42000", "permission was denied (229)")])
        with self.assertRaises(MetadataError):
            self.reader.list_databases()

    def test_definition_none_when_no_rights(self):
        self.server.when("sys.sql_modules", columns=["d"], rows=[(None,)])
        self.assertIsNone(self.reader.get_definition("db", 7))

    def test_object_list_truncation(self):
        self.server.when("sys.tables", columns=list("abcdef"),
                         rows=[("dbo", f"T{i}", i, "USER_TABLE", None, "") for i in range(5)])
        reader = MetadataReader(self.conn, max_objects=3)
        res = reader.list_objects("db", "table")
        self.assertEqual(len(res.items), 3)
        self.assertTrue(res.truncated)


if __name__ == "__main__":
    unittest.main()
