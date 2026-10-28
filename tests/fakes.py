"""Имитация ODBC-сервера для тестов (реальный SQL Server не требуется)."""
from __future__ import annotations

import threading


class FakeOdbcError(Exception):
    """Похоже на pyodbc.Error: args = (SQLSTATE, текст)."""

    def __init__(self, state: str, text: str = ""):
        super().__init__(state, text)


class FakeCursor:
    def __init__(self, raw: "FakeRaw"):
        self.raw = raw
        self.server = raw.server
        self.description = None
        self._rows: list[tuple] = []
        self._pos = 0
        self.closed = False
        self.cancel_event = threading.Event()
        self.rowcount = -1
        self.messages = []
        self._sets = []
        self._set_idx = 0

    def nextset(self):
        if self._set_idx + 1 < len(self._sets):
            self._set_idx += 1
            self._load(self._sets[self._set_idx])
            return True
        return False

    def _load(self, spec):
        self._set(spec.get("columns", []), list(spec.get("rows", [])))
        self.rowcount = spec.get("rowcount", -1)

    def execute(self, sql, *params):
        if self.raw.closed:
            raise FakeOdbcError("08003", "Connection is closed")
        self.server.executed.append((sql, params))
        self.raw.autocommit_at_execute.append(self.raw.autocommit)
        self.raw.current = self
        if "ProductVersion" in sql:
            self._set(["v", "e", "s"], [("15.0.2000", "Developer Edition", "TESTSRV")])
            return self
        for h in self.server.handlers:
            if h["substr"] in sql:
                if h.get("on_execute"):
                    h["on_execute"](self)
                errors = h.get("errors")
                if errors:
                    raise errors.pop(0)
                if h.get("sets"):
                    self._sets, self._set_idx = h["sets"], 0
                    self._load(h["sets"][0])
                    self.messages = list(h.get("messages") or [])
                else:
                    self._sets, self._set_idx = [], 0
                    self._set(h.get("columns", []), list(h.get("rows", [])))
                    self.messages = list(h.get("messages") or [])
                return self
        if sql.strip().upper().startswith("SELECT 1"):
            self._set(["c"], [(1,)])
        else:
            self._set([], [])
        return self

    def _set(self, columns, rows):
        self.description = [(c, None, None, None, None, None, None) for c in columns] if columns else None
        self._rows = rows
        self._pos = 0

    def fetchmany(self, n):
        self.server.fetch_calls.append(n)
        chunk = self._rows[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def fetchone(self):
        r = self.fetchmany(1)
        return r[0] if r else None

    def cancel(self):
        self.cancel_event.set()

    def close(self):
        self.closed = True


class FakeRaw:
    def __init__(self, server: "FakeServer"):
        self.server = server
        self.closed = False
        self.timeout = None
        self.current = None
        self.cursors: list[FakeCursor] = []
        self.autocommit = True
        self.autocommit_at_execute = []
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def cursor(self):
        c = FakeCursor(self)
        self.cursors.append(c)
        return c

    def close(self):
        self.closed = True


class FakeServer:
    def __init__(self):
        self.handlers: list[dict] = []
        self.connect_errors: list[Exception] = []
        self.connect_calls = 0
        self.connection_strings: list[str] = []
        self.raws: list[FakeRaw] = []
        self.executed: list[tuple] = []
        self.fetch_calls: list[int] = []

    def when(self, substr, columns=None, rows=None, errors=None, on_execute=None, sets=None, messages=None):
        self.handlers.append({"substr": substr, "columns": columns or [], "rows": rows or [],
                              "errors": errors, "on_execute": on_execute, "sets": sets, "messages": messages})

    def connector(self, connection_string, timeout, autocommit):
        self.connect_calls += 1
        self.connection_strings.append(connection_string)
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        raw = FakeRaw(self)
        raw.autocommit = autocommit
        self.raws.append(raw)
        return raw

    def drivers(self):
        return ["ODBC Driver 17 for SQL Server", "ODBC Driver 18 for SQL Server"]

    def make_connection(self, profile, password=None, **kw):
        from database.connection import SqlConnection
        return SqlConnection(profile, password, connector=self.connector, driver_lister=self.drivers, **kw)

    def open_raws(self):
        return [r for r in self.raws if not r.closed]
