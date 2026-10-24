"""The single main window: the Tray gallery or the Mods packages on the left; on the right the
selected household, lot, room or package, or a zip being checked."""

from __future__ import annotations

import threading
import time
from pathlib import Path, PurePosixPath
from collections.abc import Callable

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFileDialog, QFrame,
                               QHBoxLayout, QLineEdit, QMainWindow, QPushButton, QSizePolicy,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from traytrim import paths, tray
from traytrim.gui.gallery import PORTRAIT, TrayList, sorts_for
from traytrim.gui.describe import THUMB_TYPES
from traytrim.gui.household_view import HouseholdReport, HouseholdView
from traytrim.gui.cc_view import THUMB
from traytrim.gui.report_parts import MAX_CARDS
from traytrim.gui.images import load_portraits, load_thumbnails, load_tray_pictures
from traytrim.gui.index_status import IndexController, IndexStatus, SkippedPackagesDialog
from traytrim.gui import icons, theme
from traytrim.gui.update_dialog import UpdateCheck, UpdateDialog
from traytrim.gui.tasks import BackgroundTask, describe_error, run_in_background
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import (CoffeeButton, DropOverlay, ElidedLabel, FooterAction,
                                  IconLabel, MenuButton, Spinner, label, link_button, set_role)
from traytrim.index import ResourceIndex
from traytrim.export import default_base_name
from traytrim.gui.export_dialog import ExportDialog, ExportRequest
from traytrim.batch import Choice
from traytrim.gui.batch_dialog import BatchDialog
from traytrim.gui.history_dialog import HistoryDialog
from traytrim.gui.settings_dialog import SettingsDialog
from traytrim.batch import locate_item, pick_sims, plan_refs, scoped_refs
from traytrim.names import item_names
from traytrim.resolve import Where
from traytrim.settings import Settings
from traytrim.gui.mods_list import MODS_SHOWS, MODS_SORTS, ModsList, PackageRole
from traytrim.gui.package_view import PackageView
from traytrim.gui.received_view import ReceivedView
from traytrim.gui.describe import display_name
from traytrim.depindex import DependencyIndex
from traytrim.export import safe_name
from traytrim.pkgreport import (ITEM_TYPES, TrayUsage, dependents, package_report, scan_usage,
                                texture_needs, users)
from traytrim.plan import build_plan
from traytrim.received import Received, check_received

SIDEBAR_WIDTH = 320
# Tray pictures: small in the gallery (a square crop of a 3:2 picture fills the tile at 2x),
# the game's full 300 px for the selected item's header.
GALLERY_PICTURE_WIDTH = PORTRAIT * 3
PICTURE_WIDTH = 300
# The sidebar shows one kind at a time: (setting value, tray item type, tab label, plural noun).
# "mods" isn't a tray item: it lists the Mods packages instead (tray item type None).
KINDS = [("household", tray.ITEM_HOUSEHOLD, "Households", "households"),
         ("lot", tray.ITEM_LOT, "Lots", "lots"),
         ("room", tray.ITEM_ROOM, "Rooms", "rooms"),
         ("mods", None, "Mods", "packages")]
MODS = KINDS[3]


def _folder_stamp(folder: Path | None) -> int | None:
    """A folder's modification time: it changes when files are added, removed or replaced in
    it (the game saves tray items as new files)."""
    try:
        return folder.stat().st_mtime_ns if folder is not None else None
    except OSError:
        return None


class FolderStatus(QWidget):
    """One status-bar slot: "Mods  C:\\...\\Mods", or "Mods folder not found" with a way to fix it."""

    def __init__(self, name: str, on_choose: Callable[[], None], needed_for: str = "",
                 parent=None):
        super().__init__(parent)
        self._name = name
        self._needed_for = needed_for  # what it's used for, said when it's missing
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACE["s"], 0, SPACE["s"], 0)
        lay.setSpacing(SPACE["s"])
        self._alert = IconLabel("alert", "warning_text", 16)
        self._alert.hide()
        self._title = label(name, "caption")
        self._value = ElidedLabel("", "caption", max_width=320)
        self._choose = link_button("Choose folder")
        self._choose.clicked.connect(on_choose)
        for w in (self._alert, self._title, self._value, self._choose):
            lay.addWidget(w)

    def set_path(self, path: Path | None) -> None:
        missing = path is None
        self._alert.setVisible(missing)
        set_role(self._title, "warning" if missing else "caption")
        self.setToolTip(f"Not found. {self._needed_for}" if missing and self._needed_for
                        else "")
        if path is not None:
            self._title.setText(self._name)
            self._value.setText(str(path))
            self._choose.setText("Change")
            self._choose.setAccessibleName(f"Change {self._name} folder")
        else:
            self._title.setText(f"{self._name} folder not found")
            self._value.setText("")
            self._choose.setText("Choose folder")
            self._choose.setAccessibleName(f"Choose {self._name} folder")


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings, check_updates: bool = False):
        super().__init__()
        self.settings = settings
        self._check_updates = check_updates  # ask GitHub for a newer release once shown
        self.folders = paths.resolve_folders(settings.user_dir, settings.game_dir)
        # Each background job carries the generation it was started in; results from an older
        # generation (folder changed, another household picked) are dropped.
        self._tray_gen = 0
        self._analysis_gen = 0
        self._done_gen = -1  # the last analysis generation that produced a report or an error
        self._index_ready = False
        self._tray_ready = False  # the Tray folder is read (the gallery can be searched)
        self._tray_state = ("", "", False)  # title, body, loading
        self._tray_stamp = None  # the Tray folder's modification time when it was read
        self._tray_dirty = False  # the Tray changed; what uses what waits for the Mods check
        self._last_check = 0.0   # when the folders were last checked for changes
        self._full_report: HouseholdReport | None = None  # all Sims, before the Sim filter
        self._selected: tray.TrayItem | None = None
        # tray item -> the user's Sims, left-out items and chosen copies (see batch.Choice)
        self._choices: dict[int, Choice] = {}
        self._names_read: set = set()  # TrayRefs whose packages were read (name found or not)
        self._names_cancel: threading.Event | None = None
        self._kind = next((k for k in KINDS if k[0] == settings.tray_kind), KINDS[0])
        self._last_tray_kind = self._kind if self._kind is not MODS else KINDS[0]
        self._plan_task: BackgroundTask | None = None
        self._plan_gen = 0
        self._plan_timer = QTimer(self, singleShot=True, interval=300)
        self._plan_timer.timeout.connect(self._start_plan)
        # The Mods tab: its package list, and what needs what (references and Tray usage),
        # worked out in the background the first time the tab is opened.
        self._mods_gen = 0           # bumped when the index or the Tray changes
        self._mods_listed = -1       # the generation the list was loaded for
        self._usage: TrayUsage | None = None
        self._usage_error = ""
        self._unreadable: list = []  # packages the dependency index couldn't read
        self._usage_gen = -1         # the generation _usage was worked out for
        self._usage_tray_stamp = None  # the Tray folder's modification time it was read at
        self._usage_task: BackgroundTask | None = None
        self._package = None         # ModsPackageInfo shown on the right
        self._package_gen = 0
        self._package_loading = -1   # the generation still being read, if any
        # Check a zip
        self._received: Received | None = None
        self._received_task: BackgroundTask | None = None
        self._pending_zip: Path | None = None
        self._item_planning = False  # an "export this item alone" is being worked out
        self.setWindowTitle("TrayTrim")
        self.setAcceptDrops(True)
        self.setMinimumSize(960, 600)

        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        self._update_batch_button()  # off, saying why, until the index and the Tray are read
        divider = QFrame(objectName="sidebarDivider")
        divider.setFixedWidth(1)
        row.addWidget(divider)
        self.household = HouseholdView()
        self.household.sims_changed.connect(self._sims_changed)
        self.household.exclusion_changed.connect(self._exclusion_changed)
        self.household.copies_chosen.connect(self._copies_chosen)
        self.household.export_requested.connect(self._export)
        self.household.export_item_requested.connect(
            lambda ref, name: self._export_item(ref, name, self._household_copies()))
        self.household.thumbnails_wanted.connect(self._household_thumbnails)
        self.packages = PackageView()
        self.packages.export_item_requested.connect(self._export_item)
        self.packages.package_requested.connect(self._show_package_path)
        self.packages.tray_item_requested.connect(self._show_tray_item)
        self.packages.thumbnails_wanted.connect(self._package_thumbnails)
        self.received = ReceivedView()
        self.received.closed.connect(self._close_received)
        self.right = QStackedWidget()
        for view in (self.household, self.packages, self.received):
            self.right.addWidget(view)
        self.right.setCurrentWidget(self.packages if self._kind is MODS else self.household)
        row.addWidget(self.right, 1)
        self.setCentralWidget(root)
        self._drop_overlay = DropOverlay("Drop to check this zip",
                                         "See if it works in your game. Nothing is installed or "
                                         "changed.", root)

        self._build_status_bar()
        self.index = IndexController(self.index_status, self)
        self.index.result_ready.connect(self._index_finished)
        self.index.stopped.connect(self._check_index_ready)
        self.index.stopped.connect(self._refresh_stopped)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._export)
        QShortcut(QKeySequence("Ctrl+Shift+E"), self, activated=self._export_several)
        QShortcut(QKeySequence("Ctrl+H"), self, activated=self._show_history)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self._show_settings)
        QShortcut(QKeySequence.StandardKey.Open, self, activated=self._choose_zip)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._leave_received)
        self._show_folders()
        self._restore_geometry()
        # Start with no control focused, so no focus ring shows until the keyboard is used.
        self._sidebar.setFocus()

    # Layout

    def _build_sidebar(self) -> QWidget:
        side = QWidget(objectName="sidebar")
        side.setFixedWidth(SIDEBAR_WIDTH)
        side.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        side.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._sidebar = side
        col = QVBoxLayout(side)
        col.setContentsMargins(0, SPACE["xl"], 0, 0)
        col.setSpacing(SPACE["m"])

        head = QVBoxLayout()
        head.setContentsMargins(SPACE["l"], 0, SPACE["l"], 0)
        head.setSpacing(SPACE["m"])
        head.addWidget(self._section_tabs())
        self.search = QLineEdit()
        self.search.setEnabled(False)
        self.search.textChanged.connect(self._search_changed)
        self._search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._clear_action = self.search.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self._clear_action.setToolTip("Clear search")
        self._clear_action.triggered.connect(self.search.clear)
        self._clear_action.setVisible(False)
        self._apply_icons()
        head.addWidget(self.search)
        self.kind_tabs = self._kind_tabs()
        self.kind_tabs.setVisible(self._kind is not MODS)
        head.addWidget(self.kind_tabs)
        self.tray_order = self._tray_sort_row()
        self.tray_order.setVisible(self._kind is not MODS)
        head.addWidget(self.tray_order)
        self.mods_sort = self._mods_sort_row()
        self.mods_sort.setVisible(self._kind is MODS)
        head.addWidget(self.mods_sort)
        col.addLayout(head)
        self._update_search_hint()

        self.sidebar_stack = QStackedWidget()
        self.trays = TrayList()
        self.trays.proxy.set_sort(self.tray_sort.current)
        self.trays.item_selected.connect(self._item_selected)
        self.trays.clicked.connect(self._leave_received)
        state = QWidget()
        state_col = QVBoxLayout(state)
        state_col.setContentsMargins(SPACE["l"], SPACE["s"], SPACE["l"], 0)
        state_col.setSpacing(SPACE["s"])
        self.tray_state_spinner = Spinner(24)
        self.tray_state_title = label("", None, wrap=True)
        self.tray_state_body = label("", "secondary", wrap=True)
        state_col.addWidget(self.tray_state_spinner)
        state_col.addWidget(self.tray_state_title)
        state_col.addWidget(self.tray_state_body)
        state_col.addStretch(1)
        self.mods_list = ModsList()
        self.mods_list.set_sort(self.settings.mods_sort)
        self.mods_list.set_show(self.settings.mods_show)
        self.mods_list.package_selected.connect(self._package_chosen)
        # Clicking the item that's already chosen also leaves a zip being checked.
        self.mods_list.clicked.connect(self._mods_clicked)
        self.sidebar_stack.addWidget(self.trays)
        self.sidebar_stack.addWidget(state)
        self.sidebar_stack.addWidget(self.mods_list)
        self._state_page = state
        col.addWidget(self.sidebar_stack, 1)

        line = QFrame(objectName="divider")
        line.setFixedHeight(1)
        col.addWidget(line)
        # The sidebar's tools, as tiles: an icon over a short name; the tooltip says more.
        foot = QHBoxLayout()
        foot.setContentsMargins(SPACE["s"], SPACE["xs"], SPACE["s"], SPACE["s"])
        foot.setSpacing(SPACE["xs"])
        self.batch_button = FooterAction(
            "layers", "Export several", "Export several at once, one zip each or together "
            "(Ctrl+Shift+E)")
        self.batch_button.clicked.connect(self._export_several)
        self.batch_button.setEnabled(False)
        check = FooterAction(
            "shield-check", "Check a zip", "Open a zip someone sent you and see if it works in "
            "your game, before installing it (Ctrl+O). You can also drop a zip on the window.")
        check.clicked.connect(self._choose_zip)
        history = FooterAction("history", "History",
                               "Earlier exports: where they are, and checking them again (Ctrl+H)")
        history.clicked.connect(self._show_history)
        for action in (self.batch_button, check, history):
            foot.addWidget(action, 1)
        col.addLayout(foot)
        return side

    def _section_tabs(self) -> QWidget:
        """"Tray" and "Mods" as the sidebar's heading: what the list below shows."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["l"])
        group = QButtonGroup(box)
        self._section_buttons: dict[str, QPushButton] = {}
        for name, tip in (("Tray", "Your saved households, lots and rooms"),
                          ("Mods", "Your Mods packages: what's in each, and what needs it")):
            b = QPushButton(name)
            b.setCheckable(True)
            b.setChecked((name == "Mods") == (self._kind is MODS))
            b.setProperty("kind", "titleTab")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setAccessibleName(f"Show {name}")
            b.clicked.connect(lambda _c=False, n=name: self._set_section(n))
            group.addButton(b)
            row.addWidget(b)
            self._section_buttons[name] = b
        row.addStretch(1)
        self.settings_button = QPushButton()
        self.settings_button.setProperty("kind", "icon")
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setToolTip("Settings (Ctrl+,)")
        self.settings_button.setAccessibleName("Settings")
        self.settings_button.clicked.connect(self._show_settings)
        row.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        return box

    def _set_section(self, name: str) -> None:
        if name == "Mods":
            self._set_kind(MODS)
        elif self._kind is MODS:
            self._set_kind(self._last_tray_kind)

    def _tray_sort_row(self) -> QWidget:
        """How the households, lots or rooms are ordered."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        kind = self._kind if self._kind is not MODS else KINDS[0]
        self.tray_sort = MenuButton("Sort", sorts_for(kind[1]), self.settings.tray_sort)
        self.tray_sort.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tray_sort.chosen.connect(self._set_tray_sort)
        row.addWidget(self.tray_sort)
        return box

    def _set_tray_sort(self, sort_by: str) -> None:
        self.trays.set_sort(sort_by)
        self.settings.tray_sort = sort_by  # saved with the window

    def _mods_sort_row(self) -> QWidget:
        """How the Mods packages are ordered, and which are listed."""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["s"])
        self.mods_order = MenuButton("Sort", MODS_SORTS, self.settings.mods_sort)
        self.mods_order.chosen.connect(self._set_mods_sort)
        self.mods_show = MenuButton("Show", MODS_SHOWS, self.settings.mods_show)
        self.mods_show.chosen.connect(self._set_mods_show)
        for b in (self.mods_order, self.mods_show):
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            row.addWidget(b)
        return box

    def _set_mods_sort(self, sort_by: str) -> None:
        self.mods_list.set_sort(sort_by)
        self.settings.mods_sort = sort_by  # saved with the window
        if self.mods_list.currentIndex().isValid():
            self.mods_list.scrollTo(self.mods_list.currentIndex())
        else:
            self.mods_list.scrollToTop()

    def _set_mods_show(self, show: str) -> None:
        self.mods_list.set_show(show)
        self.settings.mods_show = show
        self._mods_search_changed(self.search.text())

    def _kind_tabs(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        self._kind_buttons: dict[str, QPushButton] = {}
        tray_kinds = [k for k in KINDS if k is not MODS]
        for i, kind in enumerate(tray_kinds):
            b = QPushButton(kind[2])
            b.setCheckable(True)
            b.setChecked(kind is self._kind)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else
                          "last" if i == len(tray_kinds) - 1 else "middle")
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            b.setAccessibleName(f"Show {kind[3]}")
            b.clicked.connect(lambda _c=False, k=kind: self._set_kind(k))
            group.addButton(b)
            row.addWidget(b)
            self._kind_buttons[kind[0]] = b
        return box

    def _set_kind(self, kind) -> None:
        if kind is self._kind:
            return
        if self._kind is not MODS:
            self._last_tray_kind = self._kind
        self._kind = kind
        if kind is not MODS:
            self._kind_buttons[kind[0]].setChecked(True)
        self._section_buttons["Mods" if kind is MODS else "Tray"].setChecked(True)
        self.kind_tabs.setVisible(kind is not MODS)
        self.tray_order.setVisible(kind is not MODS)
        self.mods_sort.setVisible(kind is MODS)
        if kind is not MODS:
            self.tray_sort.set_options(sorts_for(kind[1]), self.settings.tray_sort)
            if self.tray_sort.current != self.trays.proxy.sort_by:  # "Most Sims" for a lot
                self.trays.set_sort(self.tray_sort.current)
        self.settings.tray_kind = kind[0]
        self._update_search_hint()
        self._refresh_overview()  # "Pick a lot on the left"
        self._show_right()
        if kind is MODS:
            self._enter_mods()
        self._search_changed(self.search.text())

    def _show_right(self) -> None:
        """The view for the sidebar's kind; a zip being checked stays until it's closed."""
        if self.right.currentWidget() is self.received:
            return
        self.right.setCurrentWidget(self.packages if self._kind is MODS else self.household)

    def _update_search_hint(self) -> None:
        hint = ("Search households and Sims" if self._kind[1] == tray.ITEM_HOUSEHOLD
                else f"Search {self._kind[3]}")
        self.search.setPlaceholderText(hint)
        self.search.setAccessibleName(hint)

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.user_status = FolderStatus(
            "Sims 4", self._choose_user_dir,
            "It holds your Mods and Tray folders, usually Documents/Electronic Arts/The Sims 4.")
        self.game_status = FolderStatus(
            "Game", self._choose_game_dir,
            "TrayTrim reads the game to tell its own items from CC and to know which packs the "
            "CC needs; without it every EA item looks missing. Choose the folder The Sims 4 is "
            "installed in.")
        self.index_status = IndexStatus(self._show_skipped, self._refresh_index)
        bar.addWidget(self.user_status)
        bar.addPermanentWidget(self.index_status)
        bar.addPermanentWidget(self.game_status)
        coffee = CoffeeButton(compact=True)
        coffee_box = QWidget()
        coffee_row = QHBoxLayout(coffee_box)
        coffee_row.setContentsMargins(SPACE["s"], 0, SPACE["l"], 0)
        coffee_row.addWidget(coffee)
        bar.addPermanentWidget(coffee_box)
        self.setStatusBar(bar)

    # Tray gallery

    def _show_folders(self) -> None:
        self.user_status.set_path(self.folders.user_dir)
        self.game_status.set_path(self.folders.game_dir)
        self._tray_gen += 1
        gen = self._tray_gen
        self.trays.source.set_items([])
        self._tray_ready = False
        self._mods_gen += 1  # Tray usage is worked out again
        self._update_search_enabled()
        self._select(None)
        tray_dir = self.folders.tray
        if tray_dir is None or not tray_dir.is_dir():
            self._set_tray_state(
                "Tray folder not found",
                "Choose your Sims 4 folder in the bar at the bottom. It's the folder that holds "
                "Mods and Tray, usually Documents/Electronic Arts/The Sims 4.")
            return
        self._set_tray_state("Reading your Tray folder", "", loading=True)
        run_in_background(lambda: tray.scan_tray(tray_dir),
                          lambda result: self._on_tray_scanned(gen, result),
                          lambda e: self._on_tray_failed(gen, tray_dir, e))

    def _on_tray_scanned(self, gen: int, result) -> None:
        if gen != self._tray_gen:
            return
        items, failed = result
        shown = {k[1] for k in KINDS}
        self.trays.source.set_items([i for i in items if i.type in shown])
        if failed:
            self.statusBar().showMessage(
                f"{len(failed)} tray items couldn't be read and are left out.", 10_000)
        self._tray_ready = True
        self._tray_stamp = _folder_stamp(self.folders.tray)
        self._refresh_overview()
        self._update_search_enabled()
        self._search_changed(self.search.text())
        self._load_gallery_images(gen, list(self.trays.source.items))

    def _load_gallery_images(self, gen: int, items: list) -> None:
        """Tray pictures and Sim portraits for gallery rows, off the UI thread."""
        run_in_background(lambda: load_tray_pictures(items, GALLERY_PICTURE_WIDTH),
                          lambda pictures: self._on_pictures(gen, pictures),
                          lambda _e: None)  # pictures are a nicety; icons stay
        user_dir = self.folders.user_dir
        households = [i for i in items if i.type == tray.ITEM_HOUSEHOLD]
        sim_ids = {s.sim_id for i in households for s in i.sims}
        if user_dir is not None and sim_ids:
            run_in_background(lambda: load_portraits(user_dir, sim_ids, PORTRAIT * 2),
                              lambda images: self._on_portraits(gen, images),
                              lambda _e: None)  # portraits are a nicety; initials stay

    def _on_portraits(self, gen: int, images) -> None:
        if gen == self._tray_gen:
            self.trays.source.set_portraits(images)

    def _on_pictures(self, gen: int, pictures) -> None:
        if gen == self._tray_gen:
            self.trays.source.set_pictures(pictures)

    def _on_tray_failed(self, gen: int, tray_dir: Path, error: Exception) -> None:
        if gen != self._tray_gen:
            return
        self._set_tray_state("Couldn't read the Tray folder",
                             f"{tray_dir}: {describe_error(error)}. If the game is running, close "
                             "it and reopen TrayTrim.")

    def _update_search_enabled(self) -> None:
        self.search.setEnabled(self._mods_listed == self._mods_gen if self._kind is MODS
                               else self._tray_ready)

    def _set_tray_state(self, title: str, body: str, loading: bool = False) -> None:
        """The Tray folder's state in place of the gallery (shown when a Tray tab is open),
        and in the overview."""
        self._tray_state = (title, body, loading)
        if self._kind is not MODS:
            self._show_state(title, body, loading)
        self._refresh_overview()

    def _show_state(self, title: str, body: str, loading: bool = False) -> None:
        self.tray_state_spinner.setVisible(loading)
        self.tray_state_title.setText(title)
        self.tray_state_body.setText(body)
        self.tray_state_body.setVisible(bool(body))
        self.sidebar_stack.setCurrentWidget(self._state_page)

    def _apply_icons(self) -> None:
        p = theme.current()
        self._search_icon.setIcon(icons.icon("search", p.text_tertiary, 16))
        self._clear_action.setIcon(icons.icon("clear", p.text_secondary, 16))
        if hasattr(self, "settings_button"):
            self.settings_button.setIcon(icons.icon("settings", p.text_secondary, 20))
        for name in ("tray_sort", "mods_order", "mods_show"):  # not built yet at the start
            button = getattr(self, name, None)
            if button is not None:
                button.refresh_theme()

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self._check_for_changes()
        if event.type() == QEvent.Type.PaletteChange:  # the theme changed
            self._apply_icons()
            self.household.refresh_theme()
            self.packages.refresh_theme()
            self.received.refresh_theme()
            self.mods_list.viewport().update()
            self.trays.viewport().update()
        super().changeEvent(event)

    def _search_changed(self, text: str) -> None:
        self._clear_action.setVisible(bool(text))
        self._update_search_enabled()
        if self._kind is MODS:
            self._mods_search_changed(text)
            return
        self.trays.set_filter(text, self._kind[1])
        self._update_batch_button()
        if not self._tray_ready:  # the Tray folder isn't read yet, or couldn't be
            self._show_state(*self._tray_state)
            return
        noun = self._kind[3]
        if self.trays.visible_count():
            self.sidebar_stack.setCurrentWidget(self.trays)
        elif not any(i.type == self._kind[1] for i in self.trays.source.items):
            self._show_state(f"No {noun} in your Tray folder",
                                 f"Save {'a household' if noun == 'households' else 'one'} to "
                                 "the library in the game, and it shows up here next time you "
                                 "open TrayTrim.")
        else:
            self._show_state(
                f"Nothing matches \u201c{text.strip()}\u201d",
                "Search looks at household names and the names of their Sims."
                if noun == "households" else f"Search looks at the names of your {noun}.")

    def _focus_search(self) -> None:
        if self.search.isEnabled():
            self.search.setFocus()
            self.search.selectAll()

    # Household analysis

    def _item_selected(self, item: tray.TrayItem | None) -> None:
        if item is not None and self.right.currentWidget() is self.received:
            self._close_received()  # picking something on the left means looking at it
        self._select(item)

    def _select(self, item: tray.TrayItem | None) -> None:
        self._selected = item
        self._analysis_gen += 1
        if item is None:
            self._show_overview()
            return
        self._analyse()

    def _show_overview(self) -> None:
        """Nothing picked: what TrayTrim found, and the ways to start."""
        items = self.trays.source.items
        stats = []
        if self._tray_ready:
            for kind in KINDS[:3]:
                n = sum(1 for i in items if i.type == kind[1])
                stats.append((f"{n:,}", kind[3] if n != 1 else kind[3][:-1]))
        result = self.index.last_result
        if result is not None:
            stats.append((f"{result.mods_packages:,}", "Mods packages"))
        kind = self._kind if self._kind is not MODS else self._last_tray_kind
        if self._tray_ready:
            title = f"Pick a {kind[0]} on the left"
            line = ("See the CC it uses, how small its export is, and what's missing or "
                    "needed besides, then export it with only that CC.")
        else:  # reading it, not found, or unreadable: what the sidebar says too
            title, line = self._tray_state[0] or "Reading your Tray folder", self._tray_state[1]
        can_batch = self._index_ready and bool(items)
        batch_text = ("One zip each, or several together in one zip (Ctrl+Shift+E)." if can_batch
                      else "Once the index of your Mods is ready." if not self._index_ready
                      else "Once your Tray has a household, lot or room in it.")
        actions = [
            ("layers", "Export several", batch_text, self._export_several, can_batch),
            ("shield-check", "Check a zip", "See whether a zip someone sent you works in your "
             "game, before installing it (Ctrl+O).", self._choose_zip, True),
            ("history", "Export history", "Find earlier exports and check them again (Ctrl+H).",
             self._show_history, True),
        ]
        self.household.show_overview(title, line, stats, actions,
                                     "You can also drop a zip anywhere on the window to check "
                                     "it. Nothing is installed or changed.")

    def _refresh_overview(self) -> None:
        if self._selected is None and self.household.showing_overview():
            self._show_overview()

    def _sims_changed(self, sim_ids: set[int]) -> None:
        if self._selected is None:
            return
        self._choice(self._selected.instance).sim_ids = sim_ids
        full = self._full_report
        if full is not None and full.item.instance == self._selected.instance:
            self._show_report(full)  # just a filter over what's already looked up

    def _analyse(self) -> None:
        item = self._selected
        if item is None:
            return
        if not self._index_ready:
            self.household.show_message(
                item.name or f"Unnamed {item.kind}",
                "Waiting for the index of your Mods and game packages. The CC list appears as "
                "soon as it's ready.", loading=True)
            return
        gen = self._analysis_gen
        # Only show "Reading..." if it takes long enough to notice; avoids a flash.
        QTimer.singleShot(250, lambda: self._show_reading(gen, item))

        def work() -> HouseholdReport:
            with ResourceIndex() as ix:
                located = locate_item(item, ix)
            return HouseholdReport(item, located.contents, located.refs, located.sim_ids,
                                   overrides=located.start_excluded)

        run_in_background(work, lambda r: self._on_report(gen, r),
                          lambda e: self._on_report_failed(gen, item, e))

    def _show_reading(self, gen: int, item: tray.TrayItem) -> None:
        if gen == self._analysis_gen and self._done_gen != gen:
            self.household.show_message(
                item.name or f"Unnamed {item.kind}",
                "Reading its Sims." if item.type == tray.ITEM_HOUSEHOLD
                else "Reading its objects and build items.", loading=True)

    def _on_report(self, gen: int, report: HouseholdReport) -> None:
        if gen != self._analysis_gen:
            return
        self._done_gen = gen
        self._full_report = report
        self._show_report(report)
        self._load_names(report)
        self._load_picture(report.item)

    def _household_thumbnails(self, instance: int, refs: list) -> None:
        """The household view shows cards that have no thumbnail yet (a page at a time)."""
        tried = {r.ref.instance for r in refs}
        self._load_thumbnails(refs, lambda images: self.household.set_thumbnails(
            instance, images, tried))

    def _load_thumbnails(self, refs, sink, index_path: Path | None = None, on_end=None) -> None:
        """Thumbnails of CC items, read off the UI thread, then sink({instance: image}). The
        sink is always called once, with nothing if there's nothing to show; on_end after it.
        index_path: a received zip's own index instead of the user's."""
        wanted: dict[int, set[int]] = {}  # thumbnail type -> instances
        for r in refs:
            thumb_type = THUMB_TYPES.get(r.ref.kind)
            if thumb_type:
                wanted.setdefault(thumb_type, set()).add(r.ref.instance)
        user_dir = self.folders.user_dir

        def work():
            images = {}
            if wanted:
                with ResourceIndex(index_path) as ix:
                    for thumb_type, instances in wanted.items():
                        images.update(load_thumbnails(ix, sorted(instances), thumb_type,
                                                      THUMB * 2, user_dir))
            return images

        def finish(images) -> None:
            sink(images)
            if on_end is not None:
                on_end()

        run_in_background(work, finish, lambda _e: finish({}))

    def _load_picture(self, item: tray.TrayItem) -> None:
        if self.household.has_picture(item.instance):
            return
        run_in_background(lambda: load_tray_pictures([item], PICTURE_WIDTH),
                          lambda pictures: self.household.set_picture(
                              item.instance, pictures.get(item.instance)),
                          lambda _e: None)  # the header just has no picture

    def _load_names(self, report: HouseholdReport) -> None:
        """Item names come from each CC package's string tables; read them off the UI thread
        and show them when they're there (big lots take a few seconds)."""
        if self._names_cancel is not None:
            self._names_cancel.set()  # the previous item's names aren't wanted any more
        # Everything not read to the end yet, including what a cancelled read didn't reach.
        refs = [r for r in report.refs if r.where is Where.CC and r.ref not in self._names_read]
        if not refs:
            return
        cancel = self._names_cancel = threading.Event()
        instance = report.item.instance

        def done(names) -> None:
            # A read that ran to the end covered every ref (packages without names too); a
            # cancelled one only what it found.
            self._names_read.update(names if cancel.is_set() else (r.ref for r in refs))
            self.household.set_names(instance, names)

        run_in_background(lambda: item_names(refs, cancel), done,
                          lambda _e: None)  # names are a nicety; package names stay

    def _choice(self, instance: int) -> Choice:
        return self._choices.setdefault(instance, Choice())

    def _show_report(self, full: HouseholdReport) -> None:
        all_ids = {s.sim.sim_id for s in full.contents.sims}
        choice = self._choice(full.item.instance)
        if choice.excluded is None:
            choice.excluded = set(full.overrides)
        chosen = pick_sims(choice.sim_ids, all_ids)
        refs = scoped_refs(full.refs, all_ids, chosen)
        self.household.show_report(HouseholdReport(full.item, full.contents, refs, chosen,
                                                   choice.excluded, full.overrides,
                                                   choice.copies))
        self._start_plan()

    # Export plan (sizes, conflicts, missing files)

    def _exclusion_changed(self, excluded: set) -> None:
        if self._selected is None:
            return
        self._choice(self._selected.instance).excluded = excluded
        self._replan()

    def _copies_chosen(self, picks: dict) -> None:
        """The user picked which copies go into the export ({key: package path}, or None for
        the default, the copy the game loads)."""
        if self._selected is None:
            return
        copies = self._choice(self._selected.instance).copies
        for key, path in picks.items():
            if path is None:
                copies.pop(key, None)
            else:
                copies[key] = path
        self.household.plan_outdated()
        self._replan()

    def _replan(self) -> None:
        # A plan still running was built for the old choice: drop it now, not when it lands.
        self._plan_gen += 1
        if self._plan_task is not None and self._plan_task.running:
            self._plan_task.cancel()
        self._plan_timer.start()  # wait for a burst of clicks to settle

    def _start_plan(self) -> None:
        self._plan_timer.stop()
        if self._plan_task is not None and self._plan_task.running:
            self._plan_task.cancel()
        report = self.household._report
        if report is None:
            return
        self._plan_gen += 1
        gen, instance = self._plan_gen, report.item.instance
        refs, excluded, copies = report.refs, set(report.excluded), dict(report.copies)
        contents, sim_ids = report.contents, set(report.sim_ids)

        def work(task: BackgroundTask):
            with ResourceIndex() as ix:
                return plan_refs(contents, refs, sim_ids, excluded, copies, ix,
                                 lambda done, total: task.report(done, total), task.cancelled)

        task = BackgroundTask(work)
        task.progress.connect(
            lambda args: gen == self._plan_gen
            and self.household.set_plan_progress(instance, *args))
        task.finished.connect(
            lambda result: gen == self._plan_gen
            and self.household.set_plan(instance, *result))
        task.failed.connect(lambda e: self._on_plan_failed(gen, instance, e))
        self._plan_task = task
        task.start()

    def _export(self) -> None:
        if self.right.currentWidget() is not self.household:
            return  # Ctrl+E exports the household on screen, not one hidden behind a tab
        state = self.household.export_state()
        if state is None:
            return
        report, plan, gameplay = state
        request = ExportRequest(report.item, report.contents, report.refs, plan, gameplay,
                                set(report.sim_ids),
                                default_base_name(report.item, report.contents,
                                                  report.sim_ids),
                                self.folders.user_dir, self.folders.game_dir)
        self._run_dialog(ExportDialog(request, self.settings, self))

    def _update_batch_button(self) -> None:
        ready = self._index_ready and bool(self.trays.source.items)
        self.batch_button.setEnabled(ready)
        self.batch_button.setToolTip(
            "Export several at once, one zip each or together (Ctrl+Shift+E)" if ready
            else "Export several at once, once the index of your Mods and game is ready and "
            "your Tray has something in it.")

    def _export_several(self) -> None:
        items = list(self.trays.source.items)
        if not items:
            return
        if not self._index_ready:
            self.statusBar().showMessage("Exports can start once the index of your Mods and "
                                         "game packages is ready.", 10_000)
            return
        choices = {i.instance: self._choices[i.instance] for i in items
                   if i.instance in self._choices}
        selected = self._selected.instance if self._selected is not None else None
        self._run_dialog(BatchDialog(items, choices, self.settings, self.folders.user_dir,
                                     self.folders.game_dir, selected, self))

    def _show_history(self) -> None:
        self._run_dialog(HistoryDialog(self._open_zip, self))

    def _show_settings(self) -> None:
        dialog = SettingsDialog(
            self.settings, lambda: (self.folders.user_dir, self.folders.game_dir),
            self._choose_user_dir, self._choose_game_dir, self._refresh_index, self)
        # The index's read ends (done, failed or stopped): the dialog's Read again comes back.
        self.index.result_ready.connect(dialog.refresh_finished)
        self.index.stopped.connect(dialog.refresh_finished)
        try:
            self._run_dialog(dialog)
        finally:
            self.index.result_ready.disconnect(dialog.refresh_finished)
            self.index.stopped.disconnect(dialog.refresh_finished)

    @staticmethod
    def _run_dialog(dialog) -> None:
        """Show a dialog and free it afterwards (a parented dialog otherwise lives on)."""
        dialog.exec()
        dialog.deleteLater()

    def _on_plan_failed(self, gen: int, instance: int, error: Exception) -> None:
        if gen == self._plan_gen:
            self.household.show_plan_error(instance, describe_error(error), self._start_plan)

    def _on_report_failed(self, gen: int, item: tray.TrayItem, error: Exception) -> None:
        if gen == self._analysis_gen:
            self._done_gen = gen
            self.household.show_message(f"Couldn't read {item.name or 'this ' + item.kind}",
                                        describe_error(error))

    def _index_finished(self, result) -> None:
        QTimer.singleShot(0, self._refresh_overview)  # its Mods count, once this is done
        tray_changed, self._tray_dirty = self._tray_dirty, False
        if self._index_ready and not result.changed:
            if tray_changed:  # only the Tray: what uses what is worked out again
                self._index_changed()
            return  # otherwise everything on screen is still right
        self._index_ready = True
        # Packages may have changed: names are read again as items are shown.
        self._names_read.clear()
        self.household.forget_names()
        self._update_batch_button()
        self._analysis_gen += 1
        self._analyse()
        self._index_changed()

    def _refresh_stopped(self) -> None:
        """A check for changes didn't finish: a Tray change it was waiting for still counts."""
        if self._tray_dirty and self._index_ready:
            self._tray_dirty = False
            self._index_changed()

    def _check_index_ready(self) -> None:
        """Use the cached index right away if it's complete for these folders, even when the
        refresh failed, was cancelled or hasn't finished yet."""
        if self._index_ready:
            return
        mods, game = self.folders.mods, self.folders.game_dir
        tray_gen = self._tray_gen

        def work() -> bool:
            with ResourceIndex() as ix:
                return ix.is_complete(mods, game)

        def done(complete: bool) -> None:
            if complete and tray_gen == self._tray_gen and not self._index_ready:
                self._index_ready = True
                self._update_batch_button()
                self._refresh_overview()
                self._analysis_gen += 1
                self._analyse()
                self._index_changed()
            elif self._pending_zip is not None and not self.index.running:
                # The index couldn't be built: check the zip with what there is rather than
                # wait for it forever (it's still checked file by file).
                path, self._pending_zip = self._pending_zip, None
                self._open_zip(path)

        run_in_background(work, done, lambda _e: None)

    # Mods tab

    def _index_changed(self) -> None:
        """The index is ready or was refreshed: the Mods tab's list and what needs what are
        worked out again (when the tab is open), and a zip waiting for the index is checked."""
        self._mods_gen += 1
        if self._usage_task is not None and self._usage_task.running:
            self._usage_task.cancel()  # for the old state; it starts over once it stops
        if self._kind is MODS:
            self._enter_mods()
        if self._pending_zip is not None:
            path, self._pending_zip = self._pending_zip, None
            self._open_zip(path)

    def _enter_mods(self) -> None:
        if not self._index_ready:
            self._mods_search_changed(self.search.text())
            return
        if self._mods_listed != self._mods_gen:
            self._load_mods_list()
        self._start_usage()

    def _load_mods_list(self) -> None:
        gen = self._mods_gen

        def work():
            with ResourceIndex() as ix:
                return ix.mods_packages(ITEM_TYPES)

        def done(packages) -> None:
            if gen != self._mods_gen:
                return
            self._mods_listed = gen
            self.mods_list.set_packages(packages)
            if self._kind is MODS:
                self._search_changed(self.search.text())
            if self._package is not None:  # it may have changed: read it again
                current = next((p for p in packages if p.path == self._package.path), None)
                if current is None:  # deleted (likely just now, after looking at it here)
                    gone = PurePosixPath(self._package.rel).stem
                    self._package = None
                    self.mods_list.forget_choice()  # if it comes back, it isn't chosen silently
                    self._package_gen += 1
                    self.packages.report = None
                    self.packages.show_message(f"{gone} is gone",
                                               "It isn't in your Mods folder any more.")
                else:
                    self._package_selected(current)

        self._mods_search_changed(self.search.text())
        run_in_background(work, done, lambda e: self._mods_list_failed(gen, e))

    def _mods_list_failed(self, gen: int, error: Exception) -> None:
        if gen == self._mods_gen and self._kind is MODS:
            self._show_state("Couldn't list your Mods folder", describe_error(error))

    def _mods_search_changed(self, text: str) -> None:
        self.mods_list.set_filter(text)
        mods = self.folders.mods
        if mods is None or not mods.is_dir():
            self._show_state("Mods folder not found",
                             "Choose your Sims 4 folder in the bar at the bottom. It's the folder "
                             "that holds Mods and Tray, usually Documents/Electronic Arts/The "
                             "Sims 4.")
        elif not self._index_ready:
            self._show_state("Waiting for the index",
                             "TrayTrim is reading your Mods folder and the game. The packages "
                             "show up here when it's done.", loading=True)
        elif self._mods_listed != self._mods_gen and not self.mods_list.source.packages:
            self._show_state("Reading your Mods folder", "", loading=True)
        elif not self.mods_list.source.packages:
            self._show_state("No packages in your Mods folder",
                             "Packages the game loads from Mods show up here.")
        elif self.mods_list.visible_count():
            self.sidebar_stack.setCurrentWidget(self.mods_list)
        elif not text.strip():  # only the Show choice hides them
            self._show_state("No packages like that",
                             f"None of your packages is listed under \u201c"
                             f"{next(o[1] for o in MODS_SHOWS if o[0] == self.mods_show.current)}"
                             "\u201d. Choose All in Show to list them all.")
        else:
            self._show_state(f"Nothing matches \u201c{text.strip()}\u201d",
                             "Search looks at package names and their folders in Mods."
                             + ("" if self.mods_show.current == "all" else
                                " Only the packages Show picks are searched."))

    def _start_usage(self) -> None:
        """Work out what needs what (references between packages, and which tray items use
        what) once per index and Tray, in the background."""
        if (not self._index_ready or self._usage_gen == self._mods_gen
                or (self._usage_task is not None and self._usage_task.running)):
            return
        gen = self._mods_gen
        tray_dir, mods_dir = self.folders.tray, self.folders.mods
        self._usage_tray_stamp = _folder_stamp(tray_dir)
        shown = {k[1] for k in KINDS if k[1] is not None}

        def work(task: BackgroundTask):
            with ResourceIndex() as ix, DependencyIndex() as deps:
                deps.update(ix, lambda phase, done, total: task.report(phase, done, total),
                            task.cancelled)
                # The Tray as it is now (the gallery shows it as it was at start).
                try:
                    items, failed = tray.scan_tray(tray_dir) if tray_dir else (None, [])
                except OSError:
                    items, failed = None, []
                if items is None:
                    usage = TrayUsage(tray_read=False)
                else:
                    usage = scan_usage([i for i in items if i.type in shown], ix,
                                       lambda done, total: task.report("tray", done, total),
                                       task.cancelled)
                    usage.unreadable += [path.name for path, _reason in failed]
                # Packages the index couldn't read hold references nobody knows about.
                broken = list(deps.unreadable) + [
                    Path(p) for p, _e in ix.failed_packages()
                    if mods_dir is not None and Path(p).is_relative_to(mods_dir)]
                return usage, broken

        task = BackgroundTask(work)
        task.progress.connect(lambda args: gen == self._mods_gen and self._usage_progress(*args))
        task.finished.connect(lambda result: self._usage_finished(gen, *result, ""))
        task.failed.connect(lambda e: self._usage_finished(gen, None, [], describe_error(e)))
        task.aborted.connect(lambda: self._usage_aborted(task))
        self._usage_task = task
        task.start()

    def _usage_aborted(self, task: BackgroundTask) -> None:
        """Cancelled because the index or the Tray changed: start over for the new state."""
        if self._usage_task is task:
            self._usage_task = None
        if self._kind is MODS:
            self._start_usage()

    def _usage_progress(self, phase: str, done: int, total: int) -> None:
        line = {"read": f"Reading how your CC fits together: {done:,} of {total:,} packages. "
                        "Only the first time takes a while; after that, only packages that "
                        "changed are read.",
                "match": f"Matching objects with meshes in other packages: {done:,} of "
                         f"{total:,} packages.",
                "tray": f"Reading your Tray: {done:,} of {total:,} items."}[phase]
        self.packages.show_progress("Working out what uses it", line, done, total)

    def _usage_finished(self, gen: int, usage: TrayUsage | None, unreadable: list,
                        error: str) -> None:
        self._usage_task = None
        if gen != self._mods_gen:
            if self._kind is MODS:
                self._start_usage()  # the index or the Tray changed meanwhile
            return
        # A failure isn't remembered as done: picking a package tries again.
        self._usage_gen = gen if usage is not None else -1
        self._usage, self._usage_error, self._unreadable = usage, error, unreadable
        if self._package is None:
            self.packages.clear_progress()
        if self._package is not None and self.packages.report is not None:
            self._load_package_usage()

    def _leave_received(self, *_args) -> None:
        if self.right.currentWidget() is self.received:
            self._close_received()

    def _mods_clicked(self, index) -> None:
        """A click on the package already chosen: back from a zip, or a retry when it isn't
        shown (its report couldn't be read, it was gone, or what needs it failed)."""
        self._leave_received()
        package = index.data(PackageRole) if index.isValid() else None
        if package is not None and (self.packages.report is None or self.packages.failed):
            self._package_selected(package)

    def _package_chosen(self, package) -> None:
        if package is not None and self.right.currentWidget() is self.received:
            self._close_received()
        self._package_selected(package)

    def _package_selected(self, package) -> None:
        self._package = package
        self._package_gen += 1
        if package is None:
            self.packages.show_idle()
            return
        gen = self._package_gen
        path = package.path
        title = PurePosixPath(package.rel).stem
        # Only say "Reading" if it takes long enough to notice (a big merge); avoids a flash.
        QTimer.singleShot(250, lambda: gen == self._package_gen
                          and self._package_loading == gen and self.packages.show_loading(title))
        self._package_loading = gen

        def work():
            with ResourceIndex() as ix:
                return package_report(path, ix)

        def done(report) -> None:
            if gen != self._package_gen:
                return
            self._package_loading = -1
            self.packages.show_report(report)
            self._load_package_extras(report)
            if self._usage_gen == self._mods_gen:
                self._load_package_usage()
            elif self._usage is None and self._usage_error:
                self._usage_error = ""
                self._start_usage()  # try again; the page shows its progress

        def failed(error: Exception) -> None:
            if gen == self._package_gen:
                self._package_loading = -1
                self.packages.report = None
                self.packages.show_message(f"Couldn't read {package.rel}",
                                           f"{describe_error(error)}. If the index is being "
                                           "updated, wait for it to finish.")

        run_in_background(work, done, failed)

    def _load_package_extras(self, report) -> None:
        """Names of the items in a package, read off the UI thread (thumbnails are read as
        their cards are shown, see _package_thumbnails)."""
        refs, path = report.items, report.path
        if not refs:
            return
        run_in_background(lambda: item_names(refs),
                          lambda names: self.packages.set_names(path, names), lambda _e: None)

    def _package_thumbnails(self, path: Path, refs: list) -> None:
        tried = {r.ref.instance for r in refs}
        self._load_thumbnails(refs, lambda images: self.packages.set_thumbnails(path, images,
                                                                                tried))

    def _load_package_usage(self) -> None:
        report, usage, gen = self.packages.report, self._usage, self._package_gen
        if usage is None:  # working it out failed: say so, claim nothing about what's needed
            self.packages.set_usage_failed(report.path, self._usage_error)
            return
        unreadable = list(self._unreadable)

        def work():
            with ResourceIndex() as ix, DependencyIndex() as deps:
                via = texture_needs(report, deps, ix)  # used by both
                return users(report, deps, usage, ix, via), dependents(report, deps, ix, via)

        def done(result) -> None:
            if gen == self._package_gen:
                found, broken = result
                self.packages.set_usage(report.path, found, broken, unreadable, usage.tray_read,
                                        usage.unreadable)

        def failed(error: Exception) -> None:
            if gen == self._package_gen:
                self.packages.set_usage_failed(report.path, describe_error(error))

        run_in_background(work, done, failed)

    def _show_package_path(self, path: Path) -> None:
        """Open another package (a "needs files from it" link)."""
        if not self.mods_list.select_path(path):  # hidden by the search or by Show
            self.search.clear()
            if not self.mods_list.select_path(path) and self.mods_show.current != "all":
                self.mods_show.set_options(MODS_SHOWS, "all")
                self._set_mods_show("all")
                self.mods_list.select_path(path)

    def _show_tray_item(self, item: tray.TrayItem) -> None:
        """Open a tray item (a "Used by" link): its tab, with the search cleared."""
        kind = next((k for k in KINDS if k[1] == item.type), None)
        if kind is None:
            return
        self.search.clear()
        self._set_kind(kind)
        if not self.trays.select_item(item):  # saved since the gallery was read
            self._refresh_tray(lambda: self.trays.select_item(item))

    def _refresh_tray(self, then=None) -> None:
        """Read the Tray again (it changed while TrayTrim was open), keeping what's chosen."""
        tray_dir, gen = self.folders.tray, self._tray_gen
        if tray_dir is None or not self._tray_ready:
            return

        def done(result) -> None:
            if gen != self._tray_gen:
                return
            items, _failed = result
            shown = {k[1] for k in KINDS}
            known = {i.instance for i in self.trays.source.items}
            fresh = [i for i in items if i.type in shown]
            if not self.trays.replace_items(fresh):
                self._select(None)  # the chosen item is gone from the Tray
            elif self._selected is not None:
                now = next((i for i in fresh if i.instance == self._selected.instance), None)
                if now is not None and (now.modified, now.files) != (self._selected.modified,
                                                                      self._selected.files):
                    self._select(now)  # re-saved in the game: read it again, choices kept
            self._search_changed(self.search.text())
            added = [i for i in fresh if i.instance not in known]
            if added:
                self._load_gallery_images(gen, added)
            if then is not None:
                then()

        run_in_background(lambda: tray.scan_tray(tray_dir), done, lambda _e: None)

    # Exporting one CC item

    def _household_copies(self) -> dict:
        """The copies the user chose for the household on screen ({key: package})."""
        return dict(self._choice(self._selected.instance).copies) if self._selected else {}

    def _export_item(self, ref, name, copies: dict | None = None) -> None:
        if not self._index_ready or self._item_planning:
            return  # one at a time: a second click mustn't open a second dialog
        self._item_planning = True
        title = display_name(ref, name) or "CC item"
        self.statusBar().showMessage(f"Working out what {title} needs\u2026")

        def work():
            with ResourceIndex() as ix:
                return build_plan([ref], ix, copies_chosen=copies)

        def done(plan) -> None:
            self._item_planning = False
            self.statusBar().clearMessage()
            if not plan.resources:
                self.statusBar().showMessage(f"{title} has nothing to export: its files are "
                                             "the game's own.", 10_000)
                return
            request = ExportRequest(None, None, [ref], plan, [], set(), safe_name(title, "Item"),
                                    self.folders.user_dir, self.folders.game_dir,
                                    item_name=title)
            self._run_dialog(ExportDialog(request, self.settings, self))

        def failed(error: Exception) -> None:
            self._item_planning = False
            self.statusBar().showMessage(f"Couldn't work out what {title} needs: "
                                         f"{describe_error(error)}.", 10_000)

        run_in_background(work, done, failed)

    # Check a zip

    def _choose_zip(self) -> None:
        downloads = Path.home() / "Downloads"  # where a zip someone sent usually is
        start = downloads if downloads.is_dir() else Path.home()
        chosen, _ = QFileDialog.getOpenFileName(self, "Check a zip", str(start),
                                                "Zip files (*.zip)")
        if chosen:
            self._open_zip(Path(chosen))

    def _open_zip(self, path: Path) -> None:
        if self._received_task is not None and self._received_task.running:
            self._received_task.cancel()
        self._drop_received()
        self.right.setCurrentWidget(self.received)
        self.received.user_dir = self.folders.user_dir
        if not self._index_ready and self.index.running:  # it's being built: wait for it
            self._pending_zip = path
            self.received.show_working(path)
            self.received.verdict.show_working(
                "Checking the zip", "Waiting for the index of your Mods and game packages.")
            return
        self.received.show_working(path)

        def work(task: BackgroundTask) -> Received:
            with ResourceIndex() as ix:
                return check_received(path, ix, task.report, task.cancelled)

        # A check that finished despite a late cancel still hands its result over, so its
        # temporary folder is deleted (_received_finished closes what's no longer wanted).
        task = BackgroundTask(work, keep_late_result=True)
        task.progress.connect(lambda args: self._received_task is task
                              and self.received.show_working(path, *args))
        task.finished.connect(lambda r: self._received_finished(task, r))
        task.failed.connect(lambda e: self._received_task is task
                            and self.received.show_error(path, describe_error(e)))
        self._received_task = task
        task.start()

    def _received_finished(self, task: BackgroundTask, r: Received) -> None:
        if self._received_task is not task or self.right.currentWidget() is not self.received:
            r.close()  # closed or replaced meanwhile
            return
        self._received_task = None
        self._received = r
        self.received.show_received(r)
        if not r.cc:
            return
        # Both read the zip's temporary folder: it's deleted only once they're done. A zip with
        # more items than cards are shown for gets a list, without pictures.
        refs = r.cc
        pictures = len(refs) <= MAX_CARDS
        r.jobs += 2 if pictures else 1

        def named(names) -> None:
            self.received.set_names(r, names)
            r.job_done()

        run_in_background(lambda: item_names(refs), named, lambda _e: r.job_done())
        if pictures:
            self._load_thumbnails(refs,
                                  lambda images: self.received.set_thumbnails(r, images, True),
                                  r.index_path, r.job_done)

    def _drop_received(self, now: bool = False) -> None:
        if self._received is not None:
            self._received.close(now)
            self._received = None
        self._pending_zip = None

    def _close_received(self) -> None:
        if self._received_task is not None and self._received_task.running:
            self._received_task.cancel()
        self._received_task = None
        self._drop_received()
        self.received.received = None
        self.right.setCurrentWidget(self.packages if self._kind is MODS else self.household)

    @staticmethod
    def _dropped_zip(event) -> Path | None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].isLocalFile():
            path = Path(urls[0].toLocalFile())
            if path.suffix.lower() == ".zip":
                return path
        return None

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._dropped_zip(event) is not None:
            event.acceptProposedAction()
            self._drop_overlay.show_over_parent()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._drop_overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._drop_overlay.hide()
        path = self._dropped_zip(event)
        if path is not None:
            event.acceptProposedAction()
            self._open_zip(path)

    # Folders

    def _choose_dir(self, title: str, start: Path | None, valid: Callable[[Path], bool],
                    rejected: str) -> Path | None:
        """Ask for a folder; returns it if it passes `valid`, else explains in the status bar."""
        chosen = QFileDialog.getExistingDirectory(self, title, str(start or Path.home()))
        if not chosen:
            return None
        path = Path(chosen)
        if not valid(path):
            self.statusBar().showMessage(rejected.format(folder=path), 10_000)
            return None
        return path

    def _choose_user_dir(self) -> None:
        path = self._choose_dir(
            "Choose your Sims 4 folder", self.folders.user_dir, paths.looks_like_user_dir,
            "{folder} has neither a Mods nor a Tray folder. Choose the folder that contains them.")
        if path is not None:
            self.settings.user_dir = str(path)
            self.folders = paths.Sims4Folders(path, self.folders.game_dir)
            self._folders_changed()

    def _choose_game_dir(self) -> None:
        path = self._choose_dir(
            "Choose the folder The Sims 4 is installed in", self.folders.game_dir,
            paths.looks_like_game_dir,
            "{folder} doesn't contain the game's Data/Client folder. Choose the folder The Sims 4 "
            "is installed in.")
        if path is not None:
            self.settings.game_dir = str(path)
            self.folders = paths.Sims4Folders(self.folders.user_dir, path)
            self._folders_changed()

    def _refresh_index(self) -> None:
        self._last_check = time.monotonic()
        self.index.refresh(self.folders.mods, self.folders.game_dir)

    def _check_for_changes(self) -> None:
        """Coming back to the window: the user may have deleted or added CC meanwhile (the
        Mods tab is for deciding that), or saved something to the Tray in the game. Listing
        the folders takes a moment; only packages that changed are read, and the views are
        only updated if something did."""
        if not self._index_ready or time.monotonic() - self._last_check < 5:
            return
        self._last_check = time.monotonic()
        # The Tray is read again on its own; it doesn't wait for the index.
        stamp = _folder_stamp(self.folders.tray)
        if self._tray_ready and stamp != self._tray_stamp:  # saved in the game meanwhile
            self._tray_stamp = stamp
            self._refresh_tray()
        if self._usage_gen == self._mods_gen and stamp != self._usage_tray_stamp:
            self._tray_dirty = True  # worked out again once the Mods check is done
        if not self.index.running:  # one still running finishes the job (and the Tray's)
            # Mods only: the game install doesn't change while TrayTrim is open (a patch needs
            # the game closed and is picked up at the next start), and listing it takes longest.
            self.index.refresh(self.folders.mods, None, quiet=True,
                               game_root=self.folders.game_dir)

    def _show_skipped(self) -> None:
        if self.index.last_result is not None:
            SkippedPackagesDialog(self.index.last_result, self).exec()

    def _folders_changed(self) -> None:
        self._index_ready = False
        self._update_batch_button()
        self._show_folders()
        self._check_index_ready()
        self._refresh_index()
        try:
            self.settings.save()
        except OSError as e:
            self.statusBar().showMessage(
                f"Couldn't save settings ({e.strerror or e}). The folder is used for now but "
                "won't be remembered next time.", 10_000)

    # Window state

    def _restore_geometry(self) -> None:
        geometry = self.settings.window_geometry
        if not geometry or not self.restoreGeometry(QByteArray.fromBase64(geometry.encode())):
            self.resize(1200, 760)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_index_started", False):
            self._index_started = True
            self._check_index_ready()
            QTimer.singleShot(0, self._refresh_index)  # after the first paint
            if self._check_updates:
                QTimer.singleShot(3000, self._check_for_update)

    def _check_for_update(self) -> None:
        """A newer release on GitHub: offer it (once per start, unless skipped)."""
        self._update_check = UpdateCheck(self)
        self._update_check.found.connect(self._offer_update)
        self._update_check.start(self.settings.skipped_version)

    def _offer_update(self, release) -> None:
        if not self.isVisible():
            return
        # Not in the middle of something: another dialog (an export), a file picker (native,
        # so not a QDialog, but the window isn't active then), or another app in front.
        if not self.isActiveWindow() or any(isinstance(w, QDialog) and w.isVisible()
                                            for w in QApplication.topLevelWidgets()):
            QTimer.singleShot(10_000, self, lambda: self._offer_update(release))
            return
        self._run_dialog(UpdateDialog(release, self.settings, self))

    def stop_work(self) -> None:
        """Ask the background work to stop: the index refresh stops at its next check (SQLite
        rolls back anything unfinished, so the next start resumes), plans and name reading
        stop. The app waits for the threads before it exits (tasks.shutdown)."""
        self.index.stop()
        for task in (self._plan_task, self._usage_task, self._received_task):
            if task is not None:
                task.cancel()
        if self._names_cancel is not None:
            self._names_cancel.set()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_work()
        self._drop_received(now=True)
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        try:
            self.settings.save()
        except OSError:
            pass  # losing the window size isn't worth blocking the user from closing the app
        super().closeEvent(event)
