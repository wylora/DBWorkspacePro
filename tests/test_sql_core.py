import unittest

from database.sql_diagnostics import find_syntax_issues, locate_error
from database.sql_guard import RiskLevel, analyze, split_batches
from database.sql_lexer import BLOCK, BRACKET, NORMAL, STRING, lex, lex_line

R, M, D = RiskLevel.READ, RiskLevel.MODIFY, RiskLevel.DANGEROUS


class LexerTests(unittest.TestCase):
    def kinds(self, sql):
        return [(t.kind, t.text) for t in lex(sql).tokens]

    def test_basic_tokens(self):
        self.assertEqual(self.kinds("SELECT @x, 12.5 FROM [My Tab] -- c"),
                         [("word", "SELECT"), ("var", "@x"), ("op", ","), ("number", "12.5"), ("word", "FROM"),
                          ("ident", "[My Tab]"), ("comment", "-- c")])

    def test_escaped_quotes(self):
        self.assertEqual(self.kinds("'it''s'"), [("string", "'it''s'")])
        self.assertEqual(self.kinds("[a]]b]"), [("ident", "[a]]b]")])

    def test_multiline_string_and_state(self):
        toks, st = lex_line("SELECT 'abc", NORMAL)
        self.assertEqual(st, STRING)
        toks2, st2 = lex_line("def' + 1", st)
        self.assertEqual(st2, NORMAL)
        self.assertEqual(toks2[0].text, "def'")

    def test_nested_block_comments(self):
        _, st = lex_line("/* a /* b */ still", NORMAL)
        self.assertEqual(st, BLOCK + 1)
        _, st = lex_line("end */ SELECT 1", st)
        self.assertEqual(st, NORMAL)

    def test_open_start_for_unterminated(self):
        r = lex("SELECT 1\nSELECT 'oops\nmore")
        self.assertEqual(r.end_state, STRING)
        self.assertEqual(r.open_start, len("SELECT 1\nSELECT "))

    def test_temp_tables_and_unicode(self):
        self.assertEqual(self.kinds("#t ##g Таблица"), [("word", "#t"), ("word", "##g"), ("word", "Таблица")])


class SplitTests(unittest.TestCase):
    def test_go_split_and_empty_batches(self):
        b, e = split_batches("SELECT 1\nGO\n-- nothing\nGO\nSELECT 2\ngo")
        self.assertEqual([x.text for x in b], ["SELECT 1", "SELECT 2"])
        self.assertEqual(e, [])

    def test_go_inside_string_or_comment_is_not_separator(self):
        b, _ = split_batches("SELECT '\nGO\n'\n/*\nGO\n*/\nSELECT 2")
        self.assertEqual(len(b), 1)

    def test_go_count_unsupported(self):
        _, e = split_batches("SELECT 1\nGO 5")
        self.assertEqual(len(e), 1)
        _, e = split_batches("SELECT 1\nGO 1")
        self.assertEqual(e, [])

    def test_offsets(self):
        sql = "SELECT 1\nGO\nSELECT 2"
        b, _ = split_batches(sql)
        self.assertEqual(sql[b[1].start_offset:], "SELECT 2")


class GuardTests(unittest.TestCase):
    CASES = [
        ("SELECT * FROM t WHERE a = 1", R), ("select update_date, [Update], delete_flag from t", R),
        ("DELETE FROM t", D), ("DELETE FROM t WHERE id = 1", D), ("DELETE t WHERE 1=1", D),
        ("UPDATE t SET a = 1", D), ("UPDATE t SET a = 1 WHERE id = 3", M), ("UPDATE t SET a=1 WHERE 1 = 1", D),
        ("UPDATE t SET a = (SELECT MAX(x) FROM u WHERE u.k = 1)", D),
        ("DROP TABLE x", D), ("DROP TABLE #x", R), ("TRUNCATE TABLE t", D), ("TRUNCATE TABLE #t", R),
        ("INSERT INTO t VALUES (1)", M), ("INSERT INTO #t VALUES (1)", R), ("INSERT INTO @tv VALUES (1)", R),
        ("SELECT * INTO #t FROM a", R), ("SELECT * INTO newt FROM a", M),
        ("EXEC sp_who2", R), ("EXEC dbo.my_proc", M), ("EXEC('x')", D), ("exec sp_executesql @s", D),
        ("EXEC xp_cmdshell 'dir'", D), ("EXEC @r = sp_help 't'", R),
        ("-- DELETE FROM t\nSELECT 1", R), ("SELECT 'DELETE FROM t'", R), ("/* DROP TABLE t */ SELECT 1", R),
        ("CREATE TABLE a(i int)", M), ("CREATE TABLE #a(i int)", R), ("ALTER TABLE t ADD c int", M),
        ("WITH c AS (SELECT 1 a) DELETE FROM c", D), ("GRANT DELETE ON t TO u", M),
        ("ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES b(id) ON DELETE CASCADE", M),
        ("my_proc 1", M), ("SELECT 1; DELETE FROM t", D), ("SELECT 1\nGO\nDROP TABLE t", D),
        ("MERGE t USING s ON t.id = s.id WHEN MATCHED THEN DELETE;", D),
        ("MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET a = 1;", M),
        ("BEGIN TRAN; SELECT 1; ROLLBACK", R), ("DECLARE @a int = 1; SET @a = 2; SELECT @a", R),
        ("USE Sales; SELECT 1", R), ("DISABLE TRIGGER trg ON t", M), ("SHUTDOWN", D),
        ("UPDATE STATISTICS t", M), ("RESTORE DATABASE x FROM DISK='a'", D),
    ]

    def test_classification_table(self):
        wrong = [(sql, int(exp), int(analyze(sql).level)) for sql, exp in self.CASES
                 if analyze(sql).level != exp]
        self.assertEqual(wrong, [])

    def test_use_detected(self):
        self.assertTrue(analyze("USE x").has_use)
        self.assertFalse(analyze("SELECT 1").has_use)

    def test_reasons_and_snippets(self):
        a = analyze("SELECT 1;\nDELETE FROM Orders")
        self.assertEqual(a.level, D)
        self.assertIn("DELETE без условия WHERE удалит ВСЕ строки", a.reasons)
        self.assertEqual(a.findings[0].snippet, "DELETE FROM Orders")
        self.assertEqual("SELECT 1;\nDELETE FROM Orders"[a.findings[0].offset:], "DELETE FROM Orders")

    def test_read_only_flag(self):
        self.assertTrue(analyze("SELECT 1").is_read_only)
        self.assertFalse(analyze("SELECT 1\nGO 3").is_read_only)  # ошибка разбора


class DiagnosticsTests(unittest.TestCase):
    def test_syntax_issues(self):
        sql = "SELECT (1+2))\nGO\nSELECT 'abc"
        msgs = [(i.start, i.message) for i in find_syntax_issues(sql)]
        self.assertEqual(msgs[0], (12, "Лишняя закрывающая скобка"))
        self.assertTrue(msgs[1][1].startswith("Не закрыта строка"))
        self.assertEqual(sql[msgs[1][0]], "'")

    def test_unclosed_paren_and_comment(self):
        self.assertEqual(find_syntax_issues("SELECT (1")[0].message, "Не закрыта скобка")
        self.assertEqual(find_syntax_issues("SELECT 1 /* x")[0].message, "Не закрыт комментарий /* */")
        self.assertEqual(find_syntax_issues("SELECT (1) -- )\n"), [])

    def test_locate_error(self):
        sql = "SELECT a FROM Ordrs WHERE x"
        self.assertEqual(locate_error(sql, "Invalid object name 'Ordrs'."), (14, 19))
        self.assertEqual(locate_error("SELEC 1", "Incorrect syntax near 'SELEC'."), (0, 5))
        self.assertEqual(locate_error("SELECT 1\nSELEC", "Line 2: error"), (9, 14))
        self.assertIsNone(locate_error("SELECT 1", "Something else"))
        # слово внутри строки/комментария игнорируется при поиске по токенам
        self.assertEqual(locate_error("SELECT 'bad' , bad", "Invalid column name 'bad'.")[0], 15)


if __name__ == "__main__":
    unittest.main()
