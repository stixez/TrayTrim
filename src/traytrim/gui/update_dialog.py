"""A newer TrayTrim is out: what's new, and its download for this platform."""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtWidgets import QDialog, QFrame, QHBoxLayout, QPushButton, QTextBrowser, QVBoxLayout

from traytrim import __version__
from traytrim.gui import system
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import label, link_button
from traytrim.settings import Settings
from traytrim.updates import Release, available_update


class UpdateCheck(QObject):
    """Asks GitHub on a thread of its own, not the app's worker pool: a request can't be
    cancelled, and on a bad network it could otherwise hold up closing the app (which waits
    for the pool). `found` arrives on the UI thread with a newer release."""

    found = Signal(object)

    def start(self, skipped: str | None) -> None:
        threading.Thread(target=self._run, args=(skipped,), daemon=True,
                         name="update-check").start()

    def _run(self, skipped: str | None) -> None:
        try:
            release = available_update(skipped)
        except Exception:  # noqa: BLE001 - a failed check is silent; the next start tries again
            return
        if release is not None:
            try:
                self.found.emit(release)
            except RuntimeError:  # the window was closed meanwhile
                pass


def open_https(url: QUrl) -> None:
    """A link in the release notes: only web pages (the notes are text from the internet)."""
    if url.scheme() == "https":
        system.open_url(url.toString())


class UpdateDialog(QDialog):
    """Download opens this platform's file in the browser; Skip this version stops asking
    until a newer one comes out; Later asks again at the next start."""

    def __init__(self, release: Release, settings: Settings, parent=None):
        super().__init__(parent)
        self.release = release
        self.settings = settings
        self.setWindowTitle("Update available")
        self.setMinimumWidth(520)
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        col.setSpacing(SPACE["s"])
        col.addWidget(label(f"TrayTrim {release.version} is available", "title"))
        col.addWidget(label(f"You have {__version__}. Download the new version and install it "
                            "over this one; your settings and export history stay.",
                            "secondary", wrap=True))
        if release.notes:
            col.addSpacing(SPACE["s"])
            col.addWidget(label("What's new", "noticeTitle"))
            notes = QTextBrowser()
            notes.setOpenLinks(False)
            notes.anchorClicked.connect(open_https)
            notes.setFrameShape(QFrame.Shape.NoFrame)
            notes.setMarkdown(release.notes)
            notes.setMinimumHeight(160)
            notes.setMaximumHeight(320)
            col.addWidget(notes, 1)
        col.addSpacing(SPACE["m"])
        buttons = QHBoxLayout()
        skip = link_button("Skip this version")
        skip.setToolTip("Don't ask about this version again. A newer one is still offered.")
        skip.clicked.connect(self._skip)
        buttons.addWidget(skip)
        buttons.addStretch(1)
        later = QPushButton("Later")
        later.setToolTip("Ask again the next time TrayTrim starts")
        later.clicked.connect(self.reject)
        download = QPushButton("Download")
        download.setProperty("kind", "primary")
        download.setDefault(True)
        download.setToolTip(release.download)
        download.clicked.connect(self._download)
        buttons.addWidget(later)
        buttons.addWidget(download)
        col.addLayout(buttons)
        self.download_button = download

    def _download(self) -> None:
        system.open_url(self.release.download)
        self.accept()

    def _skip(self) -> None:
        self.settings.skipped_version = self.release.version
        try:
            self.settings.save()
        except OSError:
            pass  # it asks again next time; not worth an error
        self.reject()
