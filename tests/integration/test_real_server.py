"""Интеграционные тесты с НАСТОЯЩИМ SQL Server — только чтение, по явному включению.

Запуск (PowerShell):
    $env:DBWP_TEST_SERVER = "localhost\\SQLEXPRESS"
    $env:DBWP_TEST_AUTH = "windows"          # или sql
    $env:DBWP_TEST_USER = "tester"           # только для sql
    $env:DBWP_TEST_PASSWORD = "..."          # только для sql (в файлы не записывайте!)
    $env:DBWP_TEST_TRUST_CERT = "1"          # необязательно: для самоподписанного сертификата тестового сервера
    python -m pytest tests/integration -v

Используйте ТЕСТОВЫЙ сервер и учётную запись с правами только на чтение.
Тесты выполняют лишь SELECT по системным представлениям и ничего не изменяют.
"""
import os
import tempfile
import unittest
from pathlib import Path

from database.connection import SqlConnection
from database.editor_session import EditorSession
from database.metadata import MetadataReader
from database.profile import AUTH_SQL, AUTH_WINDOWS, ConnectionProfile

if (os.environ.get("DBWP_TEST_SERVER") and os.environ.get("DBWP_TEST_AUTH", "").lower() == "sql"
        and not os.environ.get("DBWP_TEST_PASSWORD") and os.environ.get("DBWP_TEST_PASSWORD_PROMPT") == "1"):
    import getpass                                   # скрытый ввод (используется bat\tests_integration.bat)
    os.environ["DBWP_TEST_PASSWORD"] = getpass.getpass("Пароль SQL Server: ")
SERVER = os.environ.get("DBWP_TEST_SERVER")


@unittest.skipUnless(SERVER, "DBWP_TEST_SERVER не задан — интеграционные тесты отключены")
class RealServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        auth = AUTH_SQL if os.environ.get("DBWP_TEST_AUTH", "windows").lower() == "sql" else AUTH_WINDOWS
        profile = ConnectionProfile.new("integration", SERVER)
        profile.auth = auth
        profile.username = os.environ.get("DBWP_TEST_USER", "")
        profile.trust_server_certificate = os.environ.get("DBWP_TEST_TRUST_CERT") == "1"
        cls.conn = SqlConnection(profile, os.environ.get("DBWP_TEST_PASSWORD"))
        cls.conn.connect()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_ping_and_server_info(self):
        self.assertTrue(self.conn.ping())
        self.assertTrue(self.conn.server_info.version)

    def test_metadata_walk(self):
        reader = MetadataReader(self.conn)
        dbs = reader.list_databases()
        self.assertTrue(dbs)
        for db in dbs:
            tables = reader.list_objects(db, "table").items
            if tables:
                details = reader.get_table_details(db, tables[0].object_id)
                self.assertTrue(details.columns)
                return
        self.skipTest("нет таблиц в доступных базах")

    def test_connection_closes(self):
        self.assertTrue(self.conn.is_connected)


@unittest.skipUnless(SERVER, "DBWP_TEST_SERVER не задан — интеграционные тесты отключены")
class RealEditorSessionTests(RealServerTests):
    """Редактор: только SELECT; режим «только чтение» остаётся включённым."""

    def _session(self):
        return EditorSession(self.conn, release=lambda c: None, preview_limit=5, batch_size=2)

    def test_multiple_result_sets_and_preview_limit(self):
        rep = self._session().execute(
            "SELECT 1 AS a; SELECT TOP (20) object_id FROM sys.objects ORDER BY object_id; SELECT 3 AS c")
        self.assertIsNone(rep.error)
        rows = [r for r in rep.results if r.kind == "rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].rows, [(1,)])
        self.assertTrue(rows[1].truncated)

    def test_syntax_error_is_reported_not_raised(self):
        rep = self._session().execute("SELEC 1")
        self.assertTrue(rep.error)

    def test_modification_blocked_before_reaching_server(self):
        from database.editor_session import ModificationsNotAllowed
        with self.assertRaises(ModificationsNotAllowed):
            self._session().execute("DELETE FROM sys.objects")

    def test_full_export_streams_to_csv(self):
        with tempfile.TemporaryDirectory() as d:
            sess = EditorSession(self.conn, release=lambda c: None,
                                 sibling_factory=lambda: SqlConnection(self.conn.profile, self.conn._password))
            res = sess.export_full("SELECT name FROM sys.objects", Path(d) / "o.csv", "csv")
            self.assertGreater(res.rows, 0)


@unittest.skipUnless(SERVER, "DBWP_TEST_SERVER не задан — интеграционные тесты отключены")
class RealReportTests(RealServerTests):
    """Отчёты на настоящем сервере: все встроенные шаблоны по системным каталогам (только чтение)."""

    def _factory(self):
        from contextlib import contextmanager

        @contextmanager
        def lease():
            c = SqlConnection(self.conn.profile, self.conn._password)
            c.connect()
            try:
                yield c
            finally:
                c.close()
        return lease

    def test_builtin_templates_generate_on_real_database(self):
        from reports.generator import GenerationRequest, generate_report
        from reports.templates import TemplateStore, BUILTIN_DIR
        reader = __import__("database.metadata", fromlist=["MetadataReader"]).MetadataReader(self.conn)
        dbs = reader.list_databases()
        self.assertTrue(dbs)
        store = TemplateStore(Path(tempfile.mkdtemp()), BUILTIN_DIR)
        with tempfile.TemporaryDirectory() as out:
            for info in [i for i in store.list() if i.builtin]:
                req = GenerationRequest(store.load(info.id), {}, ["docx", "xlsx", "html"], Path(out),
                                        self._factory(), base_name=info.id, database=dbs[0], max_rows=500)
                res = generate_report(req)
                self.assertEqual(res.errors, {}, info.name)
                self.assertTrue(all(f.path.stat().st_size > 0 for f in res.files))


@unittest.skipUnless(SERVER, "DBWP_TEST_SERVER не задан — интеграционные тесты отключены")
class RealMonitoringTests(RealServerTests):
    """Мониторинг на реальном сервере: только чтение."""

    def test_collect_reports_metrics_or_honest_notes(self):
        from monitoring.collect import collect, metrics_from
        db = os.environ.get("DBWP_TEST_DATABASE", "master")
        snap = collect(self.conn, db)
        self.assertTrue(snap.probe.ok)
        self.assertIsNotNone(snap.probe.latency_ms)
        self.assertTrue(snap.db_sizes is not None or snap.notes)
        self.assertTrue(metrics_from(snap))


BACKUP_FOLDER = os.environ.get("DBWP_TEST_BACKUP_FOLDER")


@unittest.skipUnless(SERVER and BACKUP_FOLDER, "задайте DBWP_TEST_BACKUP_FOLDER (папка НА СЕРВЕРЕ) и DBWP_TEST_DATABASE")
class RealBackupTests(RealServerTests):
    """ВНИМАНИЕ: создаёт файл резервной копии (COPY_ONLY) в указанной папке сервера. Данные базы не меняются."""

    def test_copy_only_backup_and_verify(self):
        from automation.backup import BackupOptions, create_backup
        db = os.environ.get("DBWP_TEST_DATABASE", "master")
        res = create_backup(self.conn, BackupOptions(db, BACKUP_FOLDER, copy_only=True, checksum=True, verify=True))
        self.assertTrue(res.ok, res.error)
        self.assertIn(res.verified, (True, None))


@unittest.skipUnless(SERVER, "DBWP_TEST_SERVER не задан — интеграционные тесты отключены")
class RealSchemaTests(RealServerTests):
    """Чтение структуры, сравнение базы с самой собой и документация — только чтение."""

    def test_schema_read_compare_and_document(self):
        from comparison.schema import read_schema
        from comparison.schema_diff import compare_schemas
        from documentation.generator import DocOptions, build_documentation
        from reports.adhoc import save_doc
        db = os.environ.get("DBWP_TEST_DATABASE", "master")
        a = read_schema(self.conn, db, server=SERVER)
        b = read_schema(self.conn, db, server=SERVER)
        self.assertTrue(compare_schemas(a, b).identical)
        with tempfile.TemporaryDirectory() as out:
            res = save_doc(build_documentation(a, DocOptions()), Path(out), "doc", ["docx", "html"])
            self.assertEqual(res.errors, {})
