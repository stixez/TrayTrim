"""Application setup: fonts, theme that follows the system, main window."""

from __future__ import annotations

import gc
import os
import sys
from importlib import resources

from PySide6.QtCore import QByteArray, Qt, QTimer
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from traytrim.gui import icons, tasks, theme
from traytrim.gui.main_window import MainWindow
from traytrim.gui.widgets import PlainToolTips
from traytrim.received import remove_stale_temp
from traytrim.settings import THEMES, Settings

_SCHEMES = {
    "system": Qt.ColorScheme.Unknown,  # Unknown means "follow the OS"
    "light": Qt.ColorScheme.Light,
    "dark": Qt.ColorScheme.Dark,
}


def apply_current_scheme(app: QApplication) -> None:
    choice = app.property("traytrim_theme") or "system"
    if choice in ("light", "dark"):
        dark = choice == "dark"
    else:
        dark = app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    p = theme.DARK if dark else theme.LIGHT
    app.setProperty("traytrim_palette", p.name)
    app.setPalette(theme.qpalette(p))
    app.setStyleSheet(theme.stylesheet(p))


def set_theme(app: QApplication, choice: str) -> None:
    """Use a theme ("system", "light" or "dark"). Qt is told too, so native title bars match;
    not every platform reports a forced scheme back, so the choice itself decides our palette."""
    app.setProperty("traytrim_theme", choice if choice in _SCHEMES else "system")
    app.styleHints().setColorScheme(_SCHEMES.get(choice, Qt.ColorScheme.Unknown))
    apply_current_scheme(app)


def create_app(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance() or QApplication(argv or sys.argv)
    app.setApplicationName("TrayTrim")
    app.setOrganizationName("TrayTrim")
    app.setStyle("Fusion")  # the same base look on every OS; our stylesheet does the rest
    svg = (resources.files("traytrim.gui") / "assets" / "app" / "icon.svg").read_bytes()
    renderer = QSvgRenderer(QByteArray(svg))
    window_icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        image = QImage(size, size, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        renderer.render(painter)
        painter.end()
        window_icon.addPixmap(QPixmap.fromImage(image))
    app.setWindowIcon(window_icon)
    theme.load_fonts()
    app.setFont(theme.ui_font())
    app.styleHints().colorSchemeChanged.connect(lambda _scheme: apply_current_scheme(app))
    if not app.findChildren(PlainToolTips):
        app.installEventFilter(PlainToolTips(app))
    return app


def _windows_taskbar_identity() -> None:
    """Group the window under TrayTrim's own taskbar icon, not Python's, when run from source."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TrayTrim.TrayTrim")
        except (AttributeError, OSError):
            pass


def run(theme_override: str | None = None) -> int:
    _windows_taskbar_identity()
    app = create_app()
    settings = Settings.load()
    set_theme(app, theme_override if theme_override in THEMES else settings.theme)
    tasks.run_in_background(remove_stale_temp, lambda _n: None, lambda _e: None)
    window = MainWindow(settings)
    window.show()
    if os.environ.get("TRAYTRIM_SMOKE_TEST"):
        # Packaging check: build the window, then quit cleanly (exit code 0).
        QTimer.singleShot(3000, app.quit)
    code = app.exec()
    # Tear down in order while Qt is still alive: stop and wait for background work, delete
    # the window, drop cached images. Left to Python's shutdown, Qt objects can be destroyed
    # after the application object, which crashes the process on exit.
    window.stop_work()
    tasks.shutdown()
    window.deleteLater()
    del window
    icons.image.cache_clear()
    app.sendPostedEvents(None, 0)  # runs the deleteLater
    gc.collect()
    return code
