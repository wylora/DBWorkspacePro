"""Создаёт packaging/version_info.txt (ресурс версии Windows для PyInstaller) из version.py."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from version import __version__  # noqa: E402

TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({v}), prodvers=({v}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'DB Workspace Pro'),
      StringStruct('FileDescription', 'DB Workspace Pro — рабочее место для SQL Server'),
      StringStruct('FileVersion', '{s}'),
      StringStruct('InternalName', 'DBWorkspacePro'),
      StringStruct('OriginalFilename', 'DBWorkspacePro.exe'),
      StringStruct('ProductName', 'DB Workspace Pro'),
      StringStruct('ProductVersion', '{s}')])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""


def main() -> Path:
    parts = [int(p) for p in __version__.split(".")[:3]] + [0]
    while len(parts) < 4:
        parts.append(0)
    out = Path(__file__).with_name("version_info.txt")
    out.write_text(TEMPLATE.format(v=", ".join(map(str, parts[:4])), s=__version__), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(main())
