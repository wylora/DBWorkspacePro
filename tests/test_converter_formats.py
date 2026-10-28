import tempfile
import unittest
from pathlib import Path

from converter.formats import (FORMATS, SOURCE_FORMATS, TARGET_FORMATS, Capabilities, available, explain_unavailable,
                               plan, resolve_source, targets_for)

EXPECTED = {
    ("docx", "md"): ("python", ""), ("docx", "txt"): ("python", ""), ("docx", "html"): ("python", ""),
    ("docx", "pdf"): ("libreoffice", ""), ("docx", "odt"): ("libreoffice", ""), ("docx", "rtf"): ("libreoffice", ""),
    ("docx", "doc"): ("chain", "odt"), ("odt", "doc"): ("libreoffice", ""), ("md", "doc"): ("chain", "odt"),
    ("md", "docx"): ("python", ""), ("md", "html"): ("python", ""), ("md", "pdf"): ("chain", "docx"),
    ("html", "docx"): ("python", ""), ("html", "pdf"): ("libreoffice", ""), ("txt", "pdf"): ("libreoffice", ""),
    ("doc", "docx"): ("libreoffice", ""), ("doc", "md"): ("chain", "docx"), ("rtf", "txt"): ("libreoffice", ""),
    ("xlsx", "csv"): ("python", ""), ("csv", "xlsx"): ("python", ""), ("csv", "tsv"): ("python", ""),
    ("json", "xlsx"): ("python", ""), ("xls", "xlsx"): ("libreoffice", ""), ("xls", "csv"): ("chain", "xlsx"),
    ("xlsx", "xls"): ("libreoffice", ""), ("csv", "xls"): ("chain", "xlsx"), ("xlsx", "pdf"): ("libreoffice", ""),
    ("csv", "pdf"): ("chain", "xlsx"), ("json", "pdf"): ("chain", "xlsx"), ("docx", "csv"): ("python", ""),
    ("html", "json"): ("python", ""), ("md", "xlsx"): ("python", ""), ("odt", "json"): ("chain", "docx"),
    ("txt", "sql"): ("copy", ""), ("sql", "txt"): ("copy", ""),
}
UNSUPPORTED = [("sql", "docx"), ("sql", "pdf"), ("xlsx", "docx"), ("xlsx", "md"), ("csv", "html"), ("txt", "csv"),
               ("csv", "csv"), ("docx", "docx"), ("pdf", "docx"), ("pdf", "txt"), ("txt", "xlsx"), ("docx", "sql"),
               ("json", "md"), ("xls", "txt")]


class PlannerTests(unittest.TestCase):
    def test_key_routes(self):
        for (s, d), (kind, via) in EXPECTED.items():
            r = plan(s, d)
            self.assertIsNotNone(r, (s, d))
            self.assertEqual((r.kind, r.via), (kind, via), (s, d))

    def test_unsupported_pairs_are_refused_with_reason(self):
        for s, d in UNSUPPORTED:
            self.assertIsNone(plan(s, d), (s, d))
        for s in SOURCE_FORMATS:
            for d in TARGET_FORMATS:
                if plan(s, d) is None:
                    self.assertTrue(explain_unavailable(s, d), (s, d))

    def test_every_route_is_consistent(self):
        n = 0
        for s in SOURCE_FORMATS:
            for d in TARGET_FORMATS:
                r = plan(s, d)
                if r is None:
                    continue
                n += 1
                self.assertEqual((r.src, r.dst), (s, d))
                if r.kind == "chain":
                    r1, r2 = plan(s, r.via), plan(r.via, d)
                    self.assertIsNotNone(r1, (s, d))
                    self.assertIsNotNone(r2, (s, d))
                    self.assertTrue(r.needs_lo)
                    self.assertTrue(r1.needs_lo or r2.needs_lo)
                elif r.kind == "python":
                    self.assertFalse(r.needs_lo, (s, d))
                elif r.kind == "libreoffice":
                    self.assertTrue(r.needs_lo)
                self.assertTrue(r.description)
        self.assertGreaterEqual(n, 100)

    def test_pdf_is_target_only_and_every_listed_format_is_known(self):
        self.assertNotIn("pdf", SOURCE_FORMATS)
        for f in SOURCE_FORMATS + TARGET_FORMATS:
            self.assertIn(f, FORMATS)

    def test_notes_warn_about_fidelity(self):
        self.assertTrue(any("изображения" in n for n in plan("docx", "md").notes))
        self.assertTrue(any("несколько листов" in n for n in plan("xlsx", "csv").notes))
        self.assertTrue(any("через промежуточный ODT" in n for n in plan("docx", "doc").notes))
        self.assertTrue(any("только таблицы" in n for n in plan("docx", "csv").notes))
        self.assertTrue(any("Первая строка" in n for n in plan("csv", "json").notes))

    def test_availability_depends_on_libreoffice(self):
        none, lo = Capabilities(None), Capabilities("/usr/bin/soffice")
        py, via_lo = plan("docx", "md"), plan("docx", "pdf")
        self.assertEqual(available(py, none), (True, ""))
        ok, why = available(via_lo, none)
        self.assertFalse(ok)
        self.assertIn("LibreOffice", why)
        self.assertTrue(available(via_lo, lo)[0])
        self.assertEqual(available(None, lo)[0], False)
        targets = {d: ok for d, _r, ok, _w in targets_for("docx", none)}
        self.assertTrue(targets["md"] and not targets["pdf"])
        self.assertNotIn("docx", targets)


class SourceResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, data):
        p = self.d / name
        p.write_bytes(data)
        return p

    def test_renamed_files_are_detected(self):
        from docx import Document
        import openpyxl
        Document().save(self.d / "real.docx")
        wb = openpyxl.Workbook()
        wb.save(self.d / "real.xlsx")
        as_doc = self.write("renamed.doc", (self.d / "real.docx").read_bytes())
        as_xls = self.write("renamed.xls", (self.d / "real.xlsx").read_bytes())
        self.assertEqual(resolve_source(as_doc)[0], "docx")
        self.assertIn("на самом деле", resolve_source(as_doc)[1][0])
        self.assertEqual(resolve_source(as_xls)[0], "xlsx")
        ole = self.write("old.docx", b"\xd0\xcf\x11\xe0" + b"\0" * 20)
        self.assertEqual(resolve_source(ole)[0], "doc")

    def test_problems_are_reported(self):
        cases = {
            "empty.docx": (b"", "пустой"), "fake.docx": (b"hello", "повреждён"), "fake.rtf": (b"hello", "повреждён"),
            "fake.xls": (b"hello", "повреждён"), "a.pdf": (b"%PDF-1.4", "не поддерживается"),
            "a.exe": (b"MZ", "не поддерживается"), "noext": (b"abc", "не поддерживается"),
        }
        for name, (data, text) in cases.items():
            fmt, _notes, err = resolve_source(self.write(name, data))
            self.assertIsNone(fmt, name)
            self.assertIn(text, err, name)
        fmt, _n, err = resolve_source(self.d / "missing.docx")
        self.assertIsNone(fmt)
        self.assertIn("Не удалось прочитать", err)

    def test_extensions_case_insensitive(self):
        self.assertEqual(resolve_source(self.write("A.MD", b"# x"))[0], "md")
        self.assertEqual(resolve_source(self.write("b.HTM", b"<p>x</p>"))[0], "html")
        self.assertEqual(resolve_source(self.write("c.markdown", b"x"))[0], "md")


if __name__ == "__main__":
    unittest.main()
