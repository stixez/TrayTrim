"""The pieces of a household report: the export size, CC cards in sections, and the CC list."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath
from PySide6.QtWidgets import (QFrame, QGraphicsOpacityEffect, QHBoxLayout, QHeaderView,
                               QProgressBar, QPushButton, QSizePolicy, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from traytrim.gui import icons, theme
from traytrim.gui.describe import (THUMB_TYPES, card_caption, category_icon, display_name,
                                   item_label, plural, usage_summary, usage_text)
from traytrim.gui.flow import FlowLayout
from traytrim.gui.theme import RADIUS, SPACE
from traytrim.gui.widgets import ClampedText, IconLabel, label, link_button, set_role
from traytrim.names import ItemName
from traytrim.plan import Plan
from traytrim.resolve import LocatedRef
from traytrim.tray import TrayRef
from traytrim.units import human_size, percent_smaller

CARD_WIDTH = 140
THUMB = 124


class Thumbnail(QWidget):
    """A square preview: blank while loading, the image, or the item's category icon with
    "No preview" when there's no image."""

    def __init__(self, size: int, icon_name: str, loading: bool, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._image: QImage | None = None
        self._icon = icon_name
        self._missing = not loading

    def set_image(self, image: QImage | None) -> None:
        self._image = image
        self.update()

    def set_missing(self) -> None:
        if self._image is None and not self._missing:
            self._missing = True
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, RADIUS["control"], RADIUS["control"])
        painter.setClipPath(path)
        painter.fillRect(r, QColor(p.sidebar))
        if self._image is not None:
            img = self._image
            scale = min(r.width() / img.width(), r.height() / img.height())
            w, h = img.width() * scale, img.height() * scale
            painter.drawImage(QRectF((r.width() - w) / 2, (r.height() - h) / 2, w, h), img)
        elif self._missing:
            icon_size = 48
            top = (r.height() - icon_size - 22) / 2
            img = icons.image(self._icon, p.text_tertiary, icon_size, self.devicePixelRatioF())
            painter.drawImage(QRectF((r.width() - icon_size) / 2, top, icon_size, icon_size),
                              img)
            painter.setPen(QColor(p.text_tertiary))
            painter.setFont(theme.ui_font("caption"))
            painter.drawText(QRectF(0, top + icon_size + 6, r.width(), 16),
                             Qt.AlignmentFlag.AlignCenter, "No preview")


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


class CcCard(QFrame):
    """One CC item. Click (or Space) to leave it out of the export or put it back."""

    toggled = Signal(object, bool)  # TrayRef, included

    def __init__(self, ref: LocatedRef, included: bool, name: ItemName | None = None,
                 parent=None):
        super().__init__(parent)
        self.ref = ref
        self.name = name
        self._included = included
        self.setObjectName("ccCard")
        self.setFixedWidth(CARD_WIDTH)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["s"], SPACE["s"], SPACE["s"], SPACE["s"])
        outer.setSpacing(SPACE["xs"])
        # Everything but the "Not exported" row sits in `body`, which fades when left out.
        self.body = QWidget()
        col = QVBoxLayout(self.body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["xs"])
        outer.addWidget(self.body)
        # Kinds with a thumbnail wait for it; the others never have one.
        self.thumb = Thumbnail(THUMB, category_icon(ref), ref.ref.kind in THUMB_TYPES)
        col.addWidget(self.thumb)
        col.addSpacing(SPACE["xs"])
        col.addWidget(label(card_caption(ref), "caption"))
        self.title = ClampedText(display_name(ref, name), 2)
        col.addWidget(self.title)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, SPACE["xs"], 0, 0)
        foot.setSpacing(SPACE["xs"])
        self.size_label = label("", "caption")
        foot.addWidget(self.size_label)
        foot.addStretch(1)
        if ref.approximate:
            approx = IconLabel("info", "text_secondary", 16)
            approx.setToolTip("Approximate match: found by its id only, because the type of "
                              "this kind of item isn't verified yet.")
            foot.addWidget(approx)
        if len(ref.mods) > 1:
            copies = IconLabel("copies", "warning_text", 16)
            copies.setToolTip(f"{len(ref.mods)} copies in your Mods folder")
            foot.addWidget(copies)
        col.addLayout(foot)
        self.state = QWidget()
        state_row = QHBoxLayout(self.state)
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.setSpacing(SPACE["xs"])
        state_row.addWidget(IconLabel("excluded", "text_secondary", 16))
        state_row.addWidget(label("Not exported", "caption"))
        state_row.addStretch(1)
        outer.addWidget(self.state)
        self._fade = QGraphicsOpacityEffect(self.body)
        self._fade.setOpacity(0.4)
        self.body.setGraphicsEffect(self._fade)
        self._apply()
        self._set_tooltip()

    @property
    def included(self) -> bool:
        return self._included

    def set_size(self, size: int | None) -> None:
        self.size_label.setText(human_size(size) if size else "")

    def set_name(self, name: ItemName | None) -> None:
        if name is not None and name != self.name:
            self.name = name
            self.title.set_text(display_name(self.ref, name))
            self._apply()
            self._set_tooltip()

    def _set_tooltip(self) -> None:
        r = self.ref
        lines = []
        if self.name is not None:
            lines.append(self.name.title + (f" by {self.name.creator}" if self.name.creator
                                            else ""))
            if self.name.raw and self.name.raw != self.name.title:
                lines.append(self.name.raw)
            lines.append("")
        lines.append(r.used.rel if r.used else "")
        if len(r.mods) > 1:
            lines += ["", "Also in:"] + [h.rel for h in r.mods[:-1]]
            lines += ["", "The game uses the copy that loads last (alphabetical order in Mods)."]
        if r.approximate:
            lines += ["", "Approximate match: found by its id only."]
        lines += ["", usage_text(r), "",
                  "Click to leave it out of the export." if self._included
                  else "Click to put it back in the export."]
        self.setToolTip("\n".join(lines).strip())

    def _apply(self) -> None:
        self._fade.setEnabled(not self._included)
        self.state.setVisible(not self._included)
        self.setProperty("excluded", not self._included)
        self.style().unpolish(self)
        self.style().polish(self)
        verb = "Included in" if self._included else "Left out of"
        approx = " Approximate match." if self.ref.approximate else ""
        self.setAccessibleName(f"{item_label(self.ref)}, {display_name(self.ref, self.name)}. "
                               f"{verb} the export.{approx}")

    def toggle(self) -> None:
        self._included = not self._included
        self._apply()
        self._set_tooltip()
        self.toggled.emit(self.ref.ref, self._included)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
                event.position().toPoint()):
            self.toggle()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.toggle()
            return
        super().keyPressEvent(event)


class Section(QWidget):
    def __init__(self, title: str, cards: list[CcCard], parent=None):
        super().__init__(parent)
        self.cards = cards
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, SPACE["xl"])
        col.setSpacing(SPACE["m"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["s"])
        head.addWidget(label(title, "title"))
        self.count_label = label(str(len(cards)), "secondary")
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addStretch(1)
        self.size_label = label("", "secondary")
        head.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignBaseline)
        col.addLayout(head)
        holder = QWidget()
        flow = FlowLayout(holder, spacing=SPACE["m"])
        self._flow = flow
        for card in cards:
            flow.addWidget(card)
        holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        col.addWidget(holder)

    def set_size(self, size: int | None) -> None:
        self.size_label.setText(human_size(size) if size else "")

    def reorder(self, key) -> None:
        """Put the cards in a new order without building them again."""
        self.cards.sort(key=key)
        while self._flow.count():
            self._flow.takeAt(0)
        for card in self.cards:
            self._flow.addWidget(card)
        self._flow.invalidate()

    def show_matching(self, matching: int) -> None:
        self.setVisible(matching > 0)
        n = len(self.cards)
        self.count_label.setText(str(n) if matching == n else f"{matching} of {n}")


class Hero(QWidget):
    """The one number that matters: how big the export is, next to whole packages, and the
    one action: Export."""

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
        row.addWidget(self.number, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addWidget(self.saving, 0, Qt.AlignmentFlag.AlignBaseline)
        row.addStretch(1)
        self.export_button = QPushButton("Export")
        self.export_button.setProperty("kind", "primary")
        self.export_button.setIcon(icons.icon("export", theme.current().on_accent, 18))
        self.export_button.setIconSize(QSize(18, 18))
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip("Save the package and tray files as one zip (Ctrl+E)")
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

    def show_error(self, reason: str, on_retry) -> None:
        self.export_button.setEnabled(False)
        self.bar.hide()
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

    def show_plan(self, plan: Plan) -> None:
        self.bar.hide()
        if self._retry is not None:
            self._retry.hide()
        set_role(self.number, "hero")
        self.export_button.setEnabled(bool(plan.resources))
        if not plan.resources:
            self.number.setText("Nothing to export")
            self.saving.setText("")
            self.line.setText(f"Every CC item is left out, or the {self._noun} only uses game "
                              "content.")
            return
        size, whole = plan.export_size, plan.whole_size
        self.number.setText(human_size(size))
        self.saving.setText(percent_smaller(size, whole) or "")
        n_whole = len(plan.packages_with_copies)
        self.line.setText(
            f"One package with {plural(len(plan.resources), 'file')} taken from "
            f"{plural(len(plan.source_packages), 'CC package')}. Copying "
            f"{'the' if n_whole > 1 else 'that'} {plural(n_whole, 'whole package')} instead "
            f"would take {human_size(whole)}.")


class CcList(QTreeWidget):
    COLUMNS = 5

    def __init__(self, groups: list[tuple[str, list[LocatedRef]]], with_names: bool,
                 excluded: set[TrayRef], names: dict[TrayRef, ItemName], parent=None):
        super().__init__(parent)
        self.setColumnCount(self.COLUMNS)
        self.setHeaderLabels(["Item", "Name", "Package", "Used in", "Notes"])
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(3, 240 if with_names else 180)
        bold = theme.ui_font("body", QFont.Weight.DemiBold)
        dim = QColor(theme.current().text_tertiary)
        self.rows: list[tuple[QTreeWidgetItem, LocatedRef]] = []
        self.tops: list[tuple[QTreeWidgetItem, str, int]] = []
        for title, refs in groups:
            top = QTreeWidgetItem([f"{title} ({len(refs)})"])
            top.setFont(0, bold)
            self.addTopLevelItem(top)
            self.tops.append((top, title, len(refs)))
            for r in refs:
                notes = []
                if r.ref in excluded:
                    notes.append("Not exported")
                if len(r.mods) > 1:
                    notes.append(f"{len(r.mods)} copies in Mods")
                if r.approximate:
                    notes.append("Approximate match")
                name = names.get(r.ref)
                child = QTreeWidgetItem([item_label(r), name.title if name else "",
                                         r.used.rel if r.used else "",
                                         usage_summary(r, with_names), ", ".join(notes)])
                if name is not None and name.raw != name.title:
                    child.setToolTip(1, name.raw)
                child.setToolTip(2, r.used.rel if r.used else "")
                child.setToolTip(3, usage_text(r))
                if r.ref in excluded:
                    for c in range(self.COLUMNS):
                        child.setForeground(c, dim)
                top.addChild(child)
                self.rows.append((child, r))
            top.setExpanded(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.itemExpanded.connect(self._fit_height)
        self.itemCollapsed.connect(self._fit_height)
        self._fit_height()

    def show_matching(self, matches) -> int:
        """Hide the rows `matches(ref)` rejects, and groups left empty. Returns rows shown."""
        shown = 0
        visible: dict[int, int] = {}
        for child, r in self.rows:
            ok = matches(r)
            child.setHidden(not ok)
            if ok:
                shown += 1
                parent = child.parent()
                visible[id(parent)] = visible.get(id(parent), 0) + 1
        for top, title, n in self.tops:
            k = visible.get(id(top), 0)
            top.setHidden(k == 0)
            top.setText(0, f"{title} ({n})" if k == n else f"{title} ({k} of {n})")
        self._fit_height()
        return shown

    def _fit_height(self, *_args) -> None:
        """Tall enough for every visible row, so the page scrolls instead of a box inside it."""
        rows = 0
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            if top.isHidden():
                continue
            children = sum(1 for j in range(top.childCount()) if not top.child(j).isHidden())
            rows += 1 + (children if top.isExpanded() else 0)
        row_height = self.sizeHintForRow(0) if rows else 0
        self.setFixedHeight(self.header().sizeHint().height() + rows * row_height + 4)
