"""The right side of the window: what the selected household uses and what an export would be."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QFrame, QGraphicsOpacityEffect,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
                               QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from traytrim import restypes, tray
from traytrim.gui import icons, theme
from traytrim.gui.flow import FlowLayout
from traytrim.gui.theme import RADIUS, SPACE, TYPE
from traytrim.gui.widgets import ClampedText, label, link_button, set_role
from traytrim.plan import Plan
from traytrim.resolve import LocatedRef, Summary, summarize
from traytrim.tray import HouseholdContents, RefKind, TrayItem, TrayRef
from traytrim.units import human_size, percent_smaller

CARD_WIDTH = 140
THUMB = 124

# Section order, as CAS shows things.
GROUP_ORDER = ["Hair", "Clothing", "Accessories", "Makeup", "Eyes", "Skin details", "Body",
               "Other CAS parts", "Skin tone", "Sculpts", "Sliders", "Custom tattoos",
               "Pet coat"]
KIND_LABEL = {RefKind.SCULPT: "Sculpt", RefKind.SIM_MODIFIER: "Slider",
              RefKind.SKIN_TONE: "Skin tone", RefKind.PELT_LAYER: "Coat layer",
              RefKind.CUSTOM_TATTOO: "Custom tattoo"}


@dataclass
class HouseholdReport:
    item: TrayItem
    contents: HouseholdContents
    refs: list[LocatedRef]
    sim_ids: set[int]
    excluded: set[TrayRef] = field(default_factory=set)


def item_label(ref: LocatedRef) -> str:
    if ref.ref.kind is RefKind.CAS_PART:
        return tray.BODY_TYPES.get(ref.ref.body_type, "CAS part")
    return KIND_LABEL.get(ref.ref.kind, ref.ref.kind.value)


def package_name(ref: LocatedRef) -> str:
    return Path(ref.used.rel).stem if ref.used else ""


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
                "Custom tattoos": "cat-skin-detail", "Pet coat": "cat-skin-tone"}


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

    def __init__(self, ref: LocatedRef, included: bool, parent=None):
        super().__init__(parent)
        self.ref = ref
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
        # CAS parts wait for their thumbnail; other kinds never have one.
        self.thumb = Thumbnail(THUMB, category_icon(ref), ref.ref.kind is RefKind.CAS_PART)
        col.addWidget(self.thumb)
        col.addSpacing(SPACE["xs"])
        col.addWidget(label(item_label(ref), "caption"))
        col.addWidget(ClampedText(package_name(ref), 2))
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

    def _set_tooltip(self) -> None:
        r = self.ref
        lines = [r.used.rel if r.used else ""]
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
        self.setAccessibleName(f"{item_label(self.ref)}, {package_name(self.ref)}. "
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
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["m"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["s"])
        head.addWidget(label(title, "title"))
        head.addWidget(label(str(len(cards)), "secondary"), 0, Qt.AlignmentFlag.AlignBaseline)
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


class Notice(QWidget):
    """A calm, one-line notice with an icon, and details that open on request."""

    def __init__(self, icon_name: str, token: str, title: str, body: str,
                 details: list[tuple[str, str, str]] | None = None, parent=None):
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
            for i, (what, info, tip) in enumerate(details):
                a, b = label(what, "detailItem"), label(info, "secondary", wrap=True)
                a.setToolTip(tip)
                b.setToolTip(tip)
                grid.addWidget(a, i, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(b, i, 1)
            box.hide()
            col.addWidget(box)
            toggle = link_button("Show")
            toggle.setAccessibleName(f"Show details: {title}")

            def flip() -> None:
                box.setVisible(not box.isVisible())
                toggle.setText("Hide" if box.isVisible() else "Show")

            toggle.clicked.connect(flip)
            head.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)


class Hero(QWidget):
    """The one number that matters: how big the export is, next to whole packages, and the
    one action: Export."""

    def __init__(self, on_export, parent=None):
        super().__init__(parent)
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
            self.line.setText(f"Following meshes, textures and sliders: {done:,} of "
                              f"{total:,} files so far.")
        else:
            self.bar.setRange(0, 0)
            self.line.setText("Following meshes, textures and sliders.")
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
            self.line.setText("Every CC item is left out, or the household only uses game "
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
    def __init__(self, groups: list[tuple[str, list[LocatedRef]]], with_names: bool,
                 excluded: set[TrayRef], parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels(["Item", "Package", "Used in", "Notes"])
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.setColumnWidth(2, 240 if with_names else 180)
        bold = theme.ui_font("body", QFont.Weight.DemiBold)
        dim = QColor(theme.current().text_tertiary)
        for title, refs in groups:
            top = QTreeWidgetItem([f"{title} ({len(refs)})"])
            top.setFont(0, bold)
            self.addTopLevelItem(top)
            for r in refs:
                notes = []
                if r.ref in excluded:
                    notes.append("Not exported")
                if len(r.mods) > 1:
                    notes.append(f"{len(r.mods)} copies in Mods")
                if r.approximate:
                    notes.append("Approximate match")
                child = QTreeWidgetItem([item_label(r), r.used.rel if r.used else "",
                                         usage_summary(r, with_names), ", ".join(notes)])
                child.setToolTip(1, r.used.rel if r.used else "")
                child.setToolTip(2, usage_text(r))
                if r.ref in excluded:
                    for c in range(4):
                        child.setForeground(c, dim)
                top.addChild(child)
            top.setExpanded(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.itemExpanded.connect(self._fit_height)
        self.itemCollapsed.connect(self._fit_height)
        self._fit_height()

    def _fit_height(self, *_args) -> None:
        """Tall enough for every visible row, so the page scrolls instead of a box inside it."""
        rows = 0
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            rows += 1 + (top.childCount() if top.isExpanded() else 0)
        row_height = self.sizeHintForRow(0) if rows else 0
        self.setFixedHeight(self.header().sizeHint().height() + rows * row_height + 4)


class HouseholdView(QWidget):
    """Empty, loading and error states, and the report for one household."""

    sims_changed = Signal(set)          # Sim ids to include
    exclusion_changed = Signal(set)     # TrayRefs left out of the export
    export_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._mode = "cards"
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
        self.show_message("No household selected",
                          "Pick a household on the left to see which CC it uses.")

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
        self.hero.show_plan(plan)
        self._fill_plan_notices()
        sizes: dict[TrayRef, int] = {}
        for res in plan.resources.values():
            sizes[res.owner] = sizes.get(res.owner, 0) + res.size
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
        """CAS parts whose thumbnail is already loaded."""
        return set(self._thumbs)

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
        if self._report is None:
            return
        position = self.scroll.verticalScrollBar().value()
        plan = self._plan
        self._build()
        if plan is not None:
            self.set_plan(self._report.item.instance, plan)
        if self._thumbs_done:
            self.set_thumbnails(self._report.item.instance, {}, True)
        self.scroll.verticalScrollBar().setValue(position)

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

        col.addWidget(label(r.item.name or "Unnamed household", "headline", wrap=True))
        col.addSpacing(SPACE["m"])
        sims = r.contents.sims
        names = {s.sim.sim_id: s.sim.name or "Unnamed Sim" for s in sims}
        if len(sims) > 1:
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
        self.hero = Hero(self.export_requested.emit)
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

        groups = self._groups(summary.cc)
        self._cards, self._all_cards, self._sections = {}, [], []
        if not summary.cc:
            col.addWidget(label("This household doesn't use any CC from your Mods folder.",
                                "secondary", wrap=True))
        elif self._mode == "cards":
            for title, refs in groups:
                cards = [CcCard(x, x.ref not in r.excluded) for x in refs]
                for card in cards:
                    card.toggled.connect(self._card_toggled)
                    self._all_cards.append(card)
                    if card.ref.ref.kind is RefKind.CAS_PART:
                        self._cards.setdefault(card.ref.ref.instance, []).append(card)
                        if card.ref.ref.instance in self._thumbs:
                            card.thumb.set_image(self._thumbs[card.ref.ref.instance])
                section = Section(title, cards)
                self._sections.append(section)
                col.addWidget(section)
                col.addSpacing(SPACE["xl"])
        else:
            col.addWidget(CcList(groups, len(r.sim_ids) > 1, r.excluded))
        col.addStretch(1)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)

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
    def _groups(refs: list[LocatedRef]) -> list[tuple[str, list[LocatedRef]]]:
        by_group: dict[str, list[LocatedRef]] = {}
        for ref in refs:
            by_group.setdefault(ref.ref.group, []).append(ref)
        order = {g: i for i, g in enumerate(GROUP_ORDER)}
        return [(g, sorted(rs, key=lambda x: (item_label(x), package_name(x).casefold())))
                for g, rs in sorted(by_group.items(), key=lambda kv: order.get(kv[0], 99))]

    # Notices

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
            sims = "Sims look" if with_names else "Sim looks"
            self._add_notice(Notice(
                "alert", "danger_text",
                f"{plural(n, 'item')} {'isn' if n == 1 else 'aren'}'t installed",
                f"They're not in your Mods folder or in the game, so the {sims} different "
                "without them, here and in the export.", details), "tray")

    def _fill_plan_notices(self) -> None:
        self._clear_notices("plan")
        plan = self._plan
        if plan is None:
            return
        if plan.missing:
            n = len(plan.missing)
            details = []
            for key, needers in sorted(plan.missing.items(), key=lambda kv: str(kv[0])):
                parents = ", ".join(sorted({restypes.name(p.type) for p in needers}))
                details.append((restypes.name(key.type), f"Needed by: {parents}", str(key)))
            self._add_notice(Notice(
                "alert", "danger_text", f"{plural(n, 'file')} that CC needs "
                f"{'is' if n == 1 else 'are'} missing",
                "Some CC refers to meshes or textures that aren't in your Mods folder, often a "
                "mesh that has to be downloaded separately. It already looks wrong in your game; "
                "the export can't fix that.", details[:200]), "plan")
        if plan.conflicts:
            n = len(plan.conflicts)
            details = [(restypes.name(c.key.type),
                        f"uses {Path(c.copy.rel).name} ({len(c.copies)} different copies)",
                        "\n".join(h.rel for h in c.copies)) for c in plan.conflicts]
            self._add_notice(Notice(
                "copies", "warning_text",
                f"{plural(n, 'file')} {'has' if n == 1 else 'have'} different copies in Mods",
                "TrayTrim takes the copy the game uses: the one that loads last, in "
                "alphabetical order of its path in Mods.", details), "plan")
        if plan.default_replacements:
            n = len(plan.default_replacements)
            details = [(restypes.name(d.key.type), Path(d.copy.rel).name, str(d.key))
                       for d in plan.default_replacements]
            several = len(self._report.sim_ids) > 1
            who, wear, look = (("The Sims", "wear", "look") if several
                               else ("The Sim", "wears", "looks"))
            self._add_notice(Notice(
                "info", "text_secondary",
                f"{plural(n, 'item')} {'replaces' if n == 1 else 'replace'} game content",
                f"{who} {wear} these EA items in a version changed by your Mods (default "
                f"replacements). They're exported so the {who.split()[1]} {look} the same, "
                "which "
                "changes the item for every Sim in the recipient's game. Leave them out if "
                "that's not wanted.", details), "plan")
        if self._gameplay:
            n = len(self._gameplay)
            details = [(m.rel, plural(m.ids, "id") + " used by the Sims", m.rel)
                       for m in self._gameplay]
            self._add_notice(Notice(
                "info", "text_secondary",
                f"{plural(n, 'gameplay mod')} {'is' if n == 1 else 'are'} also needed",
                f"{'The Sims use' if len(self._report.sim_ids) > 1 else 'The Sim uses'} "
                "traits, moods or other gameplay content from these mods. They can't be "
                "trimmed, so they're not exported; the README lists them so the recipient can "
                "install them.", details), "plan")
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
            plan = self._plan
            self._build()
            if plan is not None:
                self.set_plan(self._report.item.instance, plan)

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
