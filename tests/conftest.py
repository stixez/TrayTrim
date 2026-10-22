"""Shared test setup."""

import gc
import sys

import pytest


@pytest.fixture(scope="session", autouse=True)
def tidy_qt_at_exit():
    """GUI tests leave widgets and cached icon images behind. Free them while Qt is still up:
    left to Python's shutdown they can outlive the application object and crash the test
    process after every test has passed (seen on Linux and Windows CI)."""
    yield
    if "PySide6" not in sys.modules or "traytrim.gui.icons" not in sys.modules:
        return
    from PySide6.QtWidgets import QApplication

    from traytrim.gui import icons, tasks

    tasks.shutdown()
    app = QApplication.instance()
    if app is not None:
        for widget in app.topLevelWidgets():
            widget.close()
            widget.deleteLater()
        app.sendPostedEvents(None, 0)
    icons.image.cache_clear()
    gc.collect()
