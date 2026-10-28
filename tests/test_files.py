import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils.files import (InsufficientDiskSpaceError, atomic_write_bytes, atomic_write_text,
                         ensure_free_space, is_within, sanitize_filename)


class FilesTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(sanitize_filename('a<b>:"c"/d\\e|f?g*.txt'), "a_b___c__d_e_f_g_.txt")
        self.assertEqual(sanitize_filename("CON.txt"), "_CON.txt")
        self.assertEqual(sanitize_filename("  ..  "), "file")
        self.assertEqual(sanitize_filename("../../etc/passwd"), ".._.._etc_passwd")
        self.assertLessEqual(len(sanitize_filename("x" * 400 + ".docx")), 150)
        self.assertTrue(sanitize_filename("x" * 400 + ".docx").endswith(".docx"))

    def test_is_within(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(is_within(d, Path(d) / "a" / "b.txt"))
            self.assertFalse(is_within(d, Path(d) / ".." / "x.txt"))

    def test_atomic_write_replaces_and_leaves_no_temp(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "r.txt"
            atomic_write_text(target, "one")
            atomic_write_text(target, "two")
            self.assertEqual(target.read_text(encoding="utf-8"), "two")
            self.assertEqual(os.listdir(d), ["r.txt"])

    def test_atomic_write_failure_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "r.txt"
            atomic_write_text(target, "old")
            with mock.patch("os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    atomic_write_bytes(target, b"new")
            self.assertEqual(target.read_text(), "old")
            self.assertEqual(os.listdir(d), ["r.txt"])

    def test_write_into_missing_dir_creates_it(self):
        with tempfile.TemporaryDirectory() as d:
            atomic_write_text(Path(d) / "sub" / "dir" / "f.txt", "x")
            self.assertTrue((Path(d) / "sub" / "dir" / "f.txt").exists())

    def test_free_space_check(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertGreater(ensure_free_space(d, 1024), 0)
            with self.assertRaises(InsufficientDiskSpaceError):
                ensure_free_space(d, 10 ** 18)
            ensure_free_space(Path(d) / "not" / "yet", 10)  # путь ещё не существует


if __name__ == "__main__":
    unittest.main()
