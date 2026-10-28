import unittest

from database.connection_string import build_connection_string, odbc_quote, redacted
from database.driver import choose_driver
from database.errors import ConfigError
from database.profile import AUTH_SQL, ConnectionProfile
from database.sqltext import format_type, quote_ident


def prof(**kw):
    base = dict(id="p1", name="Тест", server="srv")
    base.update(kw)
    return ConnectionProfile(**base)


class ProfileTests(unittest.TestCase):
    def test_valid_minimal(self):
        self.assertEqual(prof().validate(), [])

    def test_missing_server_and_name(self):
        errs = prof(name=" ", server="").validate()
        self.assertEqual(len(errs), 2)

    def test_instance_and_port_conflict(self):
        self.assertTrue(any("либо экземпляр" in e for e in prof(instance="SQLEXPRESS", port=1433).validate()))

    def test_port_range(self):
        self.assertTrue(prof(port=0).validate())
        self.assertTrue(prof(port=70000).validate())
        self.assertEqual(prof(port=1433).validate(), [])

    def test_sql_auth_requires_username(self):
        self.assertTrue(prof(auth=AUTH_SQL, username="").validate())

    def test_server_with_instance_is_split(self):
        p = prof(server=r"host\SQLEXPRESS").normalized()
        self.assertEqual((p.server, p.instance), ("host", "SQLEXPRESS"))
        self.assertEqual(prof(server=r"host\SQLEXPRESS").server_spec(), r"host\SQLEXPRESS")

    def test_server_with_port_is_split(self):
        self.assertEqual(prof(server="host,1444").server_spec(), "host,1444")

    def test_bad_encrypt_and_timeouts(self):
        self.assertTrue(prof(encrypt="maybe").validate())
        self.assertTrue(prof(connect_timeout=0).validate())
        self.assertTrue(prof(query_timeout=-1).validate())

    def test_no_password_field_in_profile(self):
        d = prof(auth=AUTH_SQL, username="sa").to_dict()
        self.assertNotIn("password", d)
        self.assertNotIn("pwd", d)
        self.assertIsInstance(d["save_password"], bool)  # только флаг «запомнить», не сам пароль

    def test_roundtrip_ignores_unknown_keys(self):
        d = prof().to_dict()
        d["junk"] = 1
        self.assertEqual(ConnectionProfile.from_dict(d).server, "srv")


class ConnStringTests(unittest.TestCase):
    def test_windows_auth(self):
        cs = build_connection_string(prof(database="Sales"), None, "ODBC Driver 18 for SQL Server")
        self.assertIn("DRIVER={ODBC Driver 18 for SQL Server}", cs)
        self.assertIn("Trusted_Connection=yes", cs)
        self.assertIn("DATABASE=Sales", cs)
        self.assertNotIn("PWD", cs)

    def test_certificate_validation_on_by_default(self):
        cs = build_connection_string(prof(), None, "ODBC Driver 18 for SQL Server")
        self.assertIn("Encrypt=yes", cs)
        self.assertIn("TrustServerCertificate=no", cs)

    def test_trust_certificate_only_when_explicit(self):
        cs = build_connection_string(prof(trust_server_certificate=True), None, "ODBC Driver 18 for SQL Server")
        self.assertIn("TrustServerCertificate=yes", cs)

    def test_sql_auth_special_chars_escaped(self):
        cs = build_connection_string(prof(auth=AUTH_SQL, username="sa"), "p;w}d{=", "ODBC Driver 18 for SQL Server")
        self.assertIn("PWD={p;w}}d{=}", cs)

    def test_sql_auth_requires_password(self):
        with self.assertRaises(ConfigError):
            build_connection_string(prof(auth=AUTH_SQL, username="sa"), "", "ODBC Driver 18 for SQL Server")

    def test_strict_needs_driver_18(self):
        with self.assertRaises(ConfigError):
            build_connection_string(prof(encrypt="strict"), None, "ODBC Driver 17 for SQL Server")
        self.assertIn("Encrypt=strict",
                      build_connection_string(prof(encrypt="strict"), None, "ODBC Driver 18 for SQL Server"))

    def test_invalid_profile_rejected(self):
        with self.assertRaises(ConfigError):
            build_connection_string(prof(server=""), None, "ODBC Driver 18 for SQL Server")

    def test_redacted_hides_password(self):
        cs = build_connection_string(prof(auth=AUTH_SQL, username="sa"), "S3cret;x", "ODBC Driver 18 for SQL Server")
        self.assertNotIn("S3cret", redacted(cs))

    def test_odbc_quote(self):
        self.assertEqual(odbc_quote("abc"), "abc")
        self.assertEqual(odbc_quote("a b"), "{a b}")


class DriverAndSqlTextTests(unittest.TestCase):
    def test_choose_newest(self):
        self.assertEqual(choose_driver("", ["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server", "SQL Server"]),
                         "ODBC Driver 18 for SQL Server")

    def test_choose_none_when_only_legacy(self):
        self.assertIsNone(choose_driver("", ["SQL Server"]))

    def test_preferred_missing_is_not_silently_replaced(self):
        self.assertIsNone(choose_driver("ODBC Driver 13 for SQL Server", ["ODBC Driver 18 for SQL Server"]))

    def test_quote_ident(self):
        self.assertEqual(quote_ident("a]b"), "[a]]b]")
        self.assertEqual(quote_ident("x; DROP TABLE t--"), "[x; DROP TABLE t--]")
        with self.assertRaises(ValueError):
            quote_ident("")
        with self.assertRaises(ValueError):
            quote_ident("x" * 129)

    def test_format_type(self):
        self.assertEqual(format_type("nvarchar", 100, 0, 0), "nvarchar(50)")
        self.assertEqual(format_type("nvarchar", -1, 0, 0), "nvarchar(max)")
        self.assertEqual(format_type("varchar", 20, 0, 0), "varchar(20)")
        self.assertEqual(format_type("decimal", 9, 18, 2), "decimal(18,2)")
        self.assertEqual(format_type("datetime2", 8, 27, 7), "datetime2(7)")
        self.assertEqual(format_type("int", 4, 10, 0), "int")


if __name__ == "__main__":
    unittest.main()
