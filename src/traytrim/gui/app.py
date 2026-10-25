"""Application setup: fonts, theme that follows the system, main window."""

from __future__ import annotations

import gc
import os
import sys
import threading
import traceback
from importlib import resources
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMessageBox

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


_ISSUES = "https://github.com/stixez/TrayTrim/issues"


class CrashReporter(QObject):
    """sys.excepthook and threading.excepthook for the app: a bug in a click handler would
    otherwise only reach stderr, which a windowed app doesn't have, and the user would see
    nothing happen. The report opens later, on the UI thread: the bug may have happened in a
    worker thread or in the middle of painting, where a dialog can't open."""

    _report = Signal(str, str)  # headline, details

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._shown: set[str] = set()
        self._open = False
        self._report.connect(self._show, Qt.ConnectionType.QueuedConnection)

    def __call__(self, exc_type, exc, tb) -> None:
        details = "".join(traceback.format_exception(exc_type, exc, tb))
        if sys.stderr is not None:  # None in a windowed build
            sys.stderr.write(details)
        if issubclass(exc_type, KeyboardInterrupt):
            return
        try:
            self._report.emit(f"TrayTrim hit a bug: {exc_type.__name__}: {exc}", details)
        except RuntimeError:  # the app is shutting down and Qt already deleted the reporter
            pass

    def thread_hook(self, args: threading.ExceptHookArgs) -> None:
        if args.exc_type is not SystemExit:
            self(args.exc_type, args.exc_value, args.exc_traceback)

    def _show(self, headline: str, details: str) -> None:
        # Once per bug: one that fails on every repaint would otherwise open box after box.
        if self._open or headline in self._shown:
            return
        self._shown.add(headline)
        self._open = True
        try:
            self.ask(headline, details)
        finally:
            self._open = False

    def ask(self, headline: str, details: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, "TrayTrim", headline,
                          parent=QApplication.activeWindow())
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setInformativeText(
            "What you were doing may not have finished. Your Sims 4 folders weren't changed. "
            f"Copying the details into a report at {_ISSUES} helps fix it.")
        box.setDetailedText(details)
        copy = box.addButton("Copy details", QMessageBox.ButtonRole.ActionRole)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(details))
        box.setDefaultButton(box.addButton("Close", QMessageBox.ButtonRole.RejectRole))
        box.exec()


def _remove_stale_folders(settings: Settings) -> int:
    """Temporary folders of checks and exports the app was killed during."""
    from traytrim.export import STAGE_PREFIX
    from traytrim.gui.system import desktop

    removed = remove_stale_temp()
    outputs = {desktop(), *([Path(settings.output_dir)] if settings.output_dir else [])}
    for folder in outputs:
        removed += remove_stale_temp(root=folder, prefixes=(STAGE_PREFIX,))
    return removed


def run(theme_override: str | None = None) -> int:
    _windows_taskbar_identity()
    app = create_app()
    reporter = CrashReporter(app)
    sys.excepthook = reporter
    threading.excepthook = reporter.thread_hook
    settings = Settings.load()
    set_theme(app, theme_override if theme_override in THEMES else settings.theme)
    tasks.run_in_background(lambda: _remove_stale_folders(settings), lambda _n: None,
                            lambda _e: None)
    smoke_test = bool(os.environ.get("TRAYTRIM_SMOKE_TEST"))
    window = MainWindow(settings, check_updates=not smoke_test and settings.check_updates
                        and os.environ.get("TRAYTRIM_NO_UPDATE_CHECK", "0") in ("", "0"))
    window.show()
    if smoke_test:
        # Packaging check: build the window, then quit cleanly (exit code 0).
        QTimer.singleShot(3000, app.quit)
    code = app.exec()
    sys.excepthook, threading.excepthook = sys.__excepthook__, threading.__excepthook__
    # Tear down in order while Qt is still alive: stop and wait for background work, delete
    # the window, drop cached images. Left to Python's shutdown, Qt objects can be destroyed
    # after the application object, which crashes the process on exit.
    window.stop_work()
    if not tasks.shutdown():
        # A worker is stuck in something it can't stop (a huge read on a slow drive). Tearing
        # Qt down under it would crash, and everything worth keeping (settings, the index) is
        # already saved: leave without running Python's shutdown.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    window.deleteLater()
    del window
    icons.image.cache_clear()
    app.sendPostedEvents(None, 0)  # runs the deleteLater
    gc.collect()
    return code
