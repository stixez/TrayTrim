"""Small hand-offs to the operating system."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QUrl
from PySide6.QtGui import QDesktopServices


def reveal(path: Path) -> None:
    """Show a file in the file manager, selected where the platform supports it."""
    if sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent if path.is_file() else path)))


def open_path(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def desktop() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    return Path(location) if location else Path.home()
