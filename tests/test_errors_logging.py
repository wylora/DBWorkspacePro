import logging
import os
import tempfile
import unittest

from database.errors import (friendly_message, is_cancelled, is_connection_lost, is_timeout,
                             ConfigError)
from tests.fakes import FakeOdbcError
from utils.logging_setup import RedactingFormatter, redact, setup_logging


class FriendlyErrorTests(unittest.TestCase):
    def test_login_failed(self):
        e = FakeOdbcError("28000", "[28000] [Microsoft][ODBC Driver 18][SQL Server]Login failed for user 'x'. (18456)")
        self.assertIn("неверный логин или пароль", friendly_message(e))

    def test_unreachable(self):
        e = FakeOdbcError("08001", "[08001] TCP Provider: No such host is known. (11001)")
        self.assertIn("Не удалось подключиться к серверу", friendly_message(e))

    def test_certificate(self):
        e = FakeOdbcError("08001", "SSL Provider: certificate verify failed:self-signed certificate")
        self.assertIn("сертификат", friendly_message(e))

    def test_driver_missing(self):
        e = FakeOdbcError("IM002", "Data source name not found and no default driver specified")
        self.assertIn("ODBC", friendly_message(e))

    def test_timeout_cancel_lost_flags(self):
        self.assertTrue(is_timeout(FakeOdbcError("HYT00", "Query timeout expired")))
        self.assertTrue(is_cancelled(FakeOdbcError("HY008", "Operation canceled")))
        self.assertTrue(is_connection_lost(FakeOdbcError("08S01", "Communication link failure")))
        self.assertFalse(is_connection_lost(FakeOdbcError("42000", "Syntax error")))

    def test_generic_has_no_traceback_or_password(self):
        e = FakeOdbcError("42000", "[42000] [SQL Server]Incorrect syntax near 'x'. PWD=hunter2")
        msg = friendly_message(e)
        self.assertNotIn("hunter2", msg)
        self.assertNotIn("Traceback", msg)

    def test_own_errors_pass_through(self):
        self.assertEqual(friendly_message(ConfigError("abc")), "abc")


class RedactTests(unittest.TestCase):
    def test_conn_string(self):
        s = "DRIVER={X};UID=sa;PWD={p;ass}}word};Encrypt=yes"
        out = redact(s)
        self.assertNotIn("ass}}word", out)
        self.assertIn("PWD=***", out)
        self.assertIn("Encrypt=yes", out)

    def test_other_keys(self):
        self.assertEqual(redact("password: abc123"), "password: ***")
        self.assertEqual(redact("token=xyz"), "token=***")

    def test_log_file_never_contains_secret(self):
        with tempfile.TemporaryDirectory() as d:
            setup_logging(d, "DEBUG", console=False)
            log = logging.getLogger("dbworkspace.test")
            log.info("conn UID=sa;PWD=TopSecret1;")
            try:
                raise RuntimeError("boom PWD=TopSecret2")
            except RuntimeError:
                log.exception("fail")
            for h in logging.getLogger().handlers:
                h.flush()
            with open(os.path.join(d, "dbworkspace.log"), encoding="utf-8") as fh:
                text = fh.read()
            setup_logging(d, "INFO", console=False)  # повторный вызов не дублирует обработчики
            for h in list(logging.getLogger().handlers):
                if getattr(h, "_dbwp_handler", False):
                    logging.getLogger().removeHandler(h)
                    h.close()
        self.assertIn("conn UID=sa", text)
        self.assertNotIn("TopSecret1", text)
        self.assertNotIn("TopSecret2", text)


if __name__ == "__main__":
    unittest.main()
