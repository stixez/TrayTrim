"""The right side of the window: what the selected household uses and what an export would be."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
                               QProgressBar, QPushButton, QScrollArea, QSizePolicy,
                               QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
                               QWidget)

from traytrim import restypes
from traytrim.gui import icons, theme
from traytrim.gui.flow import FlowLayout
from traytrim.gui.theme import RADIUS, SPACE
from traytrim.gui.widgets import ClampedText, label, link_button, set_role
from traytrim.names import ItemName
from traytrim.plan import Plan, PlannedResource
from traytrim.resolve import LocatedRef, Summary, ref_label, summarize
from traytrim.tray import HouseholdContents, RefKind, TrayItem, TrayRef
from traytrim.units import human_size, percent_smaller

CARD_WIDTH = 140
THUMB = 124

# Section order, as CAS shows things; a lot or room has objects and build items.
GROUP_ORDER = ["Hair", "Clothing", "Accessories", "Makeup", "Eyes", "Skin details", "Body",
               "Other CAS parts", "Skin tone", "Sculpts", "Sliders", "Custom tattoos",
               "Pet coat", "Objects", "Build"]
# Kinds with a thumbnail in their package, and its resource type.
THUMB_TYPES = {RefKind.CAS_PART: restypes.THUM, RefKind.OBJECT: restypes.OBJECT_THUMB,
               RefKind.BUILD: restypes.OBJECT_THUMB}


@dataclass
class HouseholdReport:
    item: TrayItem
    contents: HouseholdContents
    refs: list[LocatedRef]
    sim_ids: set[int]
    excluded: set[TrayRef] = field(default_factory=set)
    overrides: set[TrayRef] = field(default_factory=set)  # left out unless the user opts in
    copies: dict = field(default_factory=dict)  # ResourceKey -> package path the user chose

    @property
    def noun(self) -> str:
        return self.contents.kind  # "household", "lot" or "room"


def item_label(ref: LocatedRef) -> str:
    return ref_label(ref.ref)


def item_subtitle(item: TrayItem, with_price: bool = False) -> str:
    """"40 × 30 · 2 bedrooms · 1 bathroom" for a lot, "6 × 4" for a room."""
    parts = []
    if item.size and all(item.size):
        parts.append(f"{item.size[0]} × {item.size[1]}")
    if with_price and item.price:
        parts.append(f"§{item.price:,}")
    if item.bedrooms:
        parts.append(plural(item.bedrooms, "bedroom"))
    if item.bathrooms:
        parts.append(plural(item.bathrooms, "bathroom"))
    return " · ".join(parts)


def card_caption(ref: LocatedRef) -> str:
    """The small line above the package name. Every card in the Objects section is an object,
    so there it says how often it's placed instead."""
    if ref.ref.kind is RefKind.OBJECT and ref.usage:
        return summarize_uses(set().union(*ref.usage.values()))
    return item_label(ref)


def package_name(ref: LocatedRef) -> str:
    return Path(ref.used.rel).stem if ref.used else ""


def display_name(ref: LocatedRef, name: ItemName | None) -> str:
    """The item's own name when its package has one, else the package it's in."""
    return name.title if name is not None and name.title else package_name(ref)


def usage_text(ref: LocatedRef) -> str:
    """Every use, one Sim per line (for tooltips)."""
    return "\n".join(f"{sim}: {', '.join(sorted(uses))}" for sim, uses in ref.usage_by_name())


def summarize_uses(uses: set[str]) -> str:
    """"Everyday outfit 1" for one or two outfits, "12 outfits" for more, plus other uses."""
    outfits = sorted(u for u in uses if " outfit " in u)
    other = sorted(u for u in uses if " outfit " not in u)
    parts = outfits if len(outfits) <= 2 else [f"{len(outfits)} outfits"]
    return ", ".join(parts + other)


def usage_summary(ref: LocatedRef, with_names: bool = False) -> str:
    """Who uses it and how, short enough for one line. Names are left out for one-Sim reports."""
    if len(ref.usage) == 1 and not with_names:
        return summarize_uses(next(iter(ref.usage.values())))
    return "; ".join(f"{sim}: {summarize_uses(uses)}" for sim, uses in ref.usage_by_name())


_CLOTHING_ICONS = {6: "cat-top", 7: "cat-bottom", 5: "cat-dress", 8: "cat-shoes",
                   36: "cat-shoes", 42: "cat-bottom"}
_GROUP_ICONS = {"Hair": "cat-hair", "Accessories": "cat-accessory", "Makeup": "cat-makeup",
                "Eyes": "cat-eyes", "Skin details": "cat-skin-detail", "Body": "cat-body",
                "Sliders": "cat-slider", "Sculpts": "cat-sculpt", "Skin tone": "cat-skin-tone",
                "Custom tattoos": "cat-skin-detail", "Pet coat": "cat-skin-tone",
                "Objects": "cat-object", "Build": "cat-build"}


def category_icon(ref: LocatedRef) -> str:
    """The category icon shown when an item has no preview image."""
    if ref.ref.group == "Clothing":
        return _CLOTHING_ICONS.get(ref.ref.body_type, "cat-top")
    return _GROUP_ICONS.get(ref.ref.group, "package")


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


# Small building blocks


class IconLabel(QLabel):
    """One of our icons in a theme colour token, redrawn when the theme changes."""

    def __init__(self, name: str, token: str, size: int = 20, parent=None):
        super().__init__(parent)
        self._name, self._token, self._size = name, token, size
        self.setFixedSize(size, size)
        self.refresh()

    def refresh(self) -> None:
        colour = getattr(theme.current(), self._token)
        self.setPixmap(icons.pixmap(self._name, colour, self._size, self.devicePixelRatioF()))


class SimChip(QAbstractButton):
    """A Sim the user can include or leave out: a rounded chip with a tick when included."""

    HEIGHT = 32

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Include {text}")
        self._hover = False

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(SPACE["m"] + 16 + SPACE["s"] + fm.horizontalAdvance(self.text())
                     + SPACE["l"], self.HEIGHT)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = r.height() / 2
        checked = self.isChecked()
        fill = p.selection if checked else (p.surface_hover if self._hover else p.surface)
        border = p.accent if checked else p.border_strong
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(r, radius, radius)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p.focus), 2))
            painter.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        x = SPACE["m"]
        if checked:
            img = icons.image("check", p.accent_text, 16, self.devicePixelRatioF())
            painter.drawImage(QRectF(x, (self.height() - 16) / 2, 16, 16), img)
        else:
            painter.setPen(QPen(QColor(p.border_strong), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(x + 2.5, (self.height() - 11) / 2, 11, 11))
        painter.setPen(QColor(p.text if checked else p.text_secondary))
        painter.setFont(self.font())
        painter.drawText(QRectF(x + 16 + SPACE["s"], 0, self.width(), self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


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
        for card in cards:
            flow.addWidget(card)
        holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        col.addWidget(holder)

    def set_size(self, size: int | None) -> None:
        self.size_label.setText(human_size(size) if size else "")

    def show_matching(self, matching: int) -> None:
        self.setVisible(matching > 0)
        n = len(self.cards)
        self.count_label.setText(str(n) if matching == n else f"{matching} of {n}")


class Notice(QWidget):
    """A calm, one-line notice with an icon, and details that open on request.

    A detail row is (item, info, tooltip) or (item, info, tooltip, (link text, callback));
    `actions` are links next to Show, like Copy list. `expanded` and `on_expand` let the view
    keep the details open when the notice is rebuilt."""

    def __init__(self, icon_name: str, token: str, title: str, body: str,
                 details: list[tuple] | None = None,
                 actions: list[tuple[str, object]] | None = None, expanded: bool = False,
                 on_expand=None, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, SPACE["m"], 0, SPACE["m"])
        outer.setSpacing(SPACE["m"])
        outer.addWidget(IconLabel(icon_name, token), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(SPACE["xs"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["m"])
        head.addWidget(label(title, "noticeTitle"), 1)
        col.addLayout(head)
        col.addWidget(label(body, "secondary", wrap=True))
        outer.addLayout(col, 1)
        if details:
            box = QWidget()
            grid = QGridLayout(box)
            grid.setContentsMargins(0, SPACE["s"], 0, 0)
            grid.setHorizontalSpacing(SPACE["l"])
            grid.setVerticalSpacing(SPACE["xs"])
            grid.setColumnStretch(1, 1)
            for i, (what, info, tip, *action) in enumerate(details):
                a, b = label(what, "detailItem"), label(info, "secondary", wrap=True)
                a.setToolTip(tip)
                b.setToolTip(tip)
                grid.addWidget(a, i, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(b, i, 1)
                if action:
                    text, callback = action[0]
                    link = link_button(text)
                    link.setAccessibleName(f"{text}: {what}, {info}")
                    link.clicked.connect(lambda _c=False, f=callback, w=link: f(w))
                    grid.addWidget(link, i, 2, Qt.AlignmentFlag.AlignTop)
            box.setVisible(expanded)
            col.addWidget(box)
        for text, callback in actions or []:
            link = link_button(text)
            link.setAccessibleName(f"{text}: {title}")
            link.clicked.connect(lambda _c=False, f=callback, w=link: f(w))
            head.addWidget(link, 0, Qt.AlignmentFlag.AlignTop)
        if details:
            toggle = link_button("Hide" if expanded else "Show")
            toggle.setAccessibleName(f"Show details: {title}")

            def flip() -> None:
                box.setVisible(not box.isVisible())
                toggle.setText("Hide" if box.isVisible() else "Show")
                if on_expand is not None:
                    on_expand(box.isVisible())

            toggle.clicked.connect(flip)
            head.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)


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
        if self._retry is None:
            self._retry = link_button("Try again")
            self.layout().addWidget(self._retry)
        try:
            self._retry.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._retry.clicked.connect(on_retry)
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


# The view


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


class HouseholdView(QWidget):
    """Empty, loading and error states, and the report for one household."""

    sims_changed = Signal(set)          # Sim ids to include
    exclusion_changed = Signal(set)     # TrayRefs left out of the export
    copies_chosen = Signal(object)  # {ResourceKey: package Path, or None for the default}
    export_requested = Signal()

    SORTS = (("category", "Category"), ("size", "Size"), ("name", "Name"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._mode = "cards"
        self._sort = "category"
        self._query = ""
        self._names: dict[TrayRef, ItemName] = {}
        self._expanded: set[str] = set()  # notices whose details are open
        self._search: QLineEdit | None = None
        self._list: CcList | None = None
        self._report: HouseholdReport | None = None
        self._plan: Plan | None = None
        self._gameplay: list = []
        self._cards: dict[int, list[CcCard]] = {}
        self._all_cards: list[CcCard] = []
        self._sections: list[Section] = []
        self._thumbs: dict[int, QImage] = {}
        self._thumbs_done = False
        self._theme_timer = QTimer(self, singleShot=True, interval=0)
        self._theme_timer.timeout.connect(self._rebuild_for_theme)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.message_page, self.message_title, self.message_body = self._message_page()
        self.stack.addWidget(self.message_page)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stack.addWidget(self.scroll)
        self.show_message("Nothing selected",
                          "Pick a household, lot or room on the left to see which CC it uses.")

    # States

    def _message_page(self):
        page = QWidget()
        page.setObjectName("content")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(SPACE["xxl"], SPACE["xxl"], SPACE["xxl"], SPACE["xxl"])
        outer.addStretch(2)
        box = QWidget()
        box.setFixedWidth(440)  # a fixed width lets wrapped labels report the right height
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        title = label("", "headline", wrap=True)
        body = label("", "secondary", wrap=True)
        col.addWidget(title)
        col.addWidget(body)
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(box)
        centred.addStretch(1)
        outer.addLayout(centred)
        outer.addStretch(3)
        return page, title, body

    def show_message(self, title: str, body: str) -> None:
        self._report = None
        self._plan = None
        self._gameplay = []
        self.message_title.setText(title)
        self.message_body.setText(body)
        self.message_body.setVisible(bool(body))
        self.stack.setCurrentWidget(self.message_page)

    def show_report(self, report: HouseholdReport) -> None:
        keep_scroll = (self._report is not None
                       and self._report.item.instance == report.item.instance)
        position = self.scroll.verticalScrollBar().value() if keep_scroll else 0
        if not keep_scroll:
            self._thumbs_done = False
            self._query = ""
            self._expanded.clear()
        self._report = report
        self._plan = None
        self._build()
        if self._thumbs_done:
            self.set_thumbnails(report.item.instance, {}, True)
        self.stack.setCurrentWidget(self.scroll)
        self.scroll.verticalScrollBar().setValue(position)

    def set_plan_progress(self, instance: int, done: int, total: int) -> None:
        if self._report is not None and self._report.item.instance == instance:
            self.hero.show_working(done, total)

    def show_plan_error(self, instance: int, reason: str, on_retry) -> None:
        if self._report is not None and self._report.item.instance == instance:
            self.hero.show_error(reason, on_retry)

    def set_plan(self, instance: int, plan: Plan, gameplay: list | None = None) -> None:
        if self._report is None or self._report.item.instance != instance:
            return
        self._plan = plan
        if gameplay is not None:
            self._gameplay = gameplay
        if self._sort == "size":
            self._rebuild()  # the order depends on the sizes just worked out
        else:
            self._apply_plan()

    def plan_outdated(self) -> None:
        """The choices changed; nothing may export the plan on screen until the new one."""
        self._plan = None
        if self._report is not None:
            self.hero.show_working()

    def _owner_sizes(self) -> dict[TrayRef, int]:
        sizes: dict[TrayRef, int] = {}
        if self._plan is not None:
            for res in self._plan.resources.values():
                sizes[res.owner] = sizes.get(res.owner, 0) + res.size
        return sizes

    def _apply_plan(self) -> None:
        plan = self._plan
        if plan is None:
            return
        self.hero.show_plan(plan)
        self._fill_plan_notices()
        sizes = self._owner_sizes()
        for card in self._all_cards:
            card.set_size(sizes.get(card.ref.ref) if card.included else None)
        for section in self._sections:
            section.set_size(sum(sizes.get(c.ref.ref, 0) for c in section.cards if c.included))

    def set_thumbnails(self, instance: int, images: dict[int, QImage],
                       finished: bool = True) -> None:
        """Show loaded thumbnails; once loading has finished, the rest get the placeholder."""
        if self._report is None or self._report.item.instance != instance:
            return
        self._thumbs.update(images)
        self._thumbs_done = self._thumbs_done or finished
        for part, cards in self._cards.items():
            for card in cards:
                if part in self._thumbs:
                    card.thumb.set_image(self._thumbs[part])
                elif self._thumbs_done:
                    card.thumb.set_missing()

    def thumbnails(self) -> set[int]:
        """Items whose thumbnail is already loaded."""
        return set(self._thumbs)

    def set_names(self, instance: int, names: dict[TrayRef, ItemName]) -> None:
        """Item names read from their packages arrive after the report is shown."""
        self._names.update(names)
        if self._report is None or self._report.item.instance != instance:
            return
        if self._mode == "list" or self._sort == "name":
            self._rebuild()
            return
        for card in self._all_cards:
            card.set_name(self._names.get(card.ref.ref))
        self._apply_filter()

    def known_names(self) -> set[TrayRef]:
        return set(self._names)

    def current_item(self) -> TrayItem | None:
        return self._report.item if self._report else None

    def export_state(self):
        """(report, plan, gameplay) when an export can start, else None."""
        if self._report is None or self._plan is None or not self._plan.resources:
            return None
        return self._report, self._plan, self._gameplay

    def refresh_theme(self) -> None:
        """Rebuild what's painted from theme colours (icons, list colours). A theme switch
        sends several change events; they're folded into one rebuild that keeps the scroll
        position."""
        if not self._theme_timer.isActive():
            self._theme_timer.start()

    def _rebuild_for_theme(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        """Build the page again (theme, view, sort or names changed), keeping the scroll
        position, the plan on screen and the search box's focus."""
        if self._report is None:
            return
        position = self.scroll.verticalScrollBar().value()
        searching = self._search is not None and self._search.hasFocus()
        cursor = self._search.cursorPosition() if searching else 0
        self._build()
        self._apply_plan()
        if self._thumbs_done:
            self.set_thumbnails(self._report.item.instance, {}, True)
        self.scroll.verticalScrollBar().setValue(position)
        if searching and self._search is not None:
            self._search.setFocus()
            self._search.setCursorPosition(cursor)

    # Report

    def _build(self) -> None:
        r = self._report
        assert r is not None
        page = QWidget()
        page.setObjectName("content")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        col = QVBoxLayout(page)
        col.setContentsMargins(SPACE["xxl"] + SPACE["s"], SPACE["xl"] + SPACE["s"],
                               SPACE["xxl"] + SPACE["s"], SPACE["xxl"])
        col.setSpacing(0)

        col.addWidget(label(r.item.name or f"Unnamed {r.noun}", "headline", wrap=True))
        col.addSpacing(SPACE["m"])
        sims = r.contents.sims
        names = {s.sim.sim_id: s.sim.name or "Unnamed Sim" for s in sims}
        if r.noun != "household":
            self._chips = []
            line = item_subtitle(r.item, with_price=True)
            col.addWidget(label(f"{r.noun.capitalize()} \u00b7 {line}" if line
                                else r.noun.capitalize(), "secondary"))
        elif len(sims) > 1:
            row = QHBoxLayout()
            row.setSpacing(SPACE["s"])
            self._chips: list[SimChip] = []
            for s in sims:
                chip = SimChip(names[s.sim.sim_id])
                chip.setChecked(s.sim.sim_id in r.sim_ids)
                chip.setProperty("sim_id", s.sim.sim_id)
                chip.toggled.connect(self._chip_toggled)
                self._chips.append(chip)
                row.addWidget(chip)
            row.addStretch(1)
            col.addLayout(row)
        else:
            self._chips = []
            if sims:
                col.addWidget(label(names[sims[0].sim.sim_id], "secondary"))

        col.addSpacing(SPACE["xl"])
        self.hero = Hero(self.export_requested.emit, r.noun)
        self.hero.show_working()
        col.addWidget(self.hero)

        summary = summarize(r.refs)
        col.addSpacing(SPACE["l"])
        self.notices = QVBoxLayout()
        self.notices.setSpacing(0)
        col.addLayout(self.notices)
        self._summary = summary
        self._fill_tray_notices()

        col.addSpacing(SPACE["xl"])
        bar = QHBoxLayout()
        bar.addWidget(label("Custom content", "title"))
        n_out = len(r.excluded & {x.ref for x in summary.cc})
        self.excluded_label = label(f"{n_out} left out" if n_out else "", "secondary")
        bar.addSpacing(SPACE["s"])
        bar.addWidget(self.excluded_label, 0, Qt.AlignmentFlag.AlignBaseline)
        bar.addStretch(1)
        bar.addWidget(self._view_toggle())
        col.addLayout(bar)
        col.addSpacing(SPACE["xs"])
        hint = ("Click an item to leave it out of the export." if self._mode == "cards"
                else "Switch to cards to choose what to leave out.")
        col.addWidget(label(f"{self._source_line(summary)} {hint}", "secondary", wrap=True))
        col.addSpacing(SPACE["l"])

        self._search, self._list = None, None
        if summary.cc:
            tools = QHBoxLayout()
            tools.setSpacing(SPACE["m"])
            search = QLineEdit(self._query)
            search.setPlaceholderText("Search names and packages")
            search.setAccessibleName("Search the CC")
            search.setClearButtonEnabled(True)
            search.setFixedWidth(280)
            search.addAction(icons.icon("search", theme.current().text_tertiary, 16),
                             QLineEdit.ActionPosition.LeadingPosition)
            search.textChanged.connect(self._query_changed)
            self._search = search
            tools.addWidget(search)
            self.match_label = label("", "secondary")
            tools.addWidget(self.match_label)
            tools.addStretch(1)
            tools.addWidget(label("Sort by", "secondary"))
            tools.addWidget(self._sort_toggle())
            col.addLayout(tools)
            col.addSpacing(SPACE["l"])
            self.no_match = label("", "secondary", wrap=True)
            self.no_match.hide()
            col.addWidget(self.no_match)

        groups = self._sorted_groups(summary.cc)
        self._cards, self._all_cards, self._sections = {}, [], []
        if not summary.cc:
            col.addWidget(label(f"This {r.noun} doesn't use any CC from your Mods folder.",
                                "secondary", wrap=True))
        elif self._mode == "cards":
            for title, refs in groups:
                cards = [CcCard(x, x.ref not in r.excluded, self._names.get(x.ref))
                         for x in refs]
                for card in cards:
                    card.toggled.connect(self._card_toggled)
                    self._all_cards.append(card)
                    if card.ref.ref.kind in THUMB_TYPES:
                        self._cards.setdefault(card.ref.ref.instance, []).append(card)
                        if card.ref.ref.instance in self._thumbs:
                            card.thumb.set_image(self._thumbs[card.ref.ref.instance])
                section = Section(title, cards)
                self._sections.append(section)
                col.addWidget(section)
        else:
            self._list = CcList(groups, len(r.sim_ids) > 1, r.excluded, self._names)
            col.addWidget(self._list)
        col.addStretch(1)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)
        self._apply_filter()

    # Search and sort

    def _title(self, ref: LocatedRef) -> str:
        return display_name(ref, self._names.get(ref.ref))

    def _sorted_groups(self, refs: list[LocatedRef]) -> list[tuple[str, list[LocatedRef]]]:
        if self._sort == "size":
            sizes = self._owner_sizes()
            ordered = sorted(refs, key=lambda x: (-sizes.get(x.ref, 0),
                                                  self._title(x).casefold()))
            return [("Largest first", ordered)]
        if self._sort == "name":
            return [("A to Z", sorted(refs, key=lambda x: self._title(x).casefold()))]
        return self._groups(refs, self._title)

    def _matches(self, ref: LocatedRef) -> bool:
        if not self._query:
            return True
        name = self._names.get(ref.ref)
        haystack = " ".join([self._title(ref), name.creator if name else "",
                             name.raw if name else "", ref.used.rel if ref.used else "",
                             item_label(ref), card_caption(ref)]).casefold()
        return all(word in haystack for word in self._query.casefold().split())

    def _query_changed(self, text: str) -> None:
        self._query = text.strip()
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._search is None:
            return
        total = len(self._summary.cc)
        if self._list is not None:
            shown = self._list.show_matching(self._matches)
        else:
            shown = 0
            for section in self._sections:
                matching = 0
                for card in section.cards:
                    ok = self._matches(card.ref)
                    card.setVisible(ok)
                    matching += ok
                section.show_matching(matching)
                shown += matching
        self.match_label.setText(f"{shown} of {total}" if self._query else "")
        self.no_match.setText(f"Nothing matches \u201c{self._query}\u201d. Search looks at "
                              "names, creators and packages.")
        self.no_match.setVisible(bool(self._query) and shown == 0)

    def _sort_toggle(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        for i, (key, text) in enumerate(self.SORTS):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self._sort == key)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else
                          "last" if i == len(self.SORTS) - 1 else "middle")
            b.setAccessibleName(f"Sort by {text.lower()}")
            b.clicked.connect(lambda _c=False, k=key: self._set_sort(k))
            group.addButton(b)
            row.addWidget(b)
        return box

    def _set_sort(self, key: str) -> None:
        if key != self._sort and self._report is not None:
            self._sort = key
            self._rebuild()

    @staticmethod
    def _source_line(s: Summary) -> str:
        parts = [f"{plural(len(s.cc), 'item')} from {plural(len(s.packages), 'package')} in "
                 "your Mods folder."]
        if s.game:
            packs = s.packs
            where = "the base game" if packs == ["base"] else ", ".join(
                "base game" if p == "base" else p for p in packs)
            parts.append(f"{plural(len(s.game), 'item')} come from the game ({where}) and "
                         "aren't exported.")
        return " ".join(parts)

    @staticmethod
    def _groups(refs: list[LocatedRef], title=package_name) -> list[tuple[str, list[LocatedRef]]]:
        by_group: dict[str, list[LocatedRef]] = {}
        for ref in refs:
            by_group.setdefault(ref.ref.group, []).append(ref)
        order = {g: i for i, g in enumerate(GROUP_ORDER)}
        return [(g, sorted(rs, key=lambda x: (item_label(x), title(x).casefold())))
                for g, rs in sorted(by_group.items(), key=lambda kv: order.get(kv[0], 99))]

    # Notices

    def _notice(self, key: str, *args, **kwargs) -> Notice:
        """A notice whose details stay open across rebuilds once the user opened them."""
        return Notice(*args, expanded=key in self._expanded,
                      on_expand=lambda shown: (self._expanded.add(key) if shown
                                               else self._expanded.discard(key)), **kwargs)

    def _copy_to_clipboard(self, lines: list[str], button) -> None:
        QGuiApplication.clipboard().setText("\n".join(lines) + "\n")
        button.setText("Copied")
        QTimer.singleShot(2000, lambda: _set_text_if_alive(button, "Copy list"))

    def _choose_copy(self, group: list[PlannedResource], button) -> None:
        """Pick the package an item's differing files come from: every package that holds a
        copy of any of them, the game's own choice first."""
        packages: dict[Path, str] = {}
        for res in group:
            for hit in reversed(res.copies):
                packages.setdefault(hit.path, hit.rel)
        default = group[0].copies[-1].path
        current = {res.copy.path for res in group}
        menu = QMenu(self)
        for path, rel in packages.items():
            text = rel + ("  (the game uses this one)" if path == default else "")
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setChecked(current == {path})
            action.triggered.connect(lambda _c=False, p=path: self._pick_package(group, p))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()) if button is not None
                  else QCursor.pos())
        menu.deleteLater()

    def _pick_package(self, group: list[PlannedResource], path: Path) -> None:
        """Take each file from `path` where it has a copy; the rest keep the game's choice."""
        picks = {}
        for res in group:
            has = any(h.path == path for h in res.copies)
            picks[res.key] = path if has and res.copies[-1].path != path else None
        self.copies_chosen.emit(picks)

    def _add_notice(self, notice: Notice, tag: str) -> None:
        notice.setProperty("phase", tag)
        if self.notices.count():
            line = QFrame()
            line.setObjectName("divider")
            line.setFixedHeight(1)
            line.setProperty("phase", tag)
            self.notices.addWidget(line)
        self.notices.addWidget(notice)

    def _clear_notices(self, tag: str) -> None:
        for i in reversed(range(self.notices.count())):
            w = self.notices.itemAt(i).widget()
            if w is not None and w.property("phase") == tag:
                self.notices.takeAt(i)
                w.deleteLater()
        # a leading divider left behind by removing the first notice
        first = self.notices.itemAt(0).widget() if self.notices.count() else None
        if first is not None and first.objectName() == "divider":
            self.notices.takeAt(0)
            first.deleteLater()

    def _fill_tray_notices(self) -> None:
        r, s = self._report, self._summary
        with_names = len(r.sim_ids) > 1
        if s.missing:
            n = len(s.missing)
            details = [(item_label(x), usage_summary(x, with_names),
                        f"Resource instance {x.ref.instance:016X}\n\n{usage_text(x)}")
                       for x in s.missing]
            if r.noun == "household":
                looks = "Sims look" if with_names else "Sim looks"
                extra = ""
            else:
                looks = f"{r.noun} looks"
                extra = (" Downloads from the gallery often use CC that doesn't come with "
                         "them.")
            lines = [f"{item_label(x)}\t{x.ref.instance:016X}\t{usage_summary(x, True)}"
                     for x in s.missing]
            self._add_notice(self._notice(
                "missing",
                "alert", "danger_text",
                f"{plural(n, 'item')} {'isn' if n == 1 else 'aren'}'t installed",
                f"They're not in your Mods folder or in the game, so the {looks} different "
                f"without them, here and in the export.{extra}", details,
                actions=[("Copy list", lambda b: self._copy_to_clipboard(lines, b))]), "tray")
        if r.overrides:
            n = len(r.overrides)
            details = [(item_label(x), package_name(x), x.used.rel if x.used else "")
                       for x in s.cc if x.ref in r.overrides]
            self._add_notice(Notice(
                "info", "text_secondary",
                f"Your mods change {plural(n, 'EA item')} this {r.noun} uses",
                "Mostly to unlock or retune them, which rarely changes how it looks. Exported, "
                f"{'it' if n == 1 else 'they'}'d change the item everywhere in the recipient's "
                f"game, so {'it starts' if n == 1 else 'they start'} out of the export. To "
                "include one, click its card (marked Not exported).", details), "tray")
        for note in (r.contents.sims[0].notes if r.contents.sims else []):
            self._add_notice(Notice("info", "text_secondary", "Partly read", note), "tray")

    def _fill_plan_notices(self) -> None:
        self._clear_notices("plan")
        plan = self._plan
        if plan is None:
            return
        if plan.missing:
            n = len(plan.missing)
            details, lines = [], []
            for key, needers in sorted(plan.missing.items(), key=lambda kv: str(kv[0])):
                parents = ", ".join(sorted({restypes.name(p.type) for p in needers}))
                details.append((restypes.name(key.type), f"Needed by: {parents}", str(key)))
                lines.append(f"{restypes.name(key.type)}\t{key}\tneeded by "
                             + ", ".join(str(p) for p in sorted(needers, key=str)))
            self._add_notice(self._notice(
                "missing-files",
                "alert", "danger_text", f"{plural(n, 'file')} that CC needs "
                f"{'is' if n == 1 else 'are'} missing",
                "Some CC refers to meshes or textures that aren't in your Mods folder, often a "
                "mesh that has to be downloaded separately. It already looks wrong in your game; "
                "the export can't fix that.", details[:200],
                actions=[("Copy list", lambda b: self._copy_to_clipboard(lines, b))]), "plan")
        if plan.conflicts:
            n = len(plan.conflicts)
            by_owner: dict[TrayRef, list[PlannedResource]] = {}
            for c in plan.conflicts:
                by_owner.setdefault(c.owner, []).append(c)
            located = {x.ref: x for x in self._report.refs}
            details = []
            for owner, group in by_owner.items():
                ref = located.get(owner)
                what = (display_name(ref, self._names.get(owner)) if ref is not None
                        else ref_label(owner))
                used = sorted({Path(c.copy.rel).name for c in group})
                files = plural(len(group), "file")
                details.append((
                    what, f"uses {', '.join(used)}"
                    + (" (your choice)" if any(c.chosen for c in group) else "")
                    + f" \u00b7 {files} with different copies",
                    "\n".join(f"{restypes.name(c.key.type)} {c.key}: "
                              + ", ".join(h.rel for h in c.copies) for c in group),
                    ("Choose", lambda b, g=group: self._choose_copy(g, b))))
            chosen = sum(1 for g in by_owner.values() if any(c.chosen for c in g))
            self._add_notice(self._notice(
                "conflicts",
                "copies", "warning_text",
                f"{plural(n, 'file')} {'has' if n == 1 else 'have'} different copies in Mods",
                "TrayTrim takes the copy the game uses: the one that loads last, in "
                "alphabetical order of its path in Mods. Choose another copy if you know it's "
                "the right one." + (f" You chose {chosen}." if chosen else ""), details),
                "plan")
        if plan.default_replacements:
            n = len(plan.default_replacements)
            details = [(restypes.name(d.key.type), Path(d.copy.rel).name, str(d.key))
                       for d in plan.default_replacements]
            if self._report.noun == "household":
                several = len(self._report.sim_ids) > 1
                who, wear, look = (("The Sims", "wear", "look") if several
                                   else ("The Sim", "wears", "looks"))
                body = (f"{who} {wear} these EA items in a version changed by your Mods "
                        f"(default replacements). They're exported so the {who.split()[1]} "
                        f"{look} the same, which changes the item for every Sim in the "
                        "recipient's game. Leave them out if that's not wanted.")
            else:
                body = ("You included EA items in a version changed by your Mods. They're "
                        "exported as they are, which changes them everywhere in the "
                        "recipient's game.")
            self._add_notice(Notice(
                "info", "text_secondary",
                f"{plural(n, 'item')} {'replaces' if n == 1 else 'replace'} game content",
                body, details), "plan")
        if self._gameplay:
            n = len(self._gameplay)
            r = self._report
            if r.noun == "household":
                users = "the Sims"
                who = "The Sims use" if len(r.sim_ids) > 1 else "The Sim uses"
                what = "traits, moods or other gameplay content"
            else:
                users, who, what = f"the {r.noun}", f"The {r.noun} uses", "lot traits"
            details = [(m.rel, plural(m.ids, "id") + f" used by {users}", m.rel)
                       for m in self._gameplay]
            self._add_notice(Notice(
                "info", "text_secondary",
                f"{plural(n, 'gameplay mod')} {'is' if n == 1 else 'are'} also needed",
                f"{who} {what} from these mods. They can't be trimmed, so they're not "
                "exported; the README lists them so the recipient can install them.",
                details), "plan")
        if plan.unreadable:
            n = len(plan.unreadable)
            details = [(restypes.name(k.type), Path(p).name, reason)
                       for k, p, reason in plan.unreadable]
            self._add_notice(Notice(
                "alert", "danger_text", f"{plural(n, 'file')} couldn't be read",
                "Their own references couldn't be followed, so something they need may be "
                "missing from the export.", details), "plan")

    # Interaction

    def _view_toggle(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        for i, (mode, text) in enumerate((("cards", "Cards"), ("list", "List"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self._mode == mode)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else "last")
            b.setAccessibleName(f"Show CC as {text.lower()}")
            b.clicked.connect(lambda _c=False, m=mode: self._set_mode(m))
            group.addButton(b)
            row.addWidget(b)
        return box

    def _set_mode(self, mode: str) -> None:
        if mode != self._mode and self._report is not None:
            self._mode = mode
            self._rebuild()

    def _chip_toggled(self) -> None:
        chosen = {c.property("sim_id") for c in self._chips if c.isChecked()}
        if not chosen:  # keep at least one Sim; an empty export makes no sense
            sender = self.sender()
            if isinstance(sender, SimChip):
                sender.blockSignals(True)
                sender.setChecked(True)
                sender.blockSignals(False)
            return
        self.sims_changed.emit(chosen)

    def _card_toggled(self, ref: TrayRef, included: bool) -> None:
        r = self._report
        if r is None:
            return
        if included:
            r.excluded.discard(ref)
        else:
            r.excluded.add(ref)
        n_out = len(r.excluded & {x.ref for x in self._summary.cc})
        self.excluded_label.setText(f"{n_out} left out" if n_out else "")
        self._plan = None  # the shown plan no longer matches; nothing may export it
        self.hero.show_working()
        self.exclusion_changed.emit(set(r.excluded))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(800, 600)


def _set_text_if_alive(button, text: str) -> None:
    try:
        button.setText(text)
    except RuntimeError:  # the notice was rebuilt meanwhile
        pass
