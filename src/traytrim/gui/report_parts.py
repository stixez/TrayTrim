"""The pieces of a report page: the export size (Hero), a verdict, notices, and the page itself.
The CC cards and list are in cc_view.py."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QMenu, QProgressBar, QPushButton,
                               QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from traytrim.export import package_groups
from traytrim.gui import icons, theme
from traytrim.gui.describe import plural
from traytrim.gui.theme import RADIUS, SPACE
from traytrim.gui.widgets import LoadingPanel, Spinner, label, link_button, set_role
from traytrim.plan import Plan
from traytrim.units import human_size, percent_smaller

# A received zip with more CC items than this shows them without pictures (they're read from
# the zip all at once).
MAX_CARDS = 300


class Picture(QWidget):
    """A tray item's own picture, rounded, `width` wide at its own aspect ratio."""

    def __init__(self, image: QImage, width: int, parent=None):
        super().__init__(parent)
        self._image = image
        height = round(width * image.height() / max(image.width(), 1))
        self.setFixedSize(width, height)

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()), RADIUS["control"], RADIUS["control"])
        painter.setClipPath(path)
        painter.drawImage(QRectF(self.rect()), self._image)


class Hero(QWidget):
    """The one number that matters: how big the export is, next to whole packages, and the
    one action: Export."""

    changed = Signal()  # what it shows, or whether Export can be pressed

    def __init__(self, on_export, noun: str = "household", parent=None):
        super().__init__(parent)
        self._noun = noun
        self._following = ("meshes, textures and sliders" if noun == "household"
                           else "models and textures")
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["xs"])
        self.number = label("", "hero")
        self.line = label("", "secondary", wrap=True)
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        self.saving = label("", "saving")
        self.spinner = Spinner(28)
        self.spinner.hide()
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.number, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(self.saving, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addStretch(1)
        self.export_button = QPushButton("Export")
        self.export_button.setProperty("kind", "primary")
        self.export_button.setIcon(icons.icon("export", theme.current().on_accent, 18))
        self.export_button.setIconSize(QSize(18, 18))
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip("Save the CC, a package per item, and the tray files as one zip (Ctrl+E)")
        self.export_button.clicked.connect(on_export)
        self.export_button.setEnabled(False)
        row.addWidget(self.export_button, 0, Qt.AlignmentFlag.AlignVCenter)
        col.addLayout(row)
        col.addWidget(self.line)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(220, 4)
        self.bar.setAccessibleName("Working out the export")
        col.addSpacing(SPACE["xs"])
        col.addWidget(self.bar)
        self._retry: QPushButton | None = None

    def show_working(self, done: int = 0, total: int = 0) -> None:
        self.export_button.setEnabled(False)
        if self._retry is not None:
            self._retry.hide()
        self.number.setText("Working out the export")
        set_role(self.number, "heroPending")
        self.spinner.show()
        self.saving.setText("")
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.line.setText(f"Following {self._following}: {done:,} of {total:,} files "
                              "so far.")
        else:
            self.bar.setRange(0, 0)
            self.line.setText(f"Following {self._following}.")
        self.bar.show()
        self.changed.emit()

    def show_error(self, reason: str, on_retry) -> None:
        self.export_button.setEnabled(False)
        self.bar.hide()
        self.spinner.hide()
        set_role(self.number, "heroPending")
        self.number.setText("Couldn't work out the export")
        self.saving.setText("")
        self.line.setText(f"{reason}. If the index is being updated, wait for it to finish.")
        self._on_retry = on_retry
        if self._retry is None:
            self._retry = link_button("Try again")
            self._retry.clicked.connect(lambda: self._on_retry())
            self.layout().addWidget(self._retry)
        self._retry.show()
        self.changed.emit()

    def show_plan(self, plan: Plan) -> None:
        self.bar.hide()
        self.spinner.hide()
        if self._retry is not None:
            self._retry.hide()
        set_role(self.number, "hero")
        self.export_button.setEnabled(bool(plan.resources))
        if not plan.resources:
            self.number.setText("Nothing to export")
            self.saving.setText("")
            self.line.setText(f"Every CC item is left out, or the {self._noun} only uses game "
                              "content.")
            self.changed.emit()
            return
        size, whole = plan.export_size, plan.whole_size
        self.number.setText(human_size(size))
        self.saving.setText(percent_smaller(size, whole) or "")
        n_whole = len(plan.packages_with_copies)
        n_packages = len(package_groups(plan))
        self.line.setText(
            f"{plural(n_packages, 'package')}, one per CC item, with "
            f"{plural(len(plan.resources), 'file')} taken from "
            f"{plural(len(plan.source_packages), 'CC package')}. Copying "
            f"{'the' if n_whole > 1 else 'that'} {plural(n_whole, 'whole package')} instead "
            f"would take {human_size(whole)}.")
        self.changed.emit()


class StickyBar(QWidget):
    """The export's name, size and Export button over the top of the page while the page is
    scrolled past them (Hero), so exporting never needs a scroll back up."""

    def __init__(self, on_export, parent=None):
        super().__init__(parent)
        self.setObjectName("stickyBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        row = QHBoxLayout(self)
        row.setContentsMargins(SPACE["xxl"] + SPACE["s"], SPACE["s"], SPACE["xxl"] + SPACE["s"],
                               SPACE["s"])
        row.setSpacing(SPACE["m"])
        self.title = label("", "title")
        self.size_label = label("", "title")
        self.saving = label("", "saving")
        self.export_button = QPushButton("Export")
        self.export_button.setProperty("kind", "primary")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip("Save the CC, a package per item, and the tray files as one "
                                      "zip (Ctrl+E)")
        self.export_button.setAccessibleName("Export (from the bar at the top)")
        self.export_button.clicked.connect(on_export)
        row.addWidget(self.title)
        row.addWidget(label("\u00b7", "secondary"))
        row.addWidget(self.size_label)
        row.addWidget(self.saving)
        row.addStretch(1)
        row.addWidget(self.export_button)
        self.refresh_theme()
        self.hide()

    def set_title(self, title: str) -> None:
        self.title.setText(title)

    def copy(self, hero: Hero) -> None:
        """Show what the page's Hero shows: the size, or that it's being worked out."""
        pending = hero.number.property("role") == "heroPending"
        self.size_label.setText(hero.number.text() if not pending else "Working out the export\u2026")
        set_role(self.size_label, "secondary" if pending else "title")
        self.saving.setText(hero.saving.text())
        self.export_button.setEnabled(hero.export_button.isEnabled())

    def refresh_theme(self) -> None:
        self.export_button.setIcon(icons.icon("export", theme.current().on_accent, 18))
        self.export_button.setIconSize(QSize(18, 18))


class Verdict(QWidget):
    """The headline of a Mods package or a received zip: one plain fact in large type, a line
    under it, and a progress bar while it's worked out. Shaped like Hero, with an optional
    button in Export's place."""

    def __init__(self, button_text: str = "", on_button=None, button_icon: str = "",
                 parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["xs"])
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        self.text = label("", "hero", wrap=True)
        self.spinner = Spinner(28)
        self.spinner.hide()
        row.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.text, 1, Qt.AlignmentFlag.AlignVCenter)
        self.button: QPushButton | None = None
        if button_text:
            self.button = QPushButton(button_text)
            self.button.setProperty("kind", "primary")
            if button_icon:
                self.button.setIcon(icons.icon(button_icon, theme.current().on_accent, 18))
                self.button.setIconSize(QSize(18, 18))
            self.button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.button.clicked.connect(on_button)
            row.addWidget(self.button, 0, Qt.AlignmentFlag.AlignVCenter)
        col.addLayout(row)
        self.line = label("", "secondary", wrap=True)
        col.addWidget(self.line)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(220, 4)
        col.addSpacing(SPACE["xs"])
        col.addWidget(self.bar)
        self.bar.hide()

    def show_working(self, text: str, line: str = "", done: int = 0, total: int = 0) -> None:
        set_role(self.text, "heroPending")
        self.text.setText(text)
        self.line.setText(line)
        self.line.setVisible(bool(line))
        self.bar.setRange(0, total if total else 0)
        self.bar.setValue(done)
        self.bar.setAccessibleName(text)
        self.bar.show()
        self.spinner.show()

    def set_result(self, text: str, line: str = "") -> None:
        set_role(self.text, "hero")
        self.text.setText(text)
        self.line.setText(line)
        self.line.setVisible(bool(line))
        self.bar.hide()
        self.spinner.hide()


class NoticeStack(QWidget):
    """Notices one under another, with a divider between them."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(0, 0, 0, 0)
        self._col.setSpacing(0)

    def add(self, notice: QWidget) -> None:
        if self._col.count():
            line = QFrame()
            line.setObjectName("divider")
            line.setFixedHeight(1)
            self._col.addWidget(line)
        self._col.addWidget(notice)

    def clear(self) -> None:
        while self._col.count():
            w = self._col.takeAt(0).widget()
            if w is not None:
                w.deleteLater()


class ReportPage(QWidget):
    """A right-hand view: a centred message (empty, loading, error) or a scrolling page."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.message_page = LoadingPanel()
        self.message_title, self.message_body = self.message_page.title, self.message_page.body
        self.stack.addWidget(self.message_page)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Never sideways: anything too wide wraps or is shortened instead.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stack.addWidget(self.scroll)

    def show_message(self, title: str, body: str = "", loading: bool = False, done: int = 0,
                     total: int = 0) -> None:
        """loading: a loader screen, with a spinner (and a bar when `total` is known)."""
        self.message_page.set_state(title, body, loading, done, total)
        self.stack.setCurrentWidget(self.message_page)

    def new_page(self) -> QVBoxLayout:
        """An empty page with the report margins, shown in place of the old one; returns its
        layout."""
        page = QWidget()
        page.setObjectName("content")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        col = QVBoxLayout(page)
        col.setContentsMargins(SPACE["xxl"] + SPACE["s"], SPACE["xl"] + SPACE["s"],
                               SPACE["xxl"] + SPACE["s"], SPACE["xxl"])
        col.setSpacing(0)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)
        self.stack.setCurrentWidget(self.scroll)
        return col

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(800, 600)


def item_menu(parent: QWidget, pos, on_export) -> None:
    """The menu of a CC item: export it on its own."""
    menu = QMenu(parent)
    menu.addAction("Export this item alone\u2026").triggered.connect(on_export)
    menu.exec(pos)
    menu.deleteLater()
