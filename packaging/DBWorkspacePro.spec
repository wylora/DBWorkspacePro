# -*- mode: python ; coding: utf-8 -*-
# Сборка: см. packaging/build.ps1  (pyinstaller --noconfirm packaging/DBWorkspacePro.spec)
# Сборка «папкой» (onedir): быстрее запускается и реже вызывает ложные срабатывания антивирусов, чем onefile.
from pathlib import Path

ROOT = Path(SPECPATH).parent

datas = [
    (str(ROOT / "examples" / "report_templates"), "examples/report_templates"),
    (str(ROOT / "docs"), "docs"),
    (str(ROOT / "packaging" / "app.ico"), "packaging"),
    (str(ROOT / "config.example.json"), "."),
]
hiddenimports = [
    "keyring.backends.Windows",          # хранилище паролей Windows (подгружается динамически)
    "win32ctypes.core",
    "pyodbc",
    "matplotlib.backends.backend_agg",
]
excludes = ["tkinter", "matplotlib.backends.backend_tkagg", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore",
            "PySide6.QtQuick", "PySide6.QtQml", "pytest", "tests"]

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DBWorkspacePro",
    icon=str(ROOT / "packaging" / "app.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
    console=False,                       # окно без консоли; --run-task пишет вывод в журнал cli-output.txt
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="DBWorkspacePro")
