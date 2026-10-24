"""The Mods tab's list in the sidebar: every package the game loads, in load order, searchable."""

from __future__ import annotations

from pathlib import PurePosixPath

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QRectF, QSize,
                            QSortFilterProxyModel, Qt, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem)

from traytrim import restypes, settings
from traytrim.gui import icons, theme
from traytrim.gui.gallery import change_filter
from traytrim.gui.describe import plural
from traytrim.gui.theme import RADIUS, SPACE, TYPE
from traytrim.index import ModsPackageInfo
from traytrim.units import human_size

TILE = 32
ROW_HEIGHT = TILE + 2 * SPACE["s"]
PackageRole = Qt.ItemDataRole.UserRole + 1


def package_title(p: ModsPackageInfo) -> str:
    return PurePosixPath(p.rel).stem


def package_subtitle(p: ModsPackageInfo) -> str:
    """"Merged · 1.2 GB · 340 items": its folder inside Mods, if any, then size and items."""
    folder = str(PurePosixPath(p.rel).parent)
    parts = [] if folder == "." else [folder]
    parts.append(human_size(p.size))
    if p.items:
        parts.append(plural(p.items, "item"))
    return " · ".join(parts)


class ModsModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.packages: list[ModsPackageInfo] = []

    def set_packages(self, packages: list[ModsPackageInfo]) -> None:
        self.beginResetModel()
        self.packages = packages
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.packages)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        p = self.packages[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return package_title(p)
        if role == Qt.ItemDataRole.AccessibleTextRole:
            return f"{package_title(p)}, {package_subtitle(p)}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return p.rel
        if role == PackageRole:
            return p
        return None


# (menu text, button text) of the choices settings keep: (key, menu text, button text).
_SORT_TEXT = {"name": ("A to Z (the order the game loads them)", "A to Z"),
              "name_desc": ("Z to A", "Z to A"), "size": ("Largest first", "Largest"),
              "size_asc": ("Smallest first", "Smallest"), "newest": ("Newest first", "Newest"),
              "items": ("Most items first", "Most items")}
_SHOW_TEXT = {"all": ("All packages", "All"), "cas": ("With CAS parts", "CAS"),
              "objects": ("With objects", "Objects"), "build": ("With build items", "Build"),
              "sliders": ("With sliders, sculpts or skin tones", "Sliders"),
              "none": ("Without CC items (tuning, scripts, meshes)", "No CC items")}
MODS_SORTS = [(k, *_SORT_TEXT[k]) for k in settings.MODS_SORTS]
MODS_SHOWS = [(k, *_SHOW_TEXT[k]) for k in settings.MODS_SHOWS]
_SHOW_TYPES = {"cas": {restypes.CASP}, "objects": {restypes.OBJECT_DEFINITION},
               "build": set(restypes.BUILD_NAMES),
               "sliders": {restypes.SIM_MODIFIER, restypes.SCULPT, restypes.SKIN_TONE}}


_KIND_ICONS = (("cas", "cat-top"), ("objects", "cat-object"), ("build", "cat-build"),
               ("sliders", "cat-slider"))


def kind_icon(p: ModsPackageInfo) -> str:
    """The icon of the kind of item a package holds most of; a box when it holds none (meshes,
    textures, tuning or a merge of those)."""
    counts = {kind: sum(p.kinds.get(t, 0) for t in _SHOW_TYPES[kind]) for kind, _i in _KIND_ICONS}
    best = max(counts, key=counts.get) if counts else None
    return dict(_KIND_ICONS)[best] if best is not None and counts[best] else "package"


def shows(p: ModsPackageInfo, show: str) -> bool:
    """Whether a package is listed for a "Show" choice (MODS_SHOWS)."""
    if show == "none":
        return not p.items
    types = _SHOW_TYPES.get(show)
    return types is None or any(p.kinds.get(t) for t in types)


class ModsFilter(QSortFilterProxyModel):
    """Packages whose path inside Mods holds every word typed, case-insensitively, and that
    hold the kind of items chosen, in load order (alphabetical, as in the Mods folder) or
    another order (MODS_SORTS)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._needle = ""
        self.sort_by = "name"
        self.show = "all"
        self.sort(0)

    def set_sort(self, sort_by: str) -> None:
        self.sort_by = sort_by
        self.invalidate()
        self.sort(0)

    def set_show(self, show: str) -> None:
        change_filter(self, lambda: setattr(self, "show", show))

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:  # noqa: N802
        packages = self.sourceModel().packages
        a, b = packages[left.row()], packages[right.row()]
        key = self.sort_by
        if key == "size" and a.size != b.size:
            return a.size > b.size
        if key == "size_asc" and a.size != b.size:
            return a.size < b.size
        if key == "newest" and a.mtime_ns != b.mtime_ns:
            return a.mtime_ns > b.mtime_ns
        if key == "items" and a.items != b.items:
            return a.items > b.items
        if key == "name_desc":
            return a.load_order > b.load_order
        return a.load_order < b.load_order

    def set_text(self, text: str) -> None:
        change_filter(self, lambda: setattr(self, "_needle", text.strip().casefold()))

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:  # noqa: N802
        package = self.sourceModel().packages[row]
        if not shows(package, self.show):
            return False
        if not self._needle:
            return True
        rel = package.rel.casefold()
        return all(word in rel for word in self._needle.split())


class ModsDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_HEIGHT)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        p = theme.current()
        package: ModsPackageInfo = index.data(PackageRole)
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

        tile = QRectF(rect.left() + 2 * SPACE["s"], rect.top() + SPACE["s"], TILE, TILE)
        clip = QPainterPath()
        clip.addRoundedRect(tile, RADIUS["control"], RADIUS["control"])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.window if selected else p.surface_hover))
        painter.drawPath(clip)
        glyph = 18
        painter.drawImage(QRectF(tile.center().x() - glyph / 2, tile.center().y() - glyph / 2,
                                 glyph, glyph),
                          icons.image(kind_icon(package), p.text_secondary, glyph,
                                      painter.device().devicePixelRatioF()))

        left = int(tile.right()) + SPACE["m"]
        width = rect.right() - SPACE["l"] - left
        painter.setFont(theme.ui_font("body", QFont.Weight.Medium))
        painter.setPen(QColor(p.text))
        top = rect.top() + (ROW_HEIGHT - TYPE["body"] - TYPE["caption"] - 14) // 2
        name_rect = QRect(left, top, width, TYPE["body"] + 6)
        painter.drawText(name_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(index.data(),
                                                          Qt.TextElideMode.ElideMiddle, width))
        painter.setFont(theme.ui_font("caption"))
        painter.setPen(QColor(p.text_secondary))
        sub_rect = QRect(left, name_rect.bottom() + 2, width, TYPE["caption"] + 6)
        painter.drawText(sub_rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                         painter.fontMetrics().elidedText(package_subtitle(package),
                                                          Qt.TextElideMode.ElideRight, width))
        painter.restore()


class ModsList(QListView):
    package_selected = Signal(object)  # ModsPackageInfo or None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.source = ModsModel(self)
        self.proxy = ModsFilter(self)
        self.proxy.setSourceModel(self.source)
        self.setModel(self.proxy)
        self.setItemDelegate(ModsDelegate(self))
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # rows elide
        self.setAccessibleName("Mods packages")
        self._filtering = False
        self._chosen: str | None = None  # path of the chosen package
        self.selectionModel().currentChanged.connect(self._current_changed)

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if self._filtering:
            return
        package = current.data(PackageRole) if current.isValid() else None
        self._chosen = str(package.path) if package else None
        self.package_selected.emit(package)

    def set_packages(self, packages: list[ModsPackageInfo]) -> None:
        """New list (the index changed); the chosen package stays chosen if it's still there."""
        self._filtering = True
        try:
            self.source.set_packages(packages)
            self._reselect()
        finally:
            self._filtering = False

    def set_filter(self, text: str) -> None:
        self._filtering = True
        try:
            self.proxy.set_text(text)
            self._reselect()
        finally:
            self._filtering = False

    def set_show(self, show: str) -> None:
        self._filtering = True
        try:
            self.proxy.set_show(show)
            self._reselect()
        finally:
            self._filtering = False

    def set_sort(self, sort_by: str) -> None:
        self._filtering = True
        try:
            self.proxy.set_sort(sort_by)
            self._reselect()
        finally:
            self._filtering = False
        if self.currentIndex().isValid():
            self.scrollTo(self.currentIndex(), QAbstractItemView.ScrollHint.PositionAtCenter)
        else:
            self.scrollToTop()

    def _reselect(self) -> None:
        self.selectionModel().clearSelection()
        self.setCurrentIndex(QModelIndex())
        if self._chosen is None:
            return
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if str(index.data(PackageRole).path) == self._chosen:
                self.setCurrentIndex(index)
                return

    def forget_choice(self) -> None:
        """Nothing is chosen (the chosen package was deleted)."""
        self._chosen = None
        self._filtering = True
        try:
            self.selectionModel().clearSelection()
            self.setCurrentIndex(QModelIndex())
        finally:
            self._filtering = False

    def select_path(self, path) -> bool:
        """Choose a package by its path (the search is cleared by the caller if needed)."""
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if index.data(PackageRole).path == path:
                self.setCurrentIndex(index)
                self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)
                return True
        return False

    def visible_count(self) -> int:
        return self.proxy.rowCount()
