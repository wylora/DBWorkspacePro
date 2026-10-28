import datetime as dt
import decimal
import unittest
import uuid

from database.completion import (ObjectEntry, SchemaSnapshot, complete, extract_table_refs, quote_if_needed)
from database.schema_cache import SchemaCache
from database.metadata import MetadataReader
from tests.fakes import FakeServer
from database.profile import ConnectionProfile
from utils.formatting import format_cell, sort_key

SNAP = SchemaSnapshot(
    objects=[ObjectEntry("dbo", "Orders", "table"), ObjectEntry("dbo", "Customers", "table"),
             ObjectEntry("sales", "Items", "view"), ObjectEntry("dbo", "Order Lines", "table")],
    columns={("dbo", "Orders"): [("Id", "int"), ("CustomerId", "int"), ("Total", "decimal(18,2)")],
             ("dbo", "Customers"): [("Id", "int"), ("Name", "nvarchar(50)")],
             ("dbo", "Order Lines"): [("Qty", "int")]})


def names(sql, pos=None):
    start, items = complete(sql, len(sql) if pos is None else pos, SNAP)
    return start, [c.insert for c in items]


class CompletionTests(unittest.TestCase):
    def test_alias_columns(self):
        sql = "SELECT o. FROM Orders o JOIN Customers AS c ON c.Id = o.CustomerId"
        self.assertEqual(names(sql, 9)[1], ["Id", "CustomerId", "Total"])

    def test_alias_prefix_filter(self):
        sql = "SELECT c.Na FROM dbo.Customers c"
        start, items = names(sql, 11)
        self.assertEqual((start, items), (9, ["Name"]))

    def test_unqualified_columns_objects_keywords(self):
        _, items = names("SELECT Tot FROM Orders", 10)
        self.assertEqual(items, ["Total"])
        _, items = names("SELECT * FROM Cus")
        self.assertEqual(items, ["Customers"])
        _, items = names("SEL")
        self.assertIn("SELECT", items)

    def test_schema_qualified_objects_and_quoting(self):
        _, items = names("SELECT * FROM sales.")
        self.assertEqual(items, ["Items"])
        _, items = names("SELECT * FROM Order")
        self.assertIn("[Order Lines]", items)
        _, items = names("SELECT * FROM It")
        self.assertEqual(items, ["sales.Items"])  # не-dbo схема вставляется вместе с именем схемы
        _, items = names("SELECT * FROM sal")
        self.assertIn("sales", items)

    def test_two_part_qualifier(self):
        _, items = names("SELECT * FROM dbo.Orders.")
        self.assertEqual(items, ["Id", "CustomerId", "Total"])

    def test_nothing_inside_string_or_comment(self):
        self.assertEqual(names("SELECT 'Cus")[1], [])
        self.assertEqual(names("-- Cus")[1], [])
        self.assertEqual(names("SELECT 1 -- Cus")[1], [])
        self.assertIn("Customers", names("-- c\nSELECT * FROM Cus")[1])   # на следующей строке комментарий уже закончен
        self.assertEqual(names("SELECT ")[1], [])  # пустой префикс без квалификатора

    def test_extract_refs(self):
        refs = extract_table_refs("SELECT * FROM dbo.Orders o, Customers c JOIN [Order Lines] AS l ON 1=1", 5)
        self.assertEqual([(r.alias, r.schema, r.name) for r in refs],
                         [("o", "dbo", "Orders"), ("c", "", "Customers"), ("l", "", "Order Lines")])

    def test_quote_if_needed(self):
        self.assertEqual(quote_if_needed("Name"), "Name")
        self.assertEqual(quote_if_needed("Order"), "[Order]")        # зарезервированное слово
        self.assertEqual(quote_if_needed("a b"), "[a b]")
        self.assertEqual(quote_if_needed("a]b"), "[a]]b]")

    def test_no_snapshot_still_offers_keywords(self):
        _, items = complete("SEL", 3, None)
        self.assertTrue(any(c.text == "SELECT" for c in items))


class SchemaCacheTests(unittest.TestCase):
    def test_load_builds_snapshot(self):
        server = FakeServer()
        server.when("sys.tables", columns=list("abcdef"), rows=[("dbo", "T1", 1, "USER_TABLE", None, "")])
        server.when("sys.views", columns=list("abcdef"), rows=[("dbo", "V1", 2, "VIEW", None, "")])
        server.when("sys.columns c", columns=list("abcdefg"), rows=[
            ("dbo", "T1", "Id", "int", 4, 10, 0), ("dbo", "T1", "Name", "nvarchar", 100, 0, 0)])
        conn = server.make_connection(ConnectionProfile(id="p", name="n", server="s"))
        conn.connect()
        cache = SchemaCache()
        snap = cache.load(MetadataReader(conn), "k", "db")
        self.assertEqual({(o.name, o.kind) for o in snap.objects}, {("T1", "table"), ("V1", "view")})
        self.assertEqual(snap.columns[("dbo", "T1")], [("Id", "int"), ("Name", "nvarchar(50)")])
        self.assertIs(cache.get("k", "db"), snap)
        cache.invalidate("k")
        self.assertIsNone(cache.get("k", "db"))


class FormattingTests(unittest.TestCase):
    def test_format_cell(self):
        self.assertEqual(format_cell(None), "NULL")
        self.assertEqual(format_cell(dt.datetime(2024, 1, 2, 3, 4, 5)), "2024-01-02 03:04:05")
        self.assertEqual(format_cell(dt.datetime(2024, 1, 2, 3, 4, 5, 120000)), "2024-01-02 03:04:05.12")
        self.assertEqual(format_cell(dt.date(2024, 1, 2)), "2024-01-02")
        self.assertEqual(format_cell(decimal.Decimal("1E+3")), "1000")
        self.assertEqual(format_cell(b"\x01\xab"), "0x01AB")
        self.assertTrue(format_cell(b"x" * 100).endswith("…"))
        self.assertEqual(format_cell(True), "1")
        self.assertEqual(format_cell(uuid.UUID(int=255)), "00000000-0000-0000-0000-0000000000FF")
        self.assertEqual(len(format_cell("x" * 1000)), 501)

    def test_sort_key_mixed_types_never_raises(self):
        values = [3, None, "b", 2.5, decimal.Decimal("1"), "A", dt.date(2020, 1, 1), b"z", True, float("nan")]
        ordered = sorted(values, key=sort_key)
        self.assertIsNone(ordered[-1])
        nums = [v for v in ordered if isinstance(v, (int, float, decimal.Decimal)) and not isinstance(v, bool)
                and v == v]
        self.assertEqual(nums, [decimal.Decimal("1"), 2.5, 3])

    def test_sort_key_old_dates(self):
        self.assertLess(sort_key(dt.date(1900, 1, 1)), sort_key(dt.date(2000, 1, 1)))


if __name__ == "__main__":
    unittest.main()
