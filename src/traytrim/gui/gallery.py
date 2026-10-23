"""The tray gallery in the sidebar: households with portraits, lots and rooms, one kind at a
time, searchable, keyboard friendly."""

from __future__ import annotations

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QRectF, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem)

from traytrim import tray
from traytrim.gui import icons, theme
from traytrim.gui.describe import item_subtitle
from traytrim.gui.theme import RADIUS, SPACE, TYPE
from traytrim.tray import TrayItem

PORTRAIT = 48
ROW_HEIGHT = PORTRAIT + 2 * SPACE["s"]
ItemRole = Qt.ItemDataRole.UserRole + 1


class TrayModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[TrayItem] = []
        self.portraits: dict[int, QImage] = {}  # Sim id -> face
        self.pictures: dict[int, QImage] = {}   # tray item -> its own picture

    def set_items(self, items: list[TrayItem]) -> None:
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def set_portraits(self, portraits: dict[int, QImage]) -> None:
        self.portraits.update(portraits)
        if self.items:
            self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))

    def set_pictures(self, pictures: dict[int, QImage]) -> None:
        self.pictures.update(pictures)
        if self.items:
            self.dataChanged.emit(self.index(0), self.index(len(self.items) - 1))

    def portrait_for(self, item: TrayItem) -> QImage | None:
        """A Sim's face for a household (it reads better at this size than the family
        picture), else the item's own picture."""
        face = next((self.portraits[s.sim_id] for s in item.sims if s.sim_id in self.portraits),
                    None)
        return face or self.pictures.get(item.instance)

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        item = self.items[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return item.name or f"Unnamed {item.kind}"
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{item.name}: {subtitle(item)}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return subtitle(item) or None
        if role == ItemRole:
            return item
        return None


def sims_text(item: TrayItem) -> str:
    names = [s.name for s in item.sims]
    return ", ".join(names) if names else "No Sims"


def subtitle(item: TrayItem) -> str:
    """The second line of a row: the Sims of a household, the size of a lot or room."""
    if item.type == tray.ITEM_HOUSEHOLD:
        return sims_text(item)
    return item_subtitle(item)


class TrayFilter(QSortFilterProxyModel):
    """One kind of tray item, matching the name or any Sim's name, case-insensitively."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""
        self._type = tray.ITEM_HOUSEHOLD

    def _change(self, apply) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt 6.10+
            self.beginFilterChange()
            apply()
            self.endFilterChange()
        else:
            apply()
            self.invalidateFilter()

    def set_text(self, text: str) -> None:
        self._change(lambda: setattr(self, "_needle", text.strip().casefold()))

    def set_type(self, item_type: int) -> None:
        self._change(lambda: setattr(self, "_type", item_type))

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        item: TrayItem = self.sourceModel().items[row]
        if item.type != self._type:
            return False
        if not self._needle:
            return True
        haystack = " ".join([item.name, *(s.name for s in item.sims)]).casefold()
        return all(word in haystack for word in self._needle.split())


class TrayDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.current()
        item: TrayItem = index.data(ItemRole)
        model: TrayModel = index.model().sourceModel()
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        bg = QRectF(rect.adjusted(SPACE["s"], 1, -SPACE["s"], -1))
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.selection if selected else p.surface_hover))
            painter.drawRoundedRect(bg, RADIUS["card"], RADIUS["card"])
        view = option.widget
        if option.state & QStyle.StateFlag.State_HasFocus and view is not None and view.hasFocus():
            pen = QPen(QColor(p.focus))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(bg.adjusted(1, 1, -1, -1), RADIUS["card"], RADIUS["card"])

        x = rect.left() + SPACE["s"] + SPACE["s"]
        art = QRectF(x, rect.top() + SPACE["s"], PORTRAIT, PORTRAIT)
        clip = QPainterPath()
        clip.addRoundedRect(art, RADIUS["control"], RADIUS["control"])
        image = model.portrait_for(item)
        if image is None and item.type != tray.ITEM_HOUSEHOLD:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.surface_hover if not selected else p.window))
            painter.drawPath(clip)
            size = 24
            glyph = icons.image("lot" if item.type == tray.ITEM_LOT else "room",
                                p.text_secondary, size, painter.device().devicePixelRatioF())
            painter.drawImage(QRectF(art.center().x() - size / 2, art.center().y() - size / 2,
                                     size, size), glyph)
        elif image is not None:
            painter.setClipPath(clip)
            src = QRectF(0, 0, image.width(), image.height())
            side = min(src.width(), src.height())
            src = QRectF((src.width() - side) / 2, 0, side, side)  # keep the top, crop sides
            painter.drawImage(art, image, src)
            painter.setClipping(False)
        else:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.surface_hover if not selected else p.window))
            painter.drawPath(clip)
            painter.setPen(QColor(p.text_secondary))
            f = theme.ui_font("title", QFont.Weight.Medium)
            painter.setFont(f)
            initial = (item.name or "?").strip()[:1].upper() or "?"
            painter.drawText(art, Qt.AlignmentFlag.AlignCenter, initial)

        text_left = int(art.right()) + SPACE["m"]
        text_width = rect.right() - SPACE["l"] - text_left
        name_font = theme.ui_font("body", QFont.Weight.Medium)
        painter.setFont(name_font)
        painter.setPen(QColor(p.text))
        name_rect = QRect(text_left, rect.top() + SPACE["s"] + 4, text_width, TYPE["body"] + 6)
        name = painter.fontMetrics().elidedText(index.data(), Qt.TextElideMode.ElideRight,
                                                text_width)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         name)
        painter.setFont(theme.ui_font("caption"))
        painter.setPen(QColor(p.text_secondary))
        sims_rect = QRect(text_left, name_rect.bottom() + 4, text_width, TYPE["caption"] + 6)
        second = painter.fontMetrics().elidedText(subtitle(item), Qt.TextElideMode.ElideRight,
                                                  text_width)
        painter.drawText(sims_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         second)
        painter.restore()


class TrayList(QListView):
    item_selected = Signal(object)  # TrayItem or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = TrayModel(self)
        self.proxy = TrayFilter(self)
        self.proxy.setSourceModel(self.source)
        self.setModel(self.proxy)
        self.setItemDelegate(TrayDelegate(self))
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setAccessibleName("Tray items")
        self._filtering = False
        self._chosen: TrayItem | None = None
        self.selectionModel().currentChanged.connect(self._current_changed)

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._filtering:
            return  # rows moving because of a search aren't a choice the user made
        item = current.data(ItemRole) if current.isValid() else None
        self._chosen = item
        self.item_selected.emit(item)

    def set_filter(self, text: str, item_type: int | None = None) -> None:
        """Filter rows; the chosen item stays chosen (and selected while it's visible)."""
        self._filtering = True
        try:
            if item_type is not None:
                self.proxy.set_type(item_type)
            self.proxy.set_text(text)
            self.selectionModel().clearSelection()
            self.setCurrentIndex(QModelIndex())
            if self._chosen is not None:
                for row in range(self.proxy.rowCount()):
                    index = self.proxy.index(row, 0)
                    if index.data(ItemRole) is self._chosen:
                        self.setCurrentIndex(index)
                        break
        finally:
            self._filtering = False

    def replace_items(self, items: list[TrayItem]) -> bool:
        """The Tray read again: new items, the chosen one kept chosen if it's still there (by
        its instance; the objects are new). Returns whether it is."""
        chosen = self._chosen.instance if self._chosen is not None else None
        self._filtering = True
        try:
            self.source.set_items(items)
            self._chosen = next((i for i in items if i.instance == chosen), None)
            self.selectionModel().clearSelection()
            self.setCurrentIndex(QModelIndex())
            if self._chosen is not None:
                for row in range(self.proxy.rowCount()):
                    index = self.proxy.index(row, 0)
                    if index.data(ItemRole) is self._chosen:
                        self.setCurrentIndex(index)
                        break
        finally:
            self._filtering = False
        return chosen is None or self._chosen is not None

    def select_item(self, item: TrayItem) -> bool:
        """Choose a tray item shown in the list (the caller sets the right kind first)."""
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if index.data(ItemRole).instance == item.instance:
                self.setCurrentIndex(index)
                self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                return True
        return False

    def visible_count(self) -> int:
        return self.proxy.rowCount()
