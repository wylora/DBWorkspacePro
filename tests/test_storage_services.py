import os
import sqlite3
import tempfile
import unittest

from bootstrap import create_services
from database.errors import ConfigError, ConnectionFailedError
from database.profile import AUTH_SQL, ConnectionProfile
from security.credentials import InMemoryCredentialStore, KeyringCredentialStore, profile_key
from storage.db import AppDatabase, SCHEMA_VERSION
from storage.history import OperationLog
from storage.profiles import ProfileError, ProfileRepository
from storage.settings import SettingsStore
from tests.fakes import FakeOdbcError, FakeServer
from utils.tasks import TaskState


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.db = AppDatabase(":memory:")

    def test_settings_defaults_and_roundtrip(self):
        s = SettingsStore(self.db)
        self.assertEqual(s.get("theme"), "light")
        s.set("theme", "dark")
        s.set("max_connections", 8)
        self.assertEqual((s.get("theme"), s.get("max_connections")), ("dark", 8))

    def test_settings_validation(self):
        s = SettingsStore(self.db)
        for key, bad in (("theme", "blue"), ("max_connections", 0), ("max_connections", True),
                         ("query_timeout", -1), ("fetch_batch_size", 5), ("max_parallel_tasks", "3")):
            with self.assertRaises(ValueError):
                s.set(key, bad)
        with self.assertRaises(KeyError):
            s.set("nope", 1)

    def test_corrupt_setting_falls_back(self):
        s = SettingsStore(self.db)
        self.db.execute("INSERT INTO settings(key, value) VALUES('theme', 'not-json')")
        self.assertEqual(s.get("theme"), "light")

    def test_profile_repo_crud_and_unique_name(self):
        repo = ProfileRepository(self.db)
        p = repo.save(ConnectionProfile.new("Prod", "srv1"))
        self.assertEqual(repo.get(p.id).server, "srv1")
        p.server = "srv2"
        repo.save(p)
        self.assertEqual(len(repo.list()), 1)
        self.assertEqual(repo.get(p.id).server, "srv2")
        with self.assertRaises(ProfileError):
            repo.save(ConnectionProfile.new("prod", "x"))  # имя без учёта регистра
        with self.assertRaises(ProfileError):
            repo.save(ConnectionProfile.new("Bad", ""))
        repo.delete(p.id)
        self.assertEqual(repo.list(), [])

    def test_oplog_redacts_and_filters(self):
        log = OperationLog(self.db)
        log.add("connection", "login", "error", "failed PWD=hunter2;")
        log.add("task", "export", "ok", "done", 120)
        rec = log.recent()
        self.assertEqual(rec[0].category, "task")
        self.assertNotIn("hunter2", log.recent(category="connection")[0].message)
        self.assertEqual(log.count_since(24, "error"), 1)

    def test_persistence_between_runs_and_newer_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "a.sqlite3")
            db = AppDatabase(path)
            SettingsStore(db).set("theme", "dark")
            db.close()
            db2 = AppDatabase(path)
            self.assertEqual(SettingsStore(db2).get("theme"), "dark")
            self.assertEqual(db2.schema_version, SCHEMA_VERSION)
            db2.close()
            raw = sqlite3.connect(path)
            raw.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 5}")
            raw.commit()
            raw.close()
            with self.assertRaises(RuntimeError):
                AppDatabase(path)


class CredentialTests(unittest.TestCase):
    def test_in_memory_store(self):
        s = InMemoryCredentialStore()
        s.set("k", "v")
        self.assertEqual(s.get("k"), "v")
        s.delete("k")
        self.assertIsNone(s.get("k"))
        self.assertFalse(s.persistent)

    def test_keyring_store_reports_unavailable_without_backend(self):
        s = KeyringCredentialStore()
        try:
            import keyring  # noqa: F401
            self.skipTest("keyring установлен — проверка недоступности неприменима")
        except ImportError:
            self.assertFalse(s.is_available())
            with self.assertRaises(Exception):
                s.set("k", "v")


class ServiceTests(unittest.TestCase):
    """Сервис профилей + bootstrap с поддельным ODBC-сервером и хранилищем паролей в памяти."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = FakeServer()
        self.creds = InMemoryCredentialStore()
        self.creds.persistent = True  # имитируем системное хранилище
        self.svc = create_services(self.tmp.name, self.creds, self.server.make_connection)

    def tearDown(self):
        self.svc.shutdown()
        self.tmp.cleanup()

    def sql_profile(self, name="Prod"):
        p = ConnectionProfile.new(name, "srv")
        p.auth, p.username = AUTH_SQL, "sa"
        return p

    def test_password_goes_only_to_credential_store_never_to_disk(self):
        p = self.svc.profiles.save(self.sql_profile(), "Sup3rS3cretPwd", remember=True)
        self.assertTrue(p.save_password)
        self.assertEqual(self.creds.get(profile_key(p.id)), "Sup3rS3cretPwd")
        self.svc.db.close()
        for name in os.listdir(self.svc.paths.data):
            with open(os.path.join(self.svc.paths.data, name), "rb") as fh:
                self.assertNotIn(b"Sup3rS3cretPwd", fh.read(), name)
        self.svc.db = type(self.svc.db)(self.svc.paths.db_file)  # для tearDown

    def test_not_remembered_password_is_not_stored(self):
        p = self.svc.profiles.save(self.sql_profile(), "pw", remember=False)
        self.assertIsNone(self.creds.get(profile_key(p.id)))
        self.assertTrue(self.svc.profiles.needs_password(p))

    def test_unchecking_remember_removes_stored_password(self):
        p = self.svc.profiles.save(self.sql_profile(), "pw", remember=True)
        p = self.svc.profiles.save(p, None, remember=False)
        self.assertIsNone(self.creds.get(profile_key(p.id)))

    def test_non_persistent_store_never_marks_save_password(self):
        self.creds.persistent = False
        p = self.svc.profiles.save(self.sql_profile(), "pw", remember=True)
        self.assertFalse(p.save_password)

    def test_connect_uses_stored_password_and_logs(self):
        p = self.svc.profiles.save(self.sql_profile(), "pw1", remember=True)
        conn = self.svc.profiles.connect(p)
        self.assertTrue(conn.is_connected)
        self.assertIn("PWD=pw1", self.server.connection_strings[0])
        rec = self.svc.oplog.recent(category="connection")
        self.assertEqual(rec[0].status, "ok")
        self.svc.profiles.disconnect(p)
        self.assertEqual(self.svc.manager.count(), 0)

    def test_failed_connect_is_logged_without_password(self):
        p = self.svc.profiles.save(self.sql_profile(), "hunter2", remember=True)
        self.server.connect_errors.append(FakeOdbcError("28000", "Login failed (18456) PWD=hunter2"))
        with self.assertRaises(ConnectionFailedError):
            self.svc.profiles.connect(p)
        rec = self.svc.oplog.recent(category="connection")[0]
        self.assertEqual(rec.status, "error")
        self.assertNotIn("hunter2", rec.message)

    def test_test_connection_uses_temporary_connection_and_closes_it(self):
        info = self.svc.profiles.test(self.sql_profile(), "pw")
        self.assertEqual(info.edition, "Developer Edition")
        self.assertEqual(self.svc.manager.count(), 0)
        self.assertEqual(self.server.open_raws(), [])

    def test_test_connection_rejects_invalid_profile(self):
        bad = ConnectionProfile.new("x", "")
        with self.assertRaises(ConfigError):
            self.svc.profiles.test(bad)

    def test_delete_profile_cleans_credential_and_connection(self):
        p = self.svc.profiles.save(self.sql_profile(), "pw", remember=True)
        self.svc.profiles.connect(p)
        self.svc.profiles.delete(p.id)
        self.assertIsNone(self.creds.get(profile_key(p.id)))
        self.assertEqual(self.svc.manager.count(), 0)
        self.assertIsNone(self.svc.profiles.repo.get(p.id))

    def test_task_results_recorded_in_journal(self):
        t = self.svc.tasks.submit("Тестовая задача", lambda ctx: 1)
        t.wait(2)
        import time
        time.sleep(0.05)
        self.assertEqual(t.state, TaskState.SUCCEEDED)
        self.assertEqual(self.svc.oplog.recent(category="task")[0].action, "Тестовая задача")

    def test_settings_apply_to_services(self):
        self.assertEqual(self.svc.manager.max_connections, 5)
        self.assertEqual(self.svc.tasks.max_parallel, 3)


if __name__ == "__main__":
    unittest.main()
