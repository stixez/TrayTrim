"""CC items as cards or as a list, searchable, filterable and sortable: the CC of a household,
lot or room, of a Mods package, or of a received zip.

Cards aren't widgets: each section is one QListView whose delegate paints the cards, and the
list is one QTreeView. Hundreds of cards built as widgets took seconds (every one of them a
dozen styled widgets), and every sort or search built them again; a view only paints what's on
screen, so sorting, filtering and switching views are instant, and nothing is paged.

Modes: TOGGLE (a household: a click leaves an item out or puts it back), EXPORT (a Mods
package: a click exports the item on its own), STATIC (a received zip: only shown). Every mode
has the item's menu on a right click, except STATIC.
"""

from __future__ import annotations

import functools
import math
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import ClassVar

from PySide6.QtCore import (QAbstractListModel, QAbstractTableModel, QModelIndex, QPoint,
                            QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QFrame, QHBoxLayout,
                               QHeaderView, QLineEdit, QListView, QPushButton, QScrollArea,
                               QSizePolicy,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTreeView,
                               QVBoxLayout, QWidget)

from traytrim.gui import icons, theme
from traytrim.gui.describe import (GROUP_ORDER, THUMB_TYPES, card_caption, category_icon,
                                   display_name, group_refs, item_label, package_name, plural,
                                   usage_summary, usage_text)
from traytrim.gui.theme import RADIUS, SPACE
from traytrim.gui.widgets import MenuButton, label, link_button, wrap_and_elide
from traytrim.names import ItemName
from traytrim.resolve import LocatedRef
from traytrim.tray import TrayRef
from traytrim.units import human_size

TOGGLE, EXPORT, STATIC = "toggle", "export", "static"
CARD_WIDTH = 140
THUMB = 124  # the picture in a card; thumbnails are loaded at twice this for sharp displays
GAP = SPACE["m"]
LIST_THUMB = 36
# Pictures a browser keeps in memory: ~600 x 248 x 248 px x 4 bytes, about 150 MB. Past that the
# least recently painted go, and are loaded again if they're shown again.
MAX_THUMBNAILS = 600
RefRole = Qt.ItemDataRole.UserRole + 1

BIG = 1000  # items past which typing in the search waits for a pause, and grids lay out in batches
SORTS = [("category", "Category"), ("name", "Name, A to Z"), ("name_desc", "Name, Z to A"),
         ("size", "Largest first"), ("size_asc", "Smallest first"), ("package", "Package")]
SIZE_SORTS = {"size", "size_asc"}


class CcState:
    """What every card and row of one browser shows, shared by its views: names, thumbnails,
    sizes and what's left out. `thumbs` may be shared between browsers (a cache)."""

    def __init__(self, mode: str, with_names: bool, thumbs: OrderedDict | None = None,
                 tried: set[int] | None = None, max_thumbs: int = 0,
                 asked: set[int] | None = None):
        self.mode = mode
        self.with_names = with_names        # a household of several Sims: who uses what
        self.names: dict[TrayRef, ItemName] = {}
        self.thumbs: OrderedDict[int, QImage] = thumbs if thumbs is not None else OrderedDict()
        self.tried: set[int] = tried if tried is not None else set()  # answered, image or not
        # Asked and not answered yet; shared like `tried`, so a rebuild doesn't ask again.
        self.asked: set[int] = asked if asked is not None else set()
        self.all_tried = False              # every thumbnail there'll be is in (no more asks)
        self.max_thumbs = max_thumbs        # 0: keep every one
        self.sizes: dict[TrayRef, int] | None = None  # bytes each item adds to the export
        self.excluded: set[TrayRef] = set()
        # Cards name the package each item comes from (off where the page is one package).
        self.show_source = True
        self.on_wanted: Callable[[list[LocatedRef]], None] | None = None
        self._wish: list[LocatedRef] = []
        self._timer = QTimer(singleShot=True, interval=30)
        self._timer.timeout.connect(self._flush)

    def title(self, ref: LocatedRef) -> str:
        return display_name(ref, self.names.get(ref.ref))

    def subtitle(self, ref: LocatedRef, title: str | None = None) -> str:
        """The line under a card's title. For an item named after its package, which swatch
        it is ("Solid Magenta"). Otherwise the package it comes from, as its file is named in
        Mods (its folder is in the tooltip): a part's own name is often the EA item it was
        cloned from ("Dress Maxi Lemon" for a corset dress), and the package is the name
        players recognise. Empty when the title already says it."""
        name = self.names.get(ref.ref)
        if name is not None and name.package:
            return name.swatch
        package = package_name(ref) if self.show_source else ""
        stem, shown = _plain(package), _plain(self.title(ref) if title is None else title)
        if not stem or stem in shown or shown in stem:
            return ""
        return package

    def included(self, ref: LocatedRef) -> bool:
        return ref.ref not in self.excluded

    def size(self, ref: LocatedRef) -> int | None:
        if self.sizes is None or not self.included(ref):
            return None
        return self.sizes.get(ref.ref)

    def thumbnail(self, ref: LocatedRef) -> QImage | None:
        """The item's picture, or None; asks for it (once) the first time it's painted."""
        inst = ref.ref.instance
        image = self.thumbs.get(inst)
        if image is not None:
            if self.max_thumbs:
                self.thumbs.move_to_end(inst)  # on screen: the last to be forgotten
            return image
        if (ref.ref.kind in THUMB_TYPES and not self.all_tried and inst not in self.tried
                and inst not in self.asked and self.on_wanted is not None):
            self.asked.add(inst)
            self._wish.append(ref)
            self._timer.start()
        return None

    def no_picture(self, ref: LocatedRef) -> bool:
        """It has no picture to wait for: its kind has none, or it was looked for."""
        inst = ref.ref.instance
        return inst not in self.thumbs and (ref.ref.kind not in THUMB_TYPES or self.all_tried
                                            or inst in self.tried)

    def add_thumbnails(self, images: dict[int, QImage], tried: set[int]) -> None:
        self.thumbs.update(images)
        self.tried |= tried | set(images)
        self.asked -= tried | set(images)
        while self.max_thumbs and len(self.thumbs) > self.max_thumbs:
            old, _image = self.thumbs.popitem(last=False)  # asked again if it comes back
            self.tried.discard(old)
            self.asked.discard(old)

    def _flush(self) -> None:
        wish, self._wish = self._wish, []
        if wish and self.on_wanted is not None:
            self.on_wanted(wish)

    def tooltip(self, ref: LocatedRef) -> str:
        lines = []
        name = self.names.get(ref.ref)
        if name is not None:
            lines.append(self.title(ref) + (f" by {name.creator}" if name.creator else ""))
            if name.raw and name.raw != name.title:
                lines.append(name.raw)  # its name inside the package
            lines.append("")
        lines.append(ref.used.rel if ref.used else "")
        if len(ref.mods) > 1:
            lines += ["", "Also in:"] + [h.rel for h in ref.mods[:-1]]
            lines += ["", "The game uses the copy that loads last (alphabetical order in Mods)."]
        if ref.approximate:
            lines += ["", "Approximate match: found by its id only, because the type of this "
                          "kind of item isn't verified yet."]
        if ref.usage:
            lines += ["", usage_text(ref)]
        if self.mode == TOGGLE:
            lines += ["", "Click to leave it out of the export." if self.included(ref)
                      else "Click to put it back in the export.",
                      "Right-click to export it on its own."]
        elif self.mode == EXPORT:
            lines += ["", "Click to export it on its own, with everything it needs."]
        return "\n".join(lines).strip()

    def accessible(self, ref: LocatedRef) -> str:
        if self.mode == TOGGLE:
            verb = "Included in the export." if self.included(ref) else "Left out of the export."
        else:
            verb = "Press Enter to export it on its own." if self.mode == EXPORT else ""
        approx = " Approximate match." if ref.approximate else ""
        sub = self.subtitle(ref)
        sub = f", {sub}" if sub else ""
        return f"{item_label(ref)}, {self.title(ref)}{sub}. {verb}{approx}".strip()


def _plain(text: str) -> str:
    """Letters and digits only, for telling whether two names say the same."""
    return re.sub(r"[\W_]+", "", text.casefold())


# Cards


@functools.cache
def _card_metrics() -> tuple[QFontMetrics, QFontMetrics, int]:
    """Caption and body metrics, and a card's height: picture, caption, two lines of title,
    the subtitle, the size line and room for "Not exported"."""
    caption = QFontMetrics(theme.ui_font("caption"))
    body = QFontMetrics(theme.ui_font("body"))
    height = (SPACE["s"] + THUMB + SPACE["s"] + caption.height() + SPACE["xs"]
              + _title_block(caption, body) + 18 + SPACE["xs"] + 18 + SPACE["s"])
    return caption, body, height


def _title_block(caption: QFontMetrics, body: QFontMetrics) -> int:
    """The title's two lines and the subtitle under them, with the gap after."""
    return 2 * body.lineSpacing() + caption.height() + SPACE["xs"]


class CardDelegate(QStyledItemDelegate):
    def __init__(self, state: CcState, parent=None):
        super().__init__(parent)
        self.state = state

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        """As wide as the grid's cells allow (they share out the row's width), less the gap."""
        view = option.widget
        cell = view.gridSize().width() if isinstance(view, QListView) else 0
        return QSize(max(CARD_WIDTH, cell - GAP), _card_metrics()[2])

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.current()
        s = self.state
        ref: LocatedRef = index.data(RefRole)
        caption_m, body_m, height = _card_metrics()
        card = QRectF(option.rect.left(), option.rect.top(),
                      max(CARD_WIDTH, option.rect.width()), height)
        included = s.included(ref) or s.mode != TOGGLE
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver) and s.mode != STATIC
        view = option.widget
        focused = (bool(option.state & QStyle.StateFlag.State_HasFocus) and view is not None
                   and view.hasFocus())
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        # The frame: dashed on a grey ground when left out, darker on hover, a ring on focus.
        pen = QPen(QColor(p.focus if focused else p.border_strong if hovered else p.divider))
        pen.setWidthF(2 if focused else 1)
        if not included:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(p.window if included else p.sidebar))
        inset = 1 if focused else 0.5
        painter.drawRoundedRect(card.adjusted(inset, inset, -inset, -inset), RADIUS["card"],
                                RADIUS["card"])

        x = card.left() + SPACE["s"]
        width = card.width() - 2 * SPACE["s"]
        if not included:  # only the picture fades: the text keeps its contrast
            painter.setOpacity(0.4)
        thumb = QRectF(x, card.top() + SPACE["s"], width, THUMB)  # the picture centred in it
        clip = QPainterPath()
        clip.addRoundedRect(thumb, RADIUS["control"], RADIUS["control"])
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(thumb, QColor(p.sidebar))
        image = s.thumbnail(ref)
        dpr = painter.device().devicePixelRatioF()
        if image is not None:
            scale = min(thumb.width() / image.width(), thumb.height() / image.height())
            w, h = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(thumb.left() + (thumb.width() - w) / 2,
                                     thumb.top() + (thumb.height() - h) / 2, w, h), image)
        elif s.no_picture(ref):
            glyph = 48
            top = thumb.top() + (thumb.height() - glyph - 22) / 2
            painter.drawImage(QRectF(thumb.left() + (thumb.width() - glyph) / 2, top, glyph,
                                     glyph),
                              icons.image(category_icon(ref), p.text_tertiary, glyph, dpr))
            painter.setPen(QColor(p.text_tertiary))
            painter.setFont(theme.ui_font("caption"))
            painter.drawText(QRectF(thumb.left(), top + glyph + 6, thumb.width(), 16),
                             Qt.AlignmentFlag.AlignCenter, "No preview")
        painter.restore()
        painter.setOpacity(1.0)

        y = thumb.bottom() + SPACE["s"]
        painter.setFont(theme.ui_font("caption"))
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRectF(x, y, width, caption_m.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         caption_m.elidedText(card_caption(ref), Qt.TextElideMode.ElideRight,
                                              int(width)))
        y += caption_m.height() + SPACE["xs"]
        title_top = y
        title = s.title(ref)
        painter.setFont(theme.ui_font("body"))
        painter.setPen(QColor(p.text if included else p.text_secondary))
        for line in wrap_and_elide(title, body_m, int(width), 2):
            painter.drawText(QRectF(x, y, width, body_m.lineSpacing()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
            y += body_m.lineSpacing()
        source = s.subtitle(ref, title)
        if source:  # right under the title, however many lines it took
            painter.setFont(theme.ui_font("caption"))
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(QRectF(x, y, width, caption_m.height()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             caption_m.elidedText(source, Qt.TextElideMode.ElideRight,
                                                  int(width)))
        y = title_top + _title_block(caption_m, body_m)
        size = s.size(ref)
        if size:
            painter.setFont(theme.ui_font("caption"))
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(QRectF(x, y, width, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             human_size(size))
        right = x + width
        for name, token, show in (("copies", p.warning_text, len(ref.mods) > 1),
                                  ("info", p.text_secondary, ref.approximate)):
            if show:
                right -= 16
                painter.drawImage(QRectF(right, y + 1, 16, 16), icons.image(name, token, 16, dpr))
                right -= SPACE["xs"]
        if not included:
            y += 18 + SPACE["xs"]
            painter.drawImage(QRectF(x, y + 1, 16, 16),
                              icons.image("excluded", p.text_secondary, 16, dpr))
            painter.setFont(theme.ui_font("caption"))
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(QRectF(x + 16 + SPACE["xs"], y, width, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             "Not exported")
        painter.restore()


class _RefsModel(QAbstractListModel):
    def __init__(self, refs: list[LocatedRef], state: CcState, parent=None):
        super().__init__(parent)
        self.refs = refs
        self.state = state

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.refs)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ref = self.refs[index.row()]
        if role == RefRole:
            return ref
        if role == Qt.ItemDataRole.DisplayRole:
            return self.state.title(ref)
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.state.tooltip(ref)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return self.state.accessible(ref)
        return None

    def refresh(self) -> None:
        if self.refs:
            self.dataChanged.emit(self.index(0), self.index(len(self.refs) - 1))


def reveal(view: QAbstractItemView, index: QModelIndex) -> None:
    """Scroll the page a view sits in (it's as tall as its content, so it never scrolls
    itself) until the item at `index` is on screen."""
    area = view.parentWidget()
    while area is not None and not isinstance(area, QScrollArea):
        area = area.parentWidget()
    if area is None or area.widget() is None or not index.isValid():
        return
    rect = view.visualRect(index)
    centre = view.viewport().mapTo(area.widget(), rect.center())
    area.ensureVisible(centre.x(), centre.y(), rect.width() // 2 + SPACE["s"],
                       rect.height() // 2 + SPACE["l"])


class CardGrid(QListView):
    """One section's cards, as tall as they need: the page scrolls, not the grid."""

    activated_ref = Signal(object)            # LocatedRef: clicked, or Space/Enter
    menu_requested = Signal(object, QPoint)   # LocatedRef, global position

    def __init__(self, refs: list[LocatedRef], state: CcState, parent=None):
        super().__init__(parent)
        self.state = state
        self.source = _RefsModel(refs, state, self)
        self.setModel(self.source)
        self.setItemDelegate(CardDelegate(state, self))
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSpacing(0)
        if len(refs) > BIG:  # laid out a batch at a time, so the window stays responsive
            self.setLayoutMode(QListView.LayoutMode.Batched)
            self.setBatchSize(500)
        self.setGridSize(QSize(CARD_WIDTH + GAP, _card_metrics()[2] + GAP))
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("CC items")
        if state.mode != STATIC:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self._pressed: QModelIndex | None = None
        self._fit()

    def columns(self) -> int:
        """Cards a row holds: as many as fit at their narrowest. Qt starts a new row when a
        cell would reach the last pixel (right() is width - 1)."""
        return max(1, (self.viewport().width() - 1) // (CARD_WIDTH + GAP))

    def _fit(self) -> None:
        """Share the row's width out between its cards (no empty strip at the end), and be as
        tall as the rows."""
        columns = self.columns()
        cell = max(CARD_WIDTH + GAP, (self.viewport().width() - 1) // columns)
        if cell != self.gridSize().width():
            self.setGridSize(QSize(cell, self.gridSize().height()))
        rows = math.ceil(self.source.rowCount() / columns) if self.width() > 0 else 1
        self.setFixedHeight(max(rows, 1) * self.gridSize().height())

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()  # the page scrolls

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._pressed = self.indexAt(event.position().toPoint())
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.position().toPoint())
        super().mouseReleaseEvent(event)
        if (event.button() == Qt.MouseButton.LeftButton and index.isValid()
                and self._pressed is not None and index == self._pressed):
            self.activated_ref.emit(index.data(RefRole))
        self._pressed = None

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # Its second release finds no press (cleared by the first), so a double click acts
        # once, like in the list: a quick second click doesn't undo the first.
        event.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        index = self.currentIndex()
        if index.isValid() and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return,
                                               Qt.Key.Key_Enter):
            self.activated_ref.emit(index.data(RefRole))
            return
        if index.isValid() and event.key() == Qt.Key.Key_Menu and self.state.mode != STATIC:
            self.menu_requested.emit(index.data(RefRole),
                                     self.viewport().mapToGlobal(self.visualRect(index).center()))
            return
        super().keyPressEvent(event)
        reveal(self, self.currentIndex())  # the arrow keys moved it: the page follows

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        if not self.currentIndex().isValid() and self.source.rowCount():
            self.setCurrentIndex(self.source.index(0))

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.pos())
        if index.isValid() and self.state.mode != STATIC:
            self.menu_requested.emit(index.data(RefRole), event.globalPos())


class CardSection(QWidget):
    """A titled group of cards with its count and, in a household, its size."""

    def __init__(self, title: str, refs: list[LocatedRef], total: int, state: CcState,
                 parent=None):
        super().__init__(parent)
        self.refs = refs
        self.state = state
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, SPACE["l"])
        col.setSpacing(SPACE["m"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["s"])
        head.addWidget(label(title, "title"))
        self.count_label = label(str(total) if len(refs) == total else
                                 f"{len(refs)} of {total}", "secondary")
        head.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addStretch(1)
        self.size_label = label("", "secondary")
        head.addWidget(self.size_label, 0, Qt.AlignmentFlag.AlignBaseline)
        col.addLayout(head)
        self.grid = CardGrid(refs, state)
        col.addWidget(self.grid)
        self.refresh_size()

    def refresh_size(self) -> None:
        total = sum(self.state.size(r) or 0 for r in self.refs)
        self.size_label.setText(human_size(total) if total else "")


# The list


class _TableModel(QAbstractTableModel):
    def __init__(self, refs: list[LocatedRef], state: CcState, columns: list[str], parent=None):
        super().__init__(parent)
        self.refs = refs
        self.state = state
        self.columns = columns

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.refs)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section]
        return None

    def flags(self, index: QModelIndex):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if index.column() == 0 and self.state.mode == TOGGLE:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def _text(self, ref: LocatedRef, column: str) -> str:
        s = self.state
        if column == "Name":
            return s.title(ref)
        if column == "Type":
            return item_label(ref)
        if column == "Package":
            return ref.used.rel if ref.used else ""
        if column == "Size":
            size = s.size(ref)
            return human_size(size) if size else ""
        if column == "Used in":
            return usage_summary(ref, s.with_names)
        notes = []
        if s.mode == TOGGLE and not s.included(ref):
            notes.append("Not exported")
        if len(ref.mods) > 1:
            notes.append(f"{len(ref.mods)} copies in Mods")
        if ref.approximate:
            notes.append("Approximate match")
        return ", ".join(notes)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ref = self.refs[index.row()]
        column = self.columns[index.column()]
        s = self.state
        if role == RefRole:
            return ref
        if role == Qt.ItemDataRole.DisplayRole:
            return self._text(ref, column)
        if role == Qt.ItemDataRole.ToolTipRole:
            if column == "Package":
                return ref.used.rel if ref.used else None
            if column == "Used in":
                return usage_text(ref) or None
            return s.tooltip(ref)
        if role == Qt.ItemDataRole.CheckStateRole and column == "Name" and s.mode == TOGGLE:
            return Qt.CheckState.Checked if s.included(ref) else Qt.CheckState.Unchecked
        if role == Qt.ItemDataRole.ForegroundRole and s.mode == TOGGLE and not s.included(ref):
            return QColor(theme.current().text_tertiary)
        if role == Qt.ItemDataRole.ForegroundRole and column != "Name":
            return QColor(theme.current().text_secondary)
        if role == Qt.ItemDataRole.TextAlignmentRole and column == "Size":
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.AccessibleTextRole and column == "Name":
            return s.accessible(ref)
        return None

    def refresh(self) -> None:
        if self.refs:
            self.dataChanged.emit(self.index(0, 0),
                                  self.index(len(self.refs) - 1, len(self.columns) - 1))


class _NameDelegate(QStyledItemDelegate):
    """The Name column: a small picture, the name, and a tick for what's exported."""

    def __init__(self, state: CcState, parent=None):
        super().__init__(parent)
        self.state = state

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(260, LIST_THUMB + 2 * SPACE["s"])

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.current()
        s = self.state
        ref: LocatedRef = index.data(RefRole)
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        x = rect.left() + SPACE["s"]
        included = s.included(ref) or s.mode != TOGGLE
        dpr = painter.device().devicePixelRatioF()
        if s.mode == TOGGLE:  # a tick box
            box = QRectF(x, rect.center().y() - 9, 18, 18)
            painter.setPen(QPen(QColor(p.text if included else p.border_strong), 1.5))
            painter.setBrush(QColor(p.accent) if included else Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(box.adjusted(0.75, 0.75, -0.75, -0.75), 4, 4)
            if included:
                painter.drawImage(box.adjusted(2, 2, -2, -2),
                                  icons.image("check", p.on_accent, 14, dpr))
            x += 18 + SPACE["m"]
        thumb = QRectF(x, rect.center().y() - LIST_THUMB / 2, LIST_THUMB, LIST_THUMB)
        clip = QPainterPath()
        clip.addRoundedRect(thumb, RADIUS["control"], RADIUS["control"])
        if not included:
            painter.setOpacity(0.4)
        painter.save()
        painter.setClipPath(clip)
        painter.fillRect(thumb, QColor(p.sidebar))
        image = s.thumbnail(ref)
        if image is not None:
            scale = min(thumb.width() / image.width(), thumb.height() / image.height())
            w, h = image.width() * scale, image.height() * scale
            painter.drawImage(QRectF(thumb.left() + (thumb.width() - w) / 2,
                                     thumb.top() + (thumb.height() - h) / 2, w, h), image)
        else:
            glyph = 20
            painter.drawImage(QRectF(thumb.center().x() - glyph / 2,
                                     thumb.center().y() - glyph / 2, glyph, glyph),
                              icons.image(category_icon(ref), p.text_tertiary, glyph, dpr))
        painter.restore()
        painter.setOpacity(1.0)
        x = thumb.right() + SPACE["m"]
        width = rect.right() - SPACE["s"] - x
        font = theme.ui_font("body", QFont.Weight.Medium)
        painter.setFont(font)
        painter.setPen(QColor(p.text if included else p.text_tertiary))
        metrics = QFontMetrics(font)
        painter.drawText(QRectF(x, rect.top(), width, rect.height()),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         metrics.elidedText(s.title(ref), Qt.TextElideMode.ElideRight,
                                            int(width)))
        painter.restore()


class CcTable(QTreeView):
    """Every item as a row; a click on a heading sorts by that column."""

    activated_ref = Signal(object)            # LocatedRef: the tick, or Space
    menu_requested = Signal(object, QPoint)
    sort_requested = Signal(str)               # a SORTS key

    _SORT_OF: ClassVar[dict[str, str]] = {"Name": "name", "Type": "category", "Package": "package",
                                          "Size": "size"}

    def __init__(self, refs: list[LocatedRef], state: CcState, sort: str, sizes: bool,
                 usage: bool, parent=None):
        super().__init__(parent)
        self.state = state
        columns = ["Name", "Type", "Package"] + (["Size"] if sizes else []) \
            + (["Used in"] if usage else []) + ["Notes"]
        self.source = _TableModel(refs, state, columns, self)
        self.setModel(self.source)
        self.setItemDelegateForColumn(0, _NameDelegate(state, self))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(False)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMouseTracking(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setAccessibleName("CC items")
        header = self.header()
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        # Shares of the width, worked out on every resize (_share_width): fitting columns to
        # their contents reads every row (seconds for a big lot), and fixed widths leave the
        # name nothing in a narrow window. Paths are shortened; their tooltips have them whole.
        for i in range(len(columns)):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self._sort = sort
        self._show_sort()
        header.sectionClicked.connect(self._heading_clicked)
        self.clicked.connect(self._clicked)
        self._fit()

    _SHARES: ClassVar[dict[str, tuple[float, int]]] = {  # column: (share, minimum px)
        "Name": (0.30, 200), "Type": (0.11, 80), "Package": (0.22, 120), "Size": (0.08, 70),
        "Used in": (0.19, 120), "Notes": (0.10, 80)}

    def _share_width(self) -> None:
        columns = self.source.columns
        total = sum(self._SHARES[c][0] for c in columns)
        width = self.viewport().width()
        for i, column in enumerate(columns[:-1]):  # the last one takes what's left
            share, minimum = self._SHARES[column]
            self.setColumnWidth(i, max(minimum, int(width * share / total)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._share_width()

    def _fit(self) -> None:
        rows = self.source.rowCount()
        row = LIST_THUMB + 2 * SPACE["s"]
        self.setFixedHeight(self.header().sizeHint().height() + rows * row + 4)

    def _show_sort(self) -> None:
        """The arrow on the heading of the column the items are sorted by, if any."""
        columns = self.source.columns
        shown = next((c for c, k in self._SORT_OF.items() if self._sort.startswith(k)), None)
        if shown in columns:
            self.header().setSortIndicator(
                columns.index(shown), Qt.SortOrder.DescendingOrder
                if self._sort in ("name_desc", "size") else Qt.SortOrder.AscendingOrder)
        else:
            self.header().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

    def _heading_clicked(self, section: int) -> None:
        """Sort by the column clicked (again: the other way round, for name and size). The
        header flips its arrow by itself first; it's put back to the sort applied."""
        key = self._SORT_OF.get(self.source.columns[section])
        if key == "name":
            key = "name_desc" if self._sort == "name" else "name"
        elif key == "size":
            key = "size_asc" if self._sort == "size" else "size"
        self._show_sort()
        if key is not None and key != self._sort:
            self.sort_requested.emit(key)

    def _clicked(self, index: QModelIndex) -> None:
        """TOGGLE: the tick (the Name column); EXPORT: anywhere on the row, like a card."""
        if (index.column() == 0 and self.state.mode == TOGGLE) or self.state.mode == EXPORT:
            self.activated_ref.emit(index.data(RefRole))

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()  # the page scrolls

    def drawRow(self, painter: QPainter, option, index: QModelIndex) -> None:  # noqa: N802
        super().drawRow(painter, option, index)
        current = self.currentIndex()
        if self.hasFocus() and current.isValid() and current.row() == index.row():
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(theme.current().focus), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            row = QRectF(0, option.rect.top(), self.viewport().width(), option.rect.height())
            painter.drawRoundedRect(row.adjusted(1, 1, -1, -1), RADIUS["control"],
                                    RADIUS["control"])
            painter.restore()

    def focusInEvent(self, event) -> None:  # noqa: N802
        super().focusInEvent(event)
        self.viewport().update()  # the ring spans the row, not just the current cell

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        self.viewport().update()

    def currentChanged(self, current: QModelIndex, previous: QModelIndex) -> None:  # noqa: N802
        super().currentChanged(current, previous)
        self.viewport().update()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        index = self.currentIndex()
        if index.isValid() and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return,
                                               Qt.Key.Key_Enter):
            if self.state.mode != STATIC:
                self.activated_ref.emit(index.data(RefRole))
            return
        super().keyPressEvent(event)
        reveal(self, self.currentIndex())  # the arrow keys moved it: the page follows

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        index = self.indexAt(event.pos())
        if index.isValid() and self.state.mode != STATIC:
            self.menu_requested.emit(index.data(RefRole), event.globalPos())



# The browser


SHOWS_TOGGLE = [("all", "All items"), ("included", "Included"), ("left_out", "Left out"),
                ("copies", "Several copies in Mods"), ("approximate", "Approximate matches")]
SHOWS = [("all", "All items"), ("copies", "Several copies in Mods"),
         ("approximate", "Approximate matches")]


class CcBrowser(QWidget):
    """Search, filters, sort and the cards or list of a set of CC items. What it shows is
    worked out in Python (fast for thousands of items) and handed to a few views."""

    toggled = Signal(object, bool)        # TrayRef, included (TOGGLE)
    many_toggled = Signal(object, bool)   # set of TrayRefs, included: Include all, Leave all out
    item_activated = Signal(object)       # LocatedRef (EXPORT: export it on its own)
    menu_requested = Signal(object, QPoint)  # LocatedRef, global position
    view_changed = Signal()               # view, sort or filter changed (to remember them)

    def __init__(self, refs: list[LocatedRef], state: CcState, *, view: str = "cards",
                 sort: str = "category", usage: bool = True, placeholder: str = "Search",
                 filters: tuple[str, str, str] = ("", "all", "all"),
                 rank: dict[TrayRef, int] | None = None, parent=None):
        """filters: (search, show, category) to start with, as filters() gave them; rank: the
        size order of the browser this one replaces (rank()), so cards don't move."""
        super().__init__(parent)
        self.refs = refs
        self.state = state
        # Asks for thumbnails only while the browser is there: it goes with it.
        state._timer.setParent(self)
        self.usage = usage
        self.view = view
        self.sort = sort if sort in dict(SORTS) else "category"
        self.query, self.show_filter, self.category = filters
        self._rank = rank if self.sort in SIZE_SORTS else None  # size order, fixed meanwhile
        self.sections: list[CardSection] = []
        self.table: CcTable | None = None
        self._search_timer = QTimer(self, singleShot=True, interval=200)
        self._search_timer.timeout.connect(self.refill)

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["m"])
        tools = QHBoxLayout()
        tools.setSpacing(SPACE["s"])
        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.setAccessibleName(placeholder)
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(180)
        self.search.setMaximumWidth(320)
        self._search_icon = self.search.addAction(icons.icon("search", theme.current()
                                                             .text_tertiary, 16),
                                                  QLineEdit.ActionPosition.LeadingPosition)
        self.search.setText(self.query)
        self.search.textChanged.connect(self._query_changed)
        tools.addWidget(self.search, 1)
        self.show_button = MenuButton("Show", SHOWS_TOGGLE if state.mode == TOGGLE else SHOWS,
                                      self.show_filter)
        self.show_filter = self.show_button.current  # one this mode has
        self.show_button.chosen.connect(self._show_chosen)
        self.category_button = MenuButton("Category", self._categories(), self.category)
        self.category = self.category_button.current
        self.category_button.chosen.connect(self._category_chosen)
        self.sort_button = MenuButton("Sort", self._sorts(), self.sort)
        self.sort_button.chosen.connect(self.set_sort)
        for b in (self.show_button, self.category_button, self.sort_button):
            tools.addWidget(b)
        tools.addStretch(1)
        tools.addWidget(self._view_toggle())
        col.addLayout(tools)

        info = QHBoxLayout()
        info.setSpacing(SPACE["m"])
        self.match_label = label("", "secondary")
        info.addWidget(self.match_label)
        self.all_buttons: list[QPushButton] = []
        if state.mode == TOGGLE:
            for text, included in (("Include all", True), ("Leave all out", False)):
                button = link_button(text)
                button.setAccessibleName(text)
                button.setToolTip(f"{'Put every item back in' if included else 'Leave every item out of'}"
                                  " the export at once. While you search or filter, only the "
                                  "items shown.")
                button.clicked.connect(lambda _c=False, i=included: self._set_all(i))
                info.addWidget(button)
                self.all_buttons.append(button)
        info.addStretch(1)
        col.addLayout(info)
        self.no_match = label("", "secondary", wrap=True)
        self.no_match.hide()
        col.addWidget(self.no_match)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        col.addLayout(self.body)
        self._holder: QWidget | None = None
        self.refill()

    # Choices

    def _sorts(self) -> list[tuple[str, str]]:
        """Size sorts where items have sizes: a household's, once its plan is in (until then
        they're listed but sort by name, so a size sort kept from the last item still shows
        as chosen)."""
        sized = self.state.mode == TOGGLE or self.state.sizes is not None
        return [(k, t) for k, t in SORTS if k not in SIZE_SORTS or sized]

    def rank(self) -> dict[TrayRef, int] | None:
        """The size order shown, if sorted by size."""
        return self._rank if self.sort in SIZE_SORTS else None

    def filters(self) -> tuple[str, str, str]:
        """(search, show, category), for a browser built again for the same items."""
        return self.query, self.show_filter, self.category

    def _categories(self) -> list[tuple[str, str]]:
        counts: dict[str, int] = {}
        for r in self.refs:
            counts[r.ref.group] = counts.get(r.ref.group, 0) + 1
        order = {g: i for i, g in enumerate(GROUP_ORDER)}  # as group_refs orders sections
        return [("all", f"All ({len(self.refs):,})")] + [
            (g, f"{g} ({n:,})") for g, n in sorted(counts.items(),
                                                    key=lambda kv: order.get(kv[0], 99))]

    def _view_toggle(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        for i, (view, text) in enumerate((("cards", "Cards"), ("list", "List"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self.view == view)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else "last")
            b.setAccessibleName(f"Show CC as {text.lower()}")
            b.clicked.connect(lambda _c=False, v=view: self.set_view(v))
            group.addButton(b)
            row.addWidget(b)
        return box

    def set_view(self, view: str) -> None:
        if view != self.view:
            self.view = view
            self.refill()
            self.view_changed.emit()

    def set_sort(self, sort: str) -> None:
        if sort != self.sort and sort in dict(SORTS):
            self.sort = sort
            self._rank = None  # sizes as they are now decide a size order
            self.sort_button.set_options(self._sorts(), sort)
            self.refill()
            self.view_changed.emit()

    def _show_chosen(self, show: str) -> None:
        self.show_filter = show
        self.refill()
        self.view_changed.emit()

    def _category_chosen(self, category: str) -> None:
        self.category = category
        self.refill()
        self.view_changed.emit()

    def _query_changed(self, text: str) -> None:
        self.query = text
        if len(self.refs) > BIG:  # thousands of items: wait for a pause in the typing
            self._search_timer.start()
        else:
            self.refill()
        self.view_changed.emit()

    # What's shown

    def matches(self, ref: LocatedRef) -> bool:
        s = self.state
        if self.category != "all" and ref.ref.group != self.category:
            return False
        show = self.show_filter
        if show == "included" and not s.included(ref):
            return False
        if show == "left_out" and s.included(ref):
            return False
        if show == "copies" and len(ref.mods) < 2:
            return False
        if show == "approximate" and not ref.approximate:
            return False
        words = self.query.casefold().split()
        if not words:
            return True
        name = s.names.get(ref.ref)
        haystack = " ".join([s.title(ref), name.title if name else "",
                             name.creator if name else "", name.raw if name else "",
                             ref.used.rel if ref.used else "", item_label(ref),
                             card_caption(ref)]).casefold()
        return all(word in haystack for word in words)

    def shown(self) -> list[LocatedRef]:
        """The items shown, in their order."""
        return [r for _title, refs, _total in self._groups() for r in refs]

    def _groups(self) -> list[tuple[str, list[LocatedRef], int]]:
        """(title, items shown, items in it before filtering) per section, in order."""
        s = self.state
        refs = [r for r in self.refs if self.matches(r)]
        title = lambda r: s.title(r).casefold()  # noqa: E731
        if self.sort == "category":
            totals: dict[str, int] = {}
            for r in self.refs:
                totals[r.ref.group] = totals.get(r.ref.group, 0) + 1
            return [(g, rs, totals[g]) for g, rs in group_refs(refs, s.title)]
        if self.sort in ("name", "name_desc"):
            ordered = sorted(refs, key=title, reverse=self.sort == "name_desc")
            return [("A to Z" if self.sort == "name" else "Z to A", ordered, len(self.refs))]
        if self.sort == "package":
            ordered = sorted(refs, key=lambda r: ((r.used.rel if r.used else "").casefold(),
                                                  title(r)))
            return [("By package", ordered, len(self.refs))]
        if self._rank is None and s.sizes is not None:
            # By size alone (items of the same size share a rank, and go A to Z both ways),
            # frozen while this sort lasts: an item left out keeps its place.
            levels = sorted({s.sizes.get(r.ref) or 0 for r in self.refs}, reverse=True)
            level = {size: i for i, size in enumerate(levels)}
            self._rank = {r.ref: level[s.sizes.get(r.ref) or 0] for r in self.refs}
        rank = self._rank or {}
        sign = -1 if self.sort == "size_asc" else 1  # ties stay A to Z either way
        ordered = sorted(refs, key=lambda r: (sign * rank.get(r.ref, len(rank)), title(r)))
        return [("Largest first" if self.sort == "size" else "Smallest first", ordered,
                 len(self.refs))]

    def refill(self) -> None:
        """Show what the search, filters and sort pick: a few views, built in milliseconds.
        The item the keyboard is on keeps the focus, wherever it moved to."""
        focus = self.window().focusWidget() if self.window() is not None else None
        had_focus = (focus is not None and self._holder is not None
                     and self._holder.isAncestorOf(focus))
        focused_ref = self.focused_ref() if had_focus else None
        position = self._position(focused_ref)
        holder = QWidget()
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(0)
        groups = self._groups()
        self.sections, self.table = [], None
        if self.view == "cards":
            for title, refs, total in groups:
                if not refs:
                    continue
                section = CardSection(title, refs, total, self.state)
                section.grid.activated_ref.connect(self._activated)
                section.grid.menu_requested.connect(self.menu_requested.emit)
                self.sections.append(section)
                col.addWidget(section)
        else:
            refs = [r for _t, rs, _n in groups for r in rs]
            if refs:
                self.table = CcTable(refs, self.state, self.sort, self.state.sizes is not None,
                                     self.usage)
                self.table.activated_ref.connect(self._activated)
                self.table.menu_requested.connect(self.menu_requested.emit)
                self.table.sort_requested.connect(self.set_sort)
                col.addWidget(self.table)
        if self._holder is not None:
            self.body.removeWidget(self._holder)
            self._holder.hide()  # deleted later: until then it mustn't show through
            self._holder.deleteLater()
        self._holder = holder
        self.body.addWidget(holder)
        if self.isVisible():
            holder.show()  # now, not when Qt gets to it: the focus can only go to what shows
        shown = sum(len(rs) for _t, rs, _n in groups)
        filtered = (bool(self.query.strip()) or self.show_filter != "all"
                    or self.category != "all")
        self.match_label.setText(f"{shown:,} of {len(self.refs):,} shown" if filtered
                                 else plural(len(self.refs), "item"))
        if shown == 0:
            what = f"“{self.query.strip()}”" if self.query.strip() else "these filters"
            self.no_match.setText(f"Nothing matches {what}. Search looks at names, creators and "
                                  "packages.")
        self.no_match.setVisible(shown == 0)
        if had_focus:  # keyboard users keep their place
            self.focus_item(focused_ref, position)

    def focused_ref(self) -> LocatedRef | None:
        """The item the keyboard is on, if the focus is in the cards or the list."""
        focus = self.window().focusWidget() if self.window() is not None else None
        if isinstance(focus, (CardGrid, CcTable)) and self.isAncestorOf(focus) \
                and focus.currentIndex().isValid():
            return focus.currentIndex().data(RefRole)
        return None

    def _position(self, ref: LocatedRef | None) -> int:
        views = [s.grid for s in self.sections] + ([self.table] if self.table else [])
        shown = [r for view in views for r in view.source.refs]
        return shown.index(ref) if ref is not None and ref in shown else 0

    def focus_item(self, ref: LocatedRef | None, position: int = 0) -> None:
        """Put the keyboard on `ref`, or, when it's no longer shown (it left a filter), on
        the item that took its place; the page scrolls to it."""
        views = [s.grid for s in self.sections] + ([self.table] if self.table else [])
        places = [(view, row) for view in views for row in range(len(view.source.refs))]
        if not places:
            self.search.setFocus()
            return
        target = next(((v, row) for v, row in places if v.source.refs[row] is ref), None)
        view, row = target or places[min(position, len(places) - 1)]
        view.setCurrentIndex(view.source.index(row, 0))
        view.setFocus()
        reveal(view, view.source.index(row, 0))

    # Changes from outside

    def refresh(self) -> None:
        """Names arrived: every order and search uses them, so it's all shown again (in
        milliseconds), the focus kept."""
        self.refill()

    def _repaint(self) -> None:
        """What's left out changed: same items, same order; cards, rows and sizes repaint."""
        for section in self.sections:
            section.grid.source.refresh()
            section.refresh_size()
        if self.table is not None:
            self.table.source.refresh()

    def repaint_items(self) -> None:
        """Only pictures arrived: nothing moves."""
        for section in self.sections:
            section.grid.viewport().update()
        if self.table is not None:
            self.table.viewport().update()

    def set_sizes(self, sizes: dict[TrayRef, int] | None) -> None:
        had = self.state.sizes is not None
        self.state.sizes = sizes
        self.sort_button.set_options(self._sorts(), self.sort)
        if had != (sizes is not None):
            # The first sizes: a size order can be worked out (unless one was handed over from
            # the browser this one replaced), and the list gets its Size column.
            self.refill()
        else:
            for section in self.sections:
                section.refresh_size()
            self.repaint_items()
            if self.table is not None:
                self.table.source.refresh()

    # Clicks

    def _activated(self, ref: LocatedRef) -> None:
        if self.state.mode == TOGGLE:
            included = not self.state.included(ref)
            if included:
                self.state.excluded.discard(ref.ref)
            else:
                self.state.excluded.add(ref.ref)
            self.toggled.emit(ref.ref, included)
            if self.show_filter in ("included", "left_out"):
                QTimer.singleShot(0, self.refill)  # it leaves the filter; after the click
            else:
                self._repaint()
        elif self.state.mode == EXPORT:
            self.item_activated.emit(ref)

    def _set_all(self, included: bool) -> None:
        refs = {r.ref for r in self.refs if self.matches(r)}
        before = set(self.state.excluded)
        if included:
            self.state.excluded -= refs
        else:
            self.state.excluded |= refs
        if self.state.excluded != before:
            self.many_toggled.emit(refs, included)
            self.refill()
