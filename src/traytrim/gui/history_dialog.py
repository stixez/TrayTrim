"""Earlier exports: where they are, and a way to check one again (in Check a zip)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from traytrim.export import load_history
from traytrim.gui import system
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import label, link_button
from traytrim.units import human_size


# English month names whatever the system locale (strftime's %b follows it); the UI is English.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def when_text(stamp: str, now: datetime | None = None) -> str:
    """"Today, 23:16", "Yesterday, 09:02" or "3 Oct 2026, 14:00"."""
    try:
        t = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return ""
    now = now or datetime.now()
    days = (now.date() - t.date()).days
    day = ("Today" if days == 0 else "Yesterday" if days == 1
           else f"{t.day} {_MONTHS[t.month - 1]} {t.year}")
    return f"{day}, {t:%H:%M}"


class HistoryRow(QWidget):
    def __init__(self, entry: dict, on_check: Callable[[Path], None], parent=None):
        super().__init__(parent)
        def text(key: str) -> str:  # the file can be hand-edited: anything not text is unset
            value = entry.get(key)
            return value if isinstance(value, str) else ""

        self.zip = Path(text("zip"))
        name = text("name") or text("household") or "Unnamed"
        kind = (text("kind") or "household").capitalize()
        exists = bool(text("zip")) and self.zip.is_file()
        size = entry.get("bytes")
        state = ("A check failed" if entry.get("verified") is False else "Verified")
        meta = [kind, when_text(text("time"))]
        if isinstance(size, int):
            meta.append(human_size(size))
        meta.append(state if exists else "The zip isn't there any more")

        col = QVBoxLayout(self)
        col.setContentsMargins(0, SPACE["m"], 0, SPACE["m"])
        col.setSpacing(SPACE["xs"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["m"])
        head.addWidget(label(name, "noticeTitle"), 1)
        self.show_button = link_button("Show in folder")
        self.show_button.setAccessibleName(f"Show {name} in its folder")
        self.show_button.clicked.connect(lambda: system.reveal(self.zip))
        self.check_button = link_button("Check")
        self.check_button.setAccessibleName(f"Check the export of {name}")
        self.check_button.setToolTip("Open it in Check a zip: whether it's still intact, and "
                                     "how it fits your game now.")
        self.check_button.clicked.connect(lambda: on_check(self.zip))
        for b in (self.show_button, self.check_button):
            b.setEnabled(exists)
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)
        meta_label = label(" · ".join(m for m in meta if m), "secondary", wrap=True)
        meta_label.setToolTip(str(self.zip))
        col.addWidget(meta_label)


class HistoryDialog(QDialog):
    """on_check(zip) opens an export in Check a zip; the dialog closes first."""

    def __init__(self, on_check: Callable[[Path], None], parent=None):
        super().__init__(parent)
        self._on_check = on_check
        self.setWindowTitle("Export history")
        self.setMinimumSize(600, 560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        outer.setSpacing(SPACE["s"])
        outer.addWidget(label("Export history", "title"))
        entries = load_history()
        self.rows: list[HistoryRow] = []
        if not entries:
            outer.addWidget(label(
                "No exports yet. Every export you make is listed here, so you can find its zip "
                "and check it later.", "secondary", wrap=True))
            outer.addStretch(1)
        else:
            outer.addWidget(label(
                "Check opens a zip the way the person you send it to sees it: whether it's "
                "intact, which packs it needs, and what's already in your Mods.", "secondary",
                wrap=True))
            holder = QWidget()
            holder.setObjectName("content")
            col = QVBoxLayout(holder)
            col.setContentsMargins(0, 0, SPACE["s"], 0)
            col.setSpacing(0)
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if self.rows:
                    line = QFrame()
                    line.setObjectName("divider")
                    line.setFixedHeight(1)
                    col.addWidget(line)
                row = HistoryRow(entry, self._check)
                self.rows.append(row)
                col.addWidget(row)
            col.addStretch(1)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setWidget(holder)
            outer.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addSpacing(SPACE["s"])
        outer.addLayout(buttons)

    def _check(self, path: Path) -> None:
        self.accept()
        self._on_check(path)
