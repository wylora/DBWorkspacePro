import tempfile
import unittest
from pathlib import Path

from converter.libreoffice import locate_soffice


def exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


class WindowsLookupTests(unittest.TestCase):
    """Сценарии Windows моделируются временными папками (platform='win32', переменные окружения, реестр)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.env = {"ProgramFiles": str(self.d / "PF"), "ProgramFiles(x86)": str(self.d / "PF86"),
                    "LOCALAPPDATA": str(self.d / "Local"), "USERPROFILE": str(self.d / "User")}

    def tearDown(self):
        self.tmp.cleanup()

    def find(self, custom="", registry=lambda: []):
        return locate_soffice(custom, env=self.env, platform="win32", which=lambda name: None, registry=registry)

    def test_other_drive_and_version_suffixed_folder(self):
        p = exe(self.d / "PF" / "LibreOffice 7" / "program" / "soffice.exe")      # ProgramFiles на другом диске
        r = self.find()
        self.assertEqual((r.path, r.source), (str(p), "папка программ"))

    def test_per_user_install(self):
        p = exe(self.d / "Local" / "Programs" / "LibreOffice" / "program" / "soffice.exe")
        self.assertEqual(self.find().path, str(p))

    def test_registry_custom_install_dir(self):
        p = exe(self.d / "Tools" / "LO" / "program" / "soffice.exe")
        r = self.find(registry=lambda: [f'"{p.parent}"'])                       # UNO\\InstallPath — папка program
        self.assertEqual((r.path, r.source), (str(p), "реестр Windows"))
        r2 = self.find(registry=lambda: [str(p)])                              # App Paths — сам файл
        self.assertEqual(r2.path, str(p))

    def test_portable_and_scoop(self):
        p = exe(self.d / "User" / "PortableApps" / "LibreOfficePortable" / "App" / "libreoffice" / "program" / "soffice.exe")
        self.assertEqual(self.find().source, "PortableApps")
        self.assertEqual(self.find().path, str(p))

    def test_settings_accept_root_folder_program_folder_and_other_exe(self):
        root = self.d / "D" / "LibreOffice"
        p = exe(root / "program" / "soffice.exe")
        exe(root / "program" / "swriter.exe")
        for custom in (str(root), str(root / "program"), str(p), f'"{p}"', str(root / "program" / "swriter.exe")):
            r = self.find(custom)
            self.assertEqual((r.path, r.source), (str(p), "путь из настроек"), custom)

    def test_wrong_settings_path_falls_back_and_explains(self):
        p = exe(self.d / "PF" / "LibreOffice" / "program" / "soffice.exe")
        r = self.find(str(self.d / "nowhere"))
        self.assertEqual(r.path, str(p))
        self.assertIn("Путь из настроек не подходит", r.note)

    def test_not_found_lists_where_it_looked(self):
        r = self.find()
        self.assertIsNone(r.path)
        joined = "\n".join(r.checked)
        for part in ("переменная PATH", "реестр Windows", "PF", "Local", "PortableApps"):
            self.assertIn(part, joined)

    def test_path_variable_wins(self):
        r = locate_soffice("", env=self.env, platform="win32", which=lambda n: "C:/X/soffice.exe" if n == "soffice" else None,
                           registry=lambda: [])
        self.assertEqual((r.path, r.source), ("C:/X/soffice.exe", "PATH"))


class EnvCheckAndCliTests(unittest.TestCase):
    def test_env_check_uses_settings_path(self):
        from bootstrap import create_services
        from security.credentials import InMemoryCredentialStore
        from utils.env_check import run_checks
        with tempfile.TemporaryDirectory() as d:
            lo = exe(Path(d) / "LO" / "program" / ("soffice"))
            svc = create_services(Path(d) / "h", InMemoryCredentialStore())
            svc.settings.set("libreoffice_path", str(Path(d) / "LO"))
            svc.shutdown()
            line = next(r for r in run_checks(Path(d) / "h") if r.name == "LibreOffice")
            self.assertIn(str(lo), line.detail)
            self.assertIn("путь из настроек", line.detail)


if __name__ == "__main__":
    unittest.main()
