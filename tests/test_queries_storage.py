import unittest

from storage.db import AppDatabase, SCHEMA_VERSION
from storage.queries import FavoriteQueries, QueryHistory
from storage.settings import SettingsStore


class QueryStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = AppDatabase(":memory:")

    def test_schema_version_includes_editor_tables(self):
        self.assertGreaterEqual(SCHEMA_VERSION, 2)
        names = {r["name"] for r in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"query_history", "favorite_queries"} <= names)

    def test_upgrade_from_v1_keeps_data(self):
        import os, sqlite3, tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "old.sqlite3")
            from storage import db as dbmod
            raw = sqlite3.connect(path)
            raw.executescript("BEGIN;" + dbmod._MIGRATIONS[0] + "PRAGMA user_version = 1;COMMIT;")
            raw.execute("INSERT INTO settings(key, value) VALUES('theme', '\"dark\"')")
            raw.commit()
            raw.close()
            db = AppDatabase(path)
            self.assertEqual(db.schema_version, SCHEMA_VERSION)
            self.assertEqual(SettingsStore(db).get("theme"), "dark")
            QueryHistory(db).add("SELECT 1", "ok")
            db.close()

    def test_history_add_search_trim_and_redact(self):
        h = QueryHistory(self.db, limit=3)
        for i in range(5):
            h.add(f"SELECT {i}", "ok", connection_name="srv", database_name="db", duration_ms=i, row_count=i)
        rows = h.recent()
        self.assertEqual([r.sql for r in rows], ["SELECT 4", "SELECT 3", "SELECT 2"])
        self.assertEqual(rows[0].connection_name, "srv")
        self.assertEqual([r.sql for r in h.recent(search="3")], ["SELECT 3"])
        h.add("CREATE LOGIN x WITH PASSWORD = 'Sup3rSecret'", "ok")
        stored = h.recent(1)[0].sql
        self.assertNotIn("Sup3rSecret", stored)
        h.add("SELECT '50%'", "ok")
        self.assertEqual(len(h.recent(search="%")), 1)       # символ % в поиске не работает как маска

    def test_history_delete_and_clear(self):
        h = QueryHistory(self.db)
        a = h.add("SELECT 1", "ok")
        h.add("SELECT 2", "error", message="boom")
        h.delete(a)
        self.assertEqual([r.sql for r in h.recent()], ["SELECT 2"])
        h.clear()
        self.assertEqual(h.recent(), [])

    def test_favorites(self):
        f = FavoriteQueries(self.db)
        fid = f.add("", "SELECT * FROM Orders\nWHERE 1=1")
        f.add("Alpha", "SELECT 1")
        items = f.list()
        self.assertEqual([i.title for i in items], ["Alpha", "SELECT * FROM Orders"])   # заголовок по умолчанию — 1-я строка
        f.rename(fid, "Заказы")
        self.assertIn("Заказы", [i.title for i in f.list()])
        f.rename(fid, "   ")                                                            # пустое имя игнорируется
        self.assertIn("Заказы", [i.title for i in f.list()])
        f.delete(fid)
        self.assertEqual(len(f.list()), 1)

    def test_bool_and_new_settings(self):
        s = SettingsStore(self.db)
        self.assertIs(s.get("query_history_enabled"), True)
        self.assertIs(s.get("export_formula_guard"), True)
        s.set("query_history_enabled", False)
        self.assertIs(s.get("query_history_enabled"), False)
        for key, bad in (("query_history_enabled", 1), ("editor_font_size", 3), ("export_csv_delimiter", "|"),
                         ("query_history_limit", 5)):
            with self.assertRaises(ValueError):
                s.set(key, bad)
        self.assertEqual(s.get("export_csv_delimiter"), ";")


if __name__ == "__main__":
    unittest.main()
