import datetime as dt
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from bootstrap import create_services
from comparison.data_compare import CompareError, DataCompareRequest, TableRef, compare_data
from comparison.data_diff import diff_tables
from comparison.report import data_diff_doc, schema_diff_doc
from comparison.schema import Column, ForeignKey, Index, Module, SchemaModel, Table, read_schema
from comparison.schema_diff import compare_schemas, normalize_definition
from comparison.sync_script import create_or_alter, sql_literal, sync_data_script, sync_schema_script
from database.profile import ConnectionProfile
from database.sql_guard import RiskLevel, analyze, split_batches
from reports.adhoc import save_doc
from security.credentials import InMemoryCredentialStore
from tests.fakes import FakeOdbcError, FakeServer


def always(err):
    def raiser(cursor):
        raise err
    return raiser


def model(db="A", tables=(), modules=()):
    m = SchemaModel("srv", db, "2026-10-20T10:00:00")
    for t in tables:
        m.tables[SchemaModel.key(t.schema, t.name)] = t
    for x in modules:
        m.modules[SchemaModel.key(x.schema, x.name, x.kind)] = x
    return m


def orders(**over):
    cols = [Column("Id", "int", False, True, 1, 1, ordinal=1), Column("Total", "decimal(18,2)", False, default="((0))",
                                                                       ordinal=2, default_name="DF__Orders__Tot__1A2B"),
            Column("Note", "nvarchar(100)", True, ordinal=3)]
    t = Table("dbo", "Orders", cols, [Index("PK__Orders__3214EC07", "CLUSTERED", True, True, False, ["Id"]),
                                      Index("IX_Orders_Total", "NONCLUSTERED", False, False, False, ["Total DESC"], ["Note"])],
              [ForeignKey("FK_Orders_Cust", ["CustId"], "dbo", "Customers", ["Id"])])
    for k, v in over.items():
        setattr(t, k, v)
    return t


class SchemaReaderTests(unittest.TestCase):
    def test_bulk_read_builds_model_and_notes_missing_rights(self):
        s = FakeServer()
        s.when("FROM [Sales].sys.tables t JOIN", columns=["o", "s", "n"], rows=[(1, "dbo", "Orders"), (2, "dbo", "Customers")])
        s.when("FROM [Sales].sys.objects o LEFT JOIN", columns=list("abcdefgh"),
               rows=[(10, "dbo", "vOrders", "V ", "VIEW", "CREATE VIEW dbo.vOrders AS SELECT 1 AS x", "", ""),
                     (11, "dbo", "pGet", "P ", "SQL_STORED_PROCEDURE", None, "", ""),
                     (12, "dbo", "trOrders", "TR", "SQL_TRIGGER", "CREATE TRIGGER trOrders ON dbo.Orders AFTER INSERT AS SELECT 1",
                      "dbo", "Orders")])
        s.when("tr.parent_class = 0", columns=["o", "n", "d"], rows=[(13, "ddl_audit", "CREATE TRIGGER ddl_audit ON DATABASE ...")])
        s.when("FROM [Sales].sys.columns c", columns=list("abcdefghijklmnop"),
               rows=[(1, 1, "Id", "int", 4, 10, 0, False, True, False, None, None, None, 1, 1, None),
                     (1, 2, "Name", "nvarchar", 100, 0, 0, True, False, False, "(N'')", "Cyrillic_General_CI_AS", None, None,
                      None, "DF_Name"),
                     (2, 1, "Id", "int", 4, 10, 0, False, False, False, None, None, None, None, None, None),
                     (10, 1, "x", "int", 4, 10, 0, True, False, False, None, None, None, None, None, None)])
        s.when("FROM [Sales].sys.indexes i", columns=list("abcdefghijk"),
               rows=[(1, 1, "PK_O", "CLUSTERED", True, True, False, None, False, False, "Id"),
                     (1, 2, "IX_N", "NONCLUSTERED", False, False, False, "([Name] IS NOT NULL)", False, True, "Name")])
        s.when("FROM [Sales].sys.foreign_keys fk", columns=list("abcdefghij"),
               rows=[(1, 99, "FK_O_C", "CASCADE", "NO_ACTION", False, "Id", "dbo", "Customers", "Id")])
        s.when("FROM [Sales].sys.parameters p", columns=list("abcdefgh"), rows=[(11, 1, "@id", "int", 4, 10, 0, False)])
        s.when("sys.extended_properties", on_execute=always(FakeOdbcError("42000", "[42000] denied (229)")))
        s.when("FROM [Sales].sys.partitions p", columns=["o", "r"], rows=[(1, 1500)])
        conn = s.make_connection(ConnectionProfile(id="p", name="n", server="s"))
        conn.connect()
        stages = []
        m = read_schema(conn, "Sales", server="srv", progress=lambda st, f: stages.append(st))
        o = m.tables["dbo.orders"]
        self.assertEqual([c.name for c in o.columns], ["Id", "Name"])
        self.assertEqual((o.columns[0].identity, o.columns[0].seed), (True, 1))
        self.assertEqual((o.columns[1].type, o.columns[1].default, o.columns[1].default_name), ("nvarchar(50)", "(N'')", "DF_Name"))
        self.assertEqual((o.pk.columns, o.indexes[1].columns, o.indexes[1].filter), (["Id"], ["Name DESC"], "([Name] IS NOT NULL)"))
        self.assertEqual((o.fks[0].ref_table, o.fks[0].on_delete), ("Customers", "CASCADE"))
        self.assertEqual(o.rows, 1500)
        self.assertEqual(m.modules["view:dbo.vorders"].columns[0].name, "x")
        self.assertEqual(m.modules["procedure:dbo.pget"].params[0].name, "@id")
        self.assertEqual(m.modules["trigger:dbo.trorders"].parent, "dbo.Orders")
        self.assertEqual(m.modules["trigger:.ddl_audit"].parent, "(база данных)")
        self.assertTrue(any("Описания" in w for w in m.warnings))
        self.assertTrue(any("Определения недоступны для объектов: 1" in w for w in m.warnings))
        self.assertEqual(SchemaModel.loads(m.dumps()).to_dict(), m.to_dict())          # снимок без потерь
        self.assertTrue(stages[-1].startswith("Чтение структуры завершено"))
        self.assertEqual(list(m.only(["Orders"]).tables), ["dbo.orders"])

    def test_tables_query_failure_is_fatal(self):
        s = FakeServer()
        s.when("sys.tables t JOIN", on_execute=always(FakeOdbcError("42000", "[42000] Database 'X' does not exist (911)")))
        conn = s.make_connection(ConnectionProfile(id="p", name="n", server="s"))
        conn.connect()
        from comparison.schema import SchemaReadError
        with self.assertRaises(SchemaReadError):
            read_schema(conn, "X")


class SchemaDiffTests(unittest.TestCase):
    def test_identical_despite_generated_constraint_names_and_whitespace(self):
        a = orders()
        b = orders()
        b.indexes[0] = Index("PK__Orders__99999999", "CLUSTERED", True, True, False, ["id"])
        b.columns[1].default = "(0)"
        va = Module("dbo", "v", "view", definition="CREATE VIEW v AS\n  SELECT 1")
        vb = Module("dbo", "v", "view", definition="CREATE VIEW v AS SELECT 1  ")
        d = compare_schemas(model("A", [a], [va]), model("B", [b], [vb]))
        self.assertTrue(d.identical, [x.__dict__ for x in d.items])
        self.assertEqual((d.equal_tables, d.equal_modules), (1, 1))
        self.assertFalse(compare_schemas(model("A", [orders()]), model("B", [b]), compare_names=True).identical)

    def test_all_kinds_of_differences(self):
        a = orders()
        b = orders()
        b.columns[1] = Column("Total", "decimal(10,2)", True, ordinal=2, default="((1))", default_name="DF_old")
        b.columns = [c for c in b.columns if c.name != "Note"] + [Column("Legacy", "int", True, ordinal=9)]
        b.indexes[1] = Index("IX_Orders_Total", "NONCLUSTERED", True, False, False, ["Total"])
        b.fks[0] = ForeignKey("FK_x", ["CustId"], "dbo", "Customers", ["Id"], on_delete="CASCADE")
        extra = Table("dbo", "Old", [Column("Id", "int", False, ordinal=1)])
        new = Table("dbo", "New", [Column("Id", "int", False, ordinal=1)])
        pa = Module("dbo", "p", "procedure", definition="CREATE PROCEDURE p AS SELECT 2")
        pb = Module("dbo", "p", "procedure", definition="CREATE PROCEDURE p AS SELECT 1")
        hidden = Module("dbo", "h", "function", definition=None)
        d = compare_schemas(model("A", [a, new], [pa, hidden]), model("B", [b, extra], [pb, Module("dbo", "h", "function", definition="x")]))
        got = {(x.category, x.object, x.status) for x in d.items}
        for exp in [("table", "dbo.New", "only_source"), ("table", "dbo.Old", "only_target"),
                    ("column", "dbo.Orders.Note", "only_source"), ("column", "dbo.Orders.Legacy", "only_target"),
                    ("column", "dbo.Orders.Total", "changed"), ("index", "dbo.Orders: IX_Orders_Total", "changed"),
                    ("fk", "dbo.Orders: FK_Orders_Cust", "changed"), ("procedure", "dbo.p", "changed"),
                    ("function", "dbo.h", "unknown")]:
            self.assertIn(exp, got)
        total = next(x for x in d.items if x.object == "dbo.Orders.Total")
        self.assertEqual(len(total.details), 3)                               # тип, NULL, значение по умолчанию
        self.assertIn("Различий:", d.summary())
        self.assertEqual(normalize_definition(" a \n\n b "), "a b")


class SyncScriptTests(unittest.TestCase):
    def script(self, **kw):
        a, b = orders(), orders()
        b.columns = [c for c in b.columns if c.name != "Note"]
        b.columns[1] = Column("Total", "decimal(10,2)", True, ordinal=2, default="((1))", default_name="DF_old")
        b.indexes = [b.indexes[0]]
        b.fks = []
        new = Table("dbo", "New Table", [Column("Id", "int", False, True, 1, 1, ordinal=1),
                                         Column("Req", "nvarchar(10)", False, ordinal=2)],
                    [Index("PK_New", "CLUSTERED", True, True, False, ["Id"])])
        new.columns.append(Column("Calc", "int", True, computed="([Id]*(2))", ordinal=3))
        b2 = orders()
        b2.columns = [c for c in b2.columns if c.name != "Note"]
        req = Table("dbo", "Orders", a.columns + [Column("Must", "int", False, ordinal=4)], a.indexes, a.fks)
        old = Table("dbo", "Old", [Column("Id", "int", False, ordinal=1)])
        view = Module("dbo", "vX", "view", definition="/* CREATE */ create view dbo.vX as select 1 as a")
        hidden = Module("dbo", "pH", "procedure", definition=None)
        d = compare_schemas(model("A", [req, new], [view, hidden]), model("B", [b, old]))
        return sync_schema_script(d, **kw), d

    def test_script_content_and_safety(self):
        s, d = self.script()
        t = s.text
        self.assertIn("CREATE TABLE [dbo].[New Table] (", t)
        self.assertIn("[Id] int IDENTITY(1,1) NOT NULL", t)
        self.assertIn("[Calc] AS ([Id]*(2))", t)
        self.assertIn("CONSTRAINT [PK_New] PRIMARY KEY CLUSTERED ([Id])", t)
        self.assertIn("ALTER TABLE [dbo].[Orders] ADD [Note] nvarchar(100) NULL;", t)
        self.assertIn("ALTER TABLE [dbo].[Orders] ADD [Must] int NULL;", t)            # NOT NULL без default -> NULL
        self.assertTrue(any("Must" in w for w in s.warnings))
        self.assertIn("ALTER TABLE [dbo].[Orders] ALTER COLUMN [Total] decimal(18,2) NOT NULL;", t)
        self.assertIn("DROP CONSTRAINT [DF_old]", t)
        self.assertIn("ADD DEFAULT ((0)) FOR [Total]", t)
        self.assertIn("CREATE NONCLUSTERED INDEX [IX_Orders_Total] ON [dbo].[Orders] ([Total] DESC) INCLUDE ([Note]);", t)
        self.assertIn("FOREIGN KEY ([CustId]) REFERENCES [dbo].[Customers] ([Id])", t)
        self.assertIn("/* CREATE */ CREATE OR ALTER view dbo.vX", t)
        self.assertIn("-- DROP TABLE [dbo].[Old];", t)                              # удаление закомментировано
        self.assertNotIn("\nDROP TABLE", t)
        self.assertTrue(any("pH" in m for m in s.manual))
        self.assertEqual(analyze(t).level, RiskLevel.DANGEROUS)                     # ALTER/CREATE — изменение схемы
        batches, errs = split_batches(t)
        self.assertEqual(errs, [])
        self.assertEqual(len(batches), s.statements)
        self.assertTrue(all(len(b.text.strip()) > 0 for b in batches))

    def test_drops_only_when_requested_and_ordered(self):
        s, _ = self.script(include_drops=True)
        self.assertIn("\nDROP TABLE [dbo].[Old];", s.text)
        self.assertIn("УДАЛЕНИЕ", s.text)

    def test_identical_schemas_give_empty_script(self):
        d = compare_schemas(model("A", [orders()]), model("B", [orders()]))
        s = sync_schema_script(d)
        self.assertEqual(s.statements, 0)
        self.assertIn("Изменений, которые можно выполнить автоматически, нет", s.text)

    def test_literals_and_data_script(self):
        self.assertEqual([sql_literal(v) for v in (None, True, 7, 2.5, Decimal("1.10"), "O'Hara", dt.date(2024, 1, 2),
                                                   dt.datetime(2024, 1, 2, 3, 4, 5), b"\x00\xff", float("nan"))],
                         ["NULL", "1", "7", "2.5", "1.10", "N'O''Hara'", "'2024-01-02'", "'2024-01-02T03:04:05'", "0x00FF", "NULL"])
        self.assertEqual(sql_literal("x'; DROP TABLE t; --"), "N'x''; DROP TABLE t; --'")
        old = (["Id", "Name"], [(1, "a"), (2, "b"), (3, "c")])
        new = (["Id", "Name"], [(1, "a"), (2, "B'"), (4, "d")])
        d = diff_tables(*old, *new, ["Id"])
        s = sync_data_script("dbo", "T", ["Id", "Name"], ["Id"], d, identity_columns=["Id"])
        self.assertIn("SET IDENTITY_INSERT [dbo].[T] ON;", s.text)
        self.assertIn("INSERT INTO [dbo].[T] ([Id], [Name]) VALUES (4, N'd');", s.text)
        self.assertIn("UPDATE [dbo].[T] SET [Name] = N'B''' WHERE [Id] = 2;", s.text)
        self.assertIn("-- DELETE FROM [dbo].[T] WHERE [Id] = 3;", s.text)
        self.assertEqual(s.statements, 2)
        s2 = sync_data_script("dbo", "T", ["Id", "Name"], ["Id"], d, include_deletes=True)
        self.assertIn("\nDELETE FROM [dbo].[T] WHERE [Id] = 3;", s2.text)
        capped = diff_tables(["Id"], [], ["Id"], [(i,) for i in range(20)], ["Id"], max_items=5)
        self.assertTrue(sync_data_script("dbo", "T", ["Id"], ["Id"], capped).warnings)

    def test_create_or_alter_edge_cases(self):
        self.assertEqual(create_or_alter("CREATE OR ALTER PROC p AS SELECT 1"), "CREATE OR ALTER PROC p AS SELECT 1")
        self.assertIsNone(create_or_alter("SELECT 'CREATE'"))
        self.assertTrue(create_or_alter("-- комментарий\nCREATE FUNCTION f() RETURNS int AS BEGIN RETURN 1 END")
                        .startswith("-- комментарий\nCREATE OR ALTER FUNCTION"))


class DataCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        self.svc = create_services(self.tmp.name, InMemoryCredentialStore(), self.server.make_connection)
        self.p = self.svc.profiles.save(ConnectionProfile.new("P", "srv"))

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def req(self, **kw):
        base = dict(source=TableRef(self.p.id, "A", "dbo", "T"), target=TableRef(self.p.id, "B", "dbo", "T"),
                    key_columns=["Id"])
        base.update(kw)
        return DataCompareRequest(**base)

    def test_compare_two_databases(self):
        self.server.when("FROM [A].[dbo].[T]", columns=["Id", "Name", "Sum"], rows=[(1, "a", 1.0), (2, "B", 2.0), (4, "d", 4.0)])
        self.server.when("FROM [B].[dbo].[T]", columns=["Id", "Name", "Sum"], rows=[(1, "a", 1.0), (2, "b", 2.0), (3, "c", 3.0)])
        self.server.when("identity_columns", columns=["n"], rows=[("Id",)])
        r = compare_data(self.svc.profiles, self.req())
        d = r.diff
        self.assertEqual((d.added_total, d.removed_total, d.changed_total), (1, 1, 1))  # added — нет в целевой
        self.assertEqual(d.added[0][0], 4)
        self.assertEqual(r.identity_columns, ["Id"])
        self.assertEqual(self.svc.manager.count(), 0)
        doc = data_diff_doc(r)
        with tempfile.TemporaryDirectory() as out:
            res = save_doc(doc, Path(out), "diff", ["docx", "xlsx"])
            self.assertEqual(res.errors, {})
            self.assertEqual(len(res.files), 2)

    def test_limits_and_errors(self):
        self.server.when("FROM [A].[dbo].[T]", columns=["Id"], rows=[(i,) for i in range(20)])
        with self.assertRaises(CompareError) as ctx:
            compare_data(self.svc.profiles, self.req(max_rows=10))
        self.assertIn("больше 10 строк", str(ctx.exception))
        with self.assertRaises(CompareError):
            compare_data(self.svc.profiles, self.req(key_columns=[]))
        self.server.handlers.clear()
        self.server.when("FROM [A].[dbo].[T]", columns=["Id"], rows=[(1,), (1,)])
        self.server.when("FROM [B].[dbo].[T]", columns=["Id"], rows=[(1,)])
        with self.assertRaises(CompareError) as ctx:
            compare_data(self.svc.profiles, self.req())
        self.assertIn("неуникален", str(ctx.exception))

    def test_case_insensitive_keys_and_selected_columns(self):
        self.server.when("FROM [A].[dbo].[T]", columns=["Code", "V"], rows=[("abc", 1)])
        self.server.when("FROM [B].[dbo].[T]", columns=["Code", "V"], rows=[("ABC", 1)])
        r = compare_data(self.svc.profiles, self.req(key_columns=["Code"], compare_columns=["V"]))
        self.assertFalse(r.diff.has_changes)
        self.assertIn("SELECT [Code], [V] FROM [A].[dbo].[T]", [q for q, _ in self.server.executed])
        r2 = compare_data(self.svc.profiles, self.req(key_columns=["Code"], key_casefold=False))
        self.assertEqual((r2.diff.added_total, r2.diff.removed_total), (1, 1))


class ReportsTests(unittest.TestCase):
    def test_schema_diff_report_formats(self):
        b = orders()
        b.columns = b.columns[:2]
        d = compare_schemas(model("A", [orders()]), model("B", [b]))
        with tempfile.TemporaryDirectory() as out:
            r = save_doc(schema_diff_doc(d, "srv/A", "srv/B"), Path(out), "schema", ["docx", "xlsx", "html"])
            self.assertEqual(r.errors, {})
            html = (Path(out) / "schema.html").read_text(encoding="utf-8")
            self.assertIn("dbo.Orders.Note", html)
            r2 = save_doc(schema_diff_doc(d, "a", "b"), Path(out), "schema", ["docx"], policy="new_version")
            self.assertEqual(r2.files[0].name, "schema (2).docx")


if __name__ == "__main__":
    unittest.main()
