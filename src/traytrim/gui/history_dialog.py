"""Earlier exports: where they are, and a way to check a zip again later."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from traytrim.export import load_history
from traytrim.gui import system
from traytrim.gui.tasks import BackgroundTask, describe_error
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import IconLabel, label, link_button
from traytrim.units import human_size
from traytrim.verify import Verification, check_zip


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
    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.zip = Path(entry.get("zip") or "")
        self._task: BackgroundTask | None = None
        name = entry.get("name") or entry.get("household") or "Unnamed"
        kind = str(entry.get("kind") or "household").capitalize()
        exists = self.zip.is_file()
        size = entry.get("bytes")
        state = ("A check failed" if entry.get("verified") is False else "Verified")
        meta = [kind, when_text(entry.get("time", ""))]
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
        self.check_button = link_button("Check again")
        self.check_button.setAccessibleName(f"Check the export of {name} again")
        self.check_button.clicked.connect(self._check)
        for b in (self.show_button, self.check_button):
            b.setEnabled(exists)
            head.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(head)
        meta_label = label(" · ".join(m for m in meta if m), "secondary", wrap=True)
        meta_label.setToolTip(str(self.zip))
        col.addWidget(meta_label)
        self.result = QWidget()
        self.result_col = QVBoxLayout(self.result)
        self.result_col.setContentsMargins(0, SPACE["xs"], 0, 0)
        self.result_col.setSpacing(SPACE["xs"])
        self.result.hide()
        col.addWidget(self.result)

    def _clear_result(self) -> None:
        _delete_layout(self.result_col)

    def _line(self, icon: str, token: str, title: str, detail: str = "") -> None:
        row = QHBoxLayout()
        row.setSpacing(SPACE["s"])
        row.addWidget(IconLabel(icon, token, 16), 0, Qt.AlignmentFlag.AlignTop)
        text = label(f"{title}{': ' + detail if detail else ''}", "secondary", wrap=True)
        row.addWidget(text, 1)
        self.result_col.addLayout(row)

    def _check(self) -> None:
        if self._task is not None:
            return
        self.check_button.setEnabled(False)
        self._clear_result()
        self.result_col.addWidget(label("Checking", "secondary"))
        self.result.show()
        path = self.zip
        task = BackgroundTask(lambda t: check_zip(path, t.report, t.cancelled))
        task.progress.connect(self._progress)
        task.finished.connect(self._checked)
        task.failed.connect(self._failed)
        self._task = task
        task.start()

    def _progress(self, args: tuple) -> None:
        step, done, total = args
        item = self.result_col.itemAt(0)
        if item is not None and item.widget() is not None:
            item.widget().setText(
                f"Reading the zip: {done * 100 // max(total, 1)}%" if step == "read"
                else f"Checking: {done:,} of {total:,} files")

    def _checked(self, verification: Verification) -> None:
        self._task = None
        self.check_button.setEnabled(self.zip.is_file())
        self._clear_result()
        if verification.ok:
            self._line("check", "success_text", "Checked just now, still intact",
                       verification.checks[-1].detail)
        else:
            for c in verification.checks:
                if not c.passed:
                    detail = c.detail + (f" {c.problems[0]}" if c.problems else "")
                    self._line("alert", "danger_text", c.name, detail)

    def _failed(self, error: Exception) -> None:
        self._task = None
        self.check_button.setEnabled(self.zip.is_file())
        self._clear_result()
        self._line("alert", "danger_text", "Couldn't check it", describe_error(error))

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()


def _delete_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _delete_layout(item.layout())


class HistoryDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
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
                "and check it again later.", "secondary", wrap=True))
            outer.addStretch(1)
        else:
            outer.addWidget(label(
                "Check again reads a zip back on its own: it's intact, its package "
                "decompresses, and it still matches the report saved with it. Your Mods "
                "folder isn't needed for that.", "secondary", wrap=True))
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
                row = HistoryRow(entry)
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

    def done(self, result: int) -> None:
        for row in self.rows:
            row.stop()
        super().done(result)
