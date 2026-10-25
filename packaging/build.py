"""Build the app (PyInstaller, one folder) and, on Windows, its installer (Inno Setup).

    pip install -e ".[gui,build]"
    python packaging/build.py              # dist/TrayTrim/ and dist/TrayTrim-<version>-setup.exe
    python packaging/build.py --no-installer

On macOS the app is dist/TrayTrim.app, on Linux dist/TrayTrim/TrayTrim. CI builds all three
(.github/workflows/release.yml). The installer needs Inno Setup 6 (ISCC.exe on PATH, in its
default folder, or in $ISCC).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
sys.path.insert(0, str(ROOT / "src"))

from traytrim import __version__  # noqa: E402

# Qt modules TrayTrim doesn't use; leaving them out keeps the download small.
EXCLUDE = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
           "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.Qt3DCore",
           "PySide6.QtMultimedia", "PySide6.QtNetwork", "PySide6.QtPdf", "PySide6.QtCharts",
           "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtPositioning",
           "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtDesigner", "tkinter"]


def version_file(path: Path) -> Path:
    """Windows file properties (version, product name) for TrayTrim.exe."""
    nums = (__version__.split("+")[0].split(".") + ["0"] * 4)[:4]
    tup = ", ".join(str(int(n)) if n.isdigit() else "0" for n in nums)
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({tup}), prodvers=({tup})),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'TrayTrim contributors'),
    StringStruct('FileDescription', 'TrayTrim'),
    StringStruct('FileVersion', '{__version__}'),
    StringStruct('InternalName', 'TrayTrim'),
    StringStruct('LegalCopyright', 'MIT License'),
    StringStruct('OriginalFilename', 'TrayTrim.exe'),
    StringStruct('ProductName', 'TrayTrim'),
    StringStruct('ProductVersion', '{__version__}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
""", encoding="utf-8")
    return path


def build_app() -> Path:
    import PyInstaller.__main__

    work = ROOT / "build"
    work.mkdir(exist_ok=True)
    sep = ";" if sys.platform == "win32" else ":"
    args = [
        str(PACKAGING / "launcher.py"),
        "--name", "TrayTrim",
        "--windowed",
        "--noconfirm",
        "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(work / "pyinstaller"),
        "--specpath", str(work),
        "--paths", str(ROOT / "src"),
        "--add-data", f"{ROOT / 'src' / 'traytrim' / 'gui' / 'assets'}{sep}traytrim/gui/assets",
        # TrayTrim's license and those of what the build bundles (Qt is LGPL).
        "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
        "--add-data", f"{ROOT / 'THIRD_PARTY_NOTICES.md'}{sep}.",
        "--add-data", f"{ROOT / 'LICENSES'}{sep}LICENSES",
        "--add-data", f"{python_license(work / 'Python')}{sep}LICENSES",
    ]
    if sys.platform == "win32":
        args += ["--icon", str(PACKAGING / "assets" / "TrayTrim.ico"),
                 "--version-file", str(version_file(work / "version_info.txt"))]
    elif sys.platform == "darwin":
        # PyInstaller turns the PNG into an .icns (with Pillow) and makes TrayTrim.app.
        args += ["--icon", str(PACKAGING / "assets" / "TrayTrim-1024.png"),
                 "--osx-bundle-identifier", "io.github.stixez.traytrim"]
    for module in EXCLUDE:
        args += ["--exclude-module", module]
    PyInstaller.__main__.run(args)
    if sys.platform == "darwin":
        set_bundle_version(ROOT / "dist" / "TrayTrim.app" / "Contents" / "Info.plist")
    return ROOT / "dist" / "TrayTrim"


def python_license(folder: Path) -> Path:
    """The license of the Python runtime the app bundles, as LICENSES/Python.txt. It also lists
    the libraries Python includes (OpenSSL, zlib, SQLite, libffi, expat, bzip2, xz)."""
    base = Path(sys.base_prefix)
    candidates = [base / "LICENSE.txt",
                  base / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "LICENSE.txt"]
    source = next((c for c in candidates if c.is_file()), None)
    if source is None:
        raise SystemExit(f"Python's LICENSE.txt not found under {base}")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "Python.txt"
    shutil.copyfile(source, target)
    return target


def set_bundle_version(plist: Path) -> None:
    """PyInstaller leaves the app at version 0.0.0; Finder's Get Info shows this one."""
    import plistlib

    with plist.open("rb") as f:
        info = plistlib.load(f)
    info["CFBundleShortVersionString"] = info["CFBundleVersion"] = __version__
    with plist.open("wb") as f:
        plistlib.dump(info, f)


def find_iscc() -> Path | None:
    candidates = [os.environ.get("ISCC"), shutil.which("ISCC")]
    for base in (os.environ.get(name) for name in ("LOCALAPPDATA", "ProgramFiles(x86)",
                                                   "ProgramFiles")):
        if base:
            candidates.append(str(Path(base) / "Programs" / "Inno Setup 6" / "ISCC.exe"))
            candidates.append(str(Path(base) / "Inno Setup 6" / "ISCC.exe"))
    return next((Path(c) for c in candidates if c and Path(c).is_file()), None)


def build_installer() -> Path:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) "
                         "or set ISCC to ISCC.exe's path.")
    subprocess.run([str(iscc), f"/DAppVersion={__version__}", str(PACKAGING / "TrayTrim.iss")],
                   check=True, cwd=PACKAGING)
    return ROOT / "dist" / f"TrayTrim-{__version__}-setup.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-installer", action="store_true")
    args = parser.parse_args()
    app = build_app()
    print(f"App: {app}")
    if sys.platform == "win32" and not args.no_installer:
        print(f"Installer: {build_installer()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
