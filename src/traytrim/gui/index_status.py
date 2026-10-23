"""Keeps the resource index up to date in the background and shows its state in the status bar."""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QPlainTextEdit, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from traytrim.gui.tasks import BackgroundTask, describe_error
from traytrim.gui.theme import SPACE, mono_font
from traytrim.gui.widgets import label, link_button, set_role
from traytrim.index import MODS, ResourceIndex, UpdateResult


class IndexStatus(QWidget):
    """Status-bar slot: progress while indexing, then "ready", skipped packages, or an error."""

    def __init__(self, on_details: Callable[[], None], on_retry: Callable[[], None], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACE["s"], 0, SPACE["l"], 0)
        lay.setSpacing(SPACE["s"])
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(96, 4)
        self.bar.setAccessibleName("Indexing progress")
        self.text = label("", "caption")
        self.details = link_button("Details")
        self.details.clicked.connect(on_details)
        self.retry = link_button("Try again")
        self.retry.clicked.connect(on_retry)
        for w in (self.bar, self.text, self.details, self.retry):
            lay.addWidget(w)
        self.show_idle()

    def _show(self, text: str, role: str = "caption", *, bar: bool = False,
              details: bool = False, retry: bool = False) -> None:
        self.text.setText(text)
        set_role(self.text, role)
        self.bar.setVisible(bar)
        self.details.setVisible(details)
        self.retry.setVisible(retry)

    def show_idle(self) -> None:
        self._show("")

    def show_listing(self) -> None:
        self.bar.setRange(0, 0)
        self._show("Looking for packages", bar=True)

    def show_progress(self, phase: str, done: int, total: int) -> None:
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)
        what = "Mods" if phase == MODS else "game"
        self._show(f"Reading {what} packages: {done:,} of {total:,}", bar=True)

    def show_result(self, r: UpdateResult) -> None:
        skipped = len(r.failed) + len(r.too_deep)
        if skipped:
            noun = "package" if skipped == 1 else "packages"
            self._show(f"Index ready, {skipped:,} {noun} skipped", "warning", details=True)
        else:
            self._show("Index ready")
        self.setToolTip(f"{r.mods_packages:,} Mods packages ({r.mods_resources:,} resources)\n"
                        f"{r.game_packages:,} game packages ({r.game_resources:,} resources)")

    def show_error(self, message: str) -> None:
        self._show(f"Indexing stopped: {message}", "danger", retry=True)


class IndexController(QObject):
    """Runs index updates one at a time; a request while one runs restarts it afterwards."""

    result_ready = Signal(object)  # UpdateResult, after a successful update
    stopped = Signal()  # an update failed or was cancelled; the cache may still be usable

    def __init__(self, status: IndexStatus, parent=None):
        super().__init__(parent)
        self.status = status
        self.last_result: UpdateResult | None = None
        self._task: BackgroundTask | None = None
        self._pending: tuple[Path | None, Path | None] | None = None

    def refresh(self, mods: Path | None, game: Path | None) -> None:
        if self._task is not None and self._task.running:
            self._pending = (mods, game)
            self._task.cancel()
            return
        self._start(mods, game)

    def _start(self, mods: Path | None, game: Path | None) -> None:
        def work(task: BackgroundTask) -> UpdateResult:
            task.report("listing", force=True)
            with ResourceIndex() as ix:
                return ix.update(mods, game,
                                 lambda phase, done, total: task.report(
                                     phase, done, total, force=done == total),
                                 task.cancelled)

        task = BackgroundTask(work)
        task.progress.connect(self._on_progress)
        task.finished.connect(self._on_finished)
        task.failed.connect(self._on_failed)
        task.aborted.connect(self._on_aborted)
        self._task = task
        task.start()

    def _on_progress(self, args: tuple) -> None:
        if args[0] == "listing":
            self.status.show_listing()
        else:
            self.status.show_progress(*args)

    def _on_finished(self, result: UpdateResult) -> None:
        if self._run_pending():
            return
        self.last_result = result
        self.status.show_result(result)
        self.result_ready.emit(result)

    def _on_aborted(self) -> None:
        if self._run_pending():
            return
        self.status.show_idle()
        self.stopped.emit()

    def _on_failed(self, error: Exception) -> None:
        if self._run_pending():
            return
        self.status.show_error(describe_error(error))
        self.stopped.emit()

    def _run_pending(self) -> bool:
        if self._pending is None:
            return False
        mods, game = self._pending
        self._pending = None
        self._start(mods, game)
        return True

    def stop(self) -> None:
        """Ask a running update to stop; it commits what it has, so the next run resumes."""
        self._pending = None
        if self._task is not None:
            self._task.cancel()


class SkippedPackagesDialog(QDialog):
    def __init__(self, result: UpdateResult, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Skipped packages")
        self.setMinimumSize(640, 420)
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        col.setSpacing(SPACE["m"])

        lines: list[str] = []
        if result.failed:
            n = len(result.failed)
            heading = ("1 package couldn't be read" if n == 1
                       else f"{n:,} packages couldn't be read")
            col.addWidget(label(heading, "title"))
            col.addWidget(label(
                "CC inside these packages can't be found or exported. A package can be locked "
                "while the game is running: close The Sims 4 and reopen TrayTrim. Packages that "
                "stay on this list are probably damaged; Sims 4 Studio's batch fix or a fresh "
                "download usually helps.", "secondary", wrap=True))
            lines += [f"{path}\n    {reason}" for path, reason in result.failed]
        if result.too_deep:
            if lines:
                lines.append("")
                col.addSpacing(SPACE["m"])
            n = len(result.too_deep)
            heading = ("1 package is too deep in Mods" if n == 1
                       else f"{n:,} packages are too deep in Mods")
            col.addWidget(label(heading, "title"))
            col.addWidget(label(
                f"The game only loads packages up to {result.mods_depth} folders deep (set in "
                "Mods/Resource.cfg), so it never sees these and TrayTrim skips them too.",
                "secondary", wrap=True))
            lines += [f"Mods/{rel}" for rel in result.too_deep]

        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setFont(mono_font())
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        col.addWidget(text, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy = QPushButton("Copy list")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(text.toPlainText()))
        close = QPushButton("Close")
        close.setProperty("kind", "primary")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addWidget(close)
        col.addLayout(buttons)
