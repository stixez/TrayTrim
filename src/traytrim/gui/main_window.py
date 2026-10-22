"""The single main window: tray gallery on the left, the selected household on the right."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLineEdit, QMainWindow,
                               QStackedWidget, QStatusBar, QVBoxLayout, QWidget)

from traytrim import paths, tray
from traytrim.gui.gallery import PORTRAIT, TrayList
from traytrim.gui.household_view import THUMB, HouseholdReport, HouseholdView
from traytrim.gui.images import load_cas_thumbnails, load_portraits
from traytrim.gui.index_status import IndexController, IndexStatus, SkippedPackagesDialog
from traytrim.gui import icons, theme
from traytrim.gui.tasks import BackgroundTask, describe_error, run_in_background
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import ElidedLabel, label, link_button
from traytrim.index import ResourceIndex
from traytrim.export import default_base_name
from traytrim.gui.export_dialog import ExportDialog, ExportRequest
from traytrim.plan import build_plan
from traytrim.resolve import Where, for_sims, gameplay_mods, locate
from traytrim.settings import Settings

SIDEBAR_WIDTH = 320


class FolderStatus(QWidget):
    """One status-bar slot: "Mods  C:\\...\\Mods", or "Mods folder not found" with a way to fix it."""

    def __init__(self, name: str, on_choose: Callable[[], None], parent=None):
        super().__init__(parent)
        self._name = name
        lay = QHBoxLayout(self)
        lay.setContentsMargins(SPACE["s"], 0, SPACE["s"], 0)
        lay.setSpacing(SPACE["s"])
        self._title = label(name, "caption")
        self._value = ElidedLabel("", "caption", max_width=320)
        self._choose = link_button("Choose folder")
        self._choose.clicked.connect(on_choose)
        for w in (self._title, self._value, self._choose):
            lay.addWidget(w)

    def set_path(self, path: Path | None) -> None:
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
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.folders = paths.resolve_folders(settings.user_dir, settings.game_dir)
        # Each background job carries the generation it was started in; results from an older
        # generation (folder changed, another household picked) are dropped.
        self._tray_gen = 0
        self._analysis_gen = 0
        self._done_gen = -1  # the last analysis generation that produced a report or an error
        self._index_ready = False
        self._full_report: HouseholdReport | None = None  # all Sims, before the Sim filter
        self._selected: tray.TrayItem | None = None
        self._sim_choice: dict[int, set[int]] = {}  # household -> Sims to include
        self._excluded: dict[int, set] = {}  # household -> TrayRefs left out of the export
        self._plan_task: BackgroundTask | None = None
        self._plan_gen = 0
        self._plan_timer = QTimer(self, singleShot=True, interval=300)
        self._plan_timer.timeout.connect(self._start_plan)
        self.setWindowTitle("TrayTrim")
        self.setMinimumSize(960, 600)

        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_sidebar())
        divider = QFrame(objectName="sidebarDivider")
        divider.setFixedWidth(1)
        row.addWidget(divider)
        self.household = HouseholdView()
        self.household.sims_changed.connect(self._sims_changed)
        self.household.exclusion_changed.connect(self._exclusion_changed)
        self.household.export_requested.connect(self._export)
        row.addWidget(self.household, 1)
        self.setCentralWidget(root)

        self._build_status_bar()
        self.index = IndexController(self.index_status, self)
        self.index.result_ready.connect(self._index_finished)
        self.index.stopped.connect(self._check_index_ready)
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+E"), self, activated=self._export)
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
        head.addWidget(label("Tray", "title"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search households and Sims")
        self.search.setAccessibleName("Search households and Sims")
        self.search.setEnabled(False)
        self.search.textChanged.connect(self._search_changed)
        self._search_icon = self.search.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
        self._clear_action = self.search.addAction(QIcon(), QLineEdit.ActionPosition.TrailingPosition)
        self._clear_action.setToolTip("Clear search")
        self._clear_action.triggered.connect(self.search.clear)
        self._clear_action.setVisible(False)
        self._apply_icons()
        head.addWidget(self.search)
        col.addLayout(head)

        self.sidebar_stack = QStackedWidget()
        self.trays = TrayList()
        self.trays.item_selected.connect(self._item_selected)
        state = QWidget()
        state_col = QVBoxLayout(state)
        state_col.setContentsMargins(SPACE["l"], SPACE["s"], SPACE["l"], 0)
        state_col.setSpacing(SPACE["s"])
        self.tray_state_title = label("", None, wrap=True)
        self.tray_state_body = label("", "secondary", wrap=True)
        state_col.addWidget(self.tray_state_title)
        state_col.addWidget(self.tray_state_body)
        state_col.addStretch(1)
        self.sidebar_stack.addWidget(self.trays)
        self.sidebar_stack.addWidget(state)
        self._state_page = state
        col.addWidget(self.sidebar_stack, 1)
        return side

    def _build_status_bar(self) -> None:
        bar = QStatusBar()
        bar.setSizeGripEnabled(False)
        self.user_status = FolderStatus("Sims 4", self._choose_user_dir)
        self.game_status = FolderStatus("Game", self._choose_game_dir)
        self.index_status = IndexStatus(self._show_skipped, self._refresh_index)
        bar.addWidget(self.user_status)
        bar.addPermanentWidget(self.index_status)
        bar.addPermanentWidget(self.game_status)
        self.setStatusBar(bar)

    # Tray gallery

    def _show_folders(self) -> None:
        self.user_status.set_path(self.folders.user_dir)
        self.game_status.set_path(self.folders.game_dir)
        self._tray_gen += 1
        gen = self._tray_gen
        self.trays.source.set_items([])
        self.search.setEnabled(False)
        self._select(None)
        tray_dir = self.folders.tray
        if tray_dir is None or not tray_dir.is_dir():
            self._set_tray_state(
                "Tray folder not found",
                "Choose your Sims 4 folder in the bar at the bottom. It's the folder that holds "
                "Mods and Tray, usually Documents/Electronic Arts/The Sims 4.")
            return
        self._set_tray_state("Reading your Tray folder", "")
        run_in_background(lambda: tray.scan_tray(tray_dir),
                          lambda result: self._on_tray_scanned(gen, result),
                          lambda e: self._on_tray_failed(gen, tray_dir, e))

    def _on_tray_scanned(self, gen: int, result) -> None:
        if gen != self._tray_gen:
            return
        items, failed = result
        households = [i for i in items if i.type == tray.ITEM_HOUSEHOLD]
        self.trays.source.set_items(households)
        if failed:
            self.statusBar().showMessage(
                f"{len(failed)} tray items couldn't be read and are left out.", 10_000)
        if not households:
            self._set_tray_state(
                "No households in your Tray folder",
                "Save a household to the library in the game, then reopen TrayTrim.")
            return
        self.search.setEnabled(True)
        self._search_changed(self.search.text())
        user_dir = self.folders.user_dir
        sim_ids = {s.sim_id for i in households for s in i.sims}
        if user_dir is not None:
            run_in_background(lambda: load_portraits(user_dir, sim_ids, PORTRAIT * 2),
                              lambda images: self._on_portraits(gen, images),
                              lambda _e: None)  # portraits are a nicety; initials stay

    def _on_portraits(self, gen: int, images) -> None:
        if gen == self._tray_gen:
            self.trays.source.set_portraits(images)

    def _on_tray_failed(self, gen: int, tray_dir: Path, error: Exception) -> None:
        if gen != self._tray_gen:
            return
        self._set_tray_state("Couldn't read the Tray folder",
                             f"{tray_dir}: {describe_error(error)}. If the game is running, close "
                             "it and reopen TrayTrim.")

    def _set_tray_state(self, title: str, body: str) -> None:
        self.tray_state_title.setText(title)
        self.tray_state_body.setText(body)
        self.tray_state_body.setVisible(bool(body))
        self.sidebar_stack.setCurrentWidget(self._state_page)

    def _apply_icons(self) -> None:
        p = theme.current()
        self._search_icon.setIcon(icons.icon("search", p.text_tertiary, 16))
        self._clear_action.setIcon(icons.icon("clear", p.text_secondary, 16))

    def changeEvent(self, event) -> None:  # noqa: N802
        if event.type() == QEvent.Type.PaletteChange:  # the theme changed
            self._apply_icons()
            self.household.refresh_theme()
            self.trays.viewport().update()
        super().changeEvent(event)

    def _search_changed(self, text: str) -> None:
        self._clear_action.setVisible(bool(text))
        self.trays.set_filter(text)
        if self.trays.source.rowCount() == 0:
            return
        if self.trays.visible_count():
            self.sidebar_stack.setCurrentWidget(self.trays)
        else:
            self._set_tray_state(f"Nothing matches \u201c{text.strip()}\u201d",
                                 "Search looks at household names and the names of their Sims.")

    def _focus_search(self) -> None:
        if self.search.isEnabled():
            self.search.setFocus()
            self.search.selectAll()

    # Household analysis

    def _item_selected(self, item: tray.TrayItem | None) -> None:
        self._select(item)

    def _select(self, item: tray.TrayItem | None) -> None:
        self._selected = item
        self._analysis_gen += 1
        if item is None:
            self.household.show_message("No household selected",
                                        "Pick a household on the left to see which CC it uses.")
            return
        self._analyse()

    def _sims_changed(self, sim_ids: set[int]) -> None:
        if self._selected is None:
            return
        self._sim_choice[self._selected.instance] = sim_ids
        full = self._full_report
        if full is not None and full.item.instance == self._selected.instance:
            self._show_report(full)  # just a filter over what's already looked up

    def _analyse(self) -> None:
        item = self._selected
        if item is None:
            return
        if not self._index_ready:
            self.household.show_message(
                item.name or "Unnamed household",
                "Waiting for the index of your Mods and game packages. The CC list appears as "
                "soon as it's ready.")
            return
        hh_file = item.file_with_suffix(".householdbinary")
        if hh_file is None:
            self.household.show_message(item.name, "This household's .householdbinary file is "
                                        "missing from the Tray folder, so its Sims can't be read.")
            return
        gen = self._analysis_gen
        # Only show "Reading..." if it takes long enough to notice; avoids a flash.
        QTimer.singleShot(250, lambda: self._show_reading(gen, item))

        def work() -> HouseholdReport:
            contents = tray.read_household(hh_file)
            ids = {s.sim.sim_id for s in contents.sims}
            with ResourceIndex() as ix:
                refs = locate(contents, ix)
            return HouseholdReport(item, contents, refs, ids)

        run_in_background(work, lambda r: self._on_report(gen, r),
                          lambda e: self._on_report_failed(gen, item, e))

    def _show_reading(self, gen: int, item: tray.TrayItem) -> None:
        if gen == self._analysis_gen and self._done_gen != gen:
            self.household.show_message(item.name or "Unnamed household", "Reading its Sims.")

    def _on_report(self, gen: int, report: HouseholdReport) -> None:
        if gen != self._analysis_gen:
            return
        self._done_gen = gen
        self._full_report = report
        self._show_report(report)
        known = self.household.thumbnails()
        parts = [r.ref.instance for r in report.refs
                 if r.where is Where.CC and r.ref.kind is tray.RefKind.CAS_PART
                 and r.ref.instance not in known]
        instance = report.item.instance
        if not parts:
            self.household.set_thumbnails(instance, {}, True)
            return
        user_dir = self.folders.user_dir

        def work():
            with ResourceIndex() as ix:
                return load_cas_thumbnails(ix, parts, THUMB * 2, user_dir)

        run_in_background(work, lambda images: self.household.set_thumbnails(instance, images),
                          lambda _e: self.household.set_thumbnails(instance, {}))

    def _show_report(self, full: HouseholdReport) -> None:
        all_ids = {s.sim.sim_id for s in full.contents.sims}
        chosen = (self._sim_choice.get(full.item.instance, all_ids) & all_ids) or all_ids
        refs = full.refs if chosen == all_ids else for_sims(full.refs, chosen)
        excluded = self._excluded.setdefault(full.item.instance, set())
        self.household.show_report(HouseholdReport(full.item, full.contents, refs, chosen,
                                                   excluded))
        self._start_plan()

    # Export plan (sizes, conflicts, missing files)

    def _exclusion_changed(self, excluded: set) -> None:
        if self._selected is None:
            return
        self._excluded[self._selected.instance] = excluded
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
        refs, excluded = report.refs, set(report.excluded)
        contents, sim_ids = report.contents, set(report.sim_ids)

        def work(task: BackgroundTask):
            with ResourceIndex() as ix:
                plan = build_plan(refs, ix, excluded,
                                  lambda done, total: task.report(done, total),
                                  task.cancelled)
                return plan, gameplay_mods(contents, ix, sim_ids)

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
        state = self.household.export_state()
        if state is None:
            return
        report, plan, gameplay = state
        request = ExportRequest(report.item, report.contents, report.refs, plan, gameplay,
                                set(report.sim_ids),
                                default_base_name(report.item, report.contents,
                                                  report.sim_ids),
                                self.folders.user_dir, self.folders.game_dir)
        ExportDialog(request, self.settings, self).exec()

    def _on_plan_failed(self, gen: int, instance: int, error: Exception) -> None:
        if gen == self._plan_gen:
            self.household.show_plan_error(instance, describe_error(error), self._start_plan)

    def _on_report_failed(self, gen: int, item: tray.TrayItem, error: Exception) -> None:
        if gen == self._analysis_gen:
            self._done_gen = gen
            self.household.show_message(f"Couldn't read {item.name or 'this household'}",
                                        describe_error(error))

    def _index_finished(self, _result) -> None:
        self._index_ready = True
        self._analysis_gen += 1
        self._analyse()

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
                self._analysis_gen += 1
                self._analyse()

        run_in_background(work, done, lambda _e: None)

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
        self.index.refresh(self.folders.mods, self.folders.game_dir)

    def _show_skipped(self) -> None:
        if self.index.last_result is not None:
            SkippedPackagesDialog(self.index.last_result, self).exec()

    def _folders_changed(self) -> None:
        self._index_ready = False
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

    def closeEvent(self, event) -> None:  # noqa: N802
        # Don't wait: the index task stops at its next check and SQLite rolls back anything
        # unfinished, so the next start simply resumes.
        self.index.stop()
        if self._plan_task is not None:
            self._plan_task.cancel()
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        try:
            self.settings.save()
        except OSError:
            pass  # losing the window size isn't worth blocking the user from closing the app
        super().closeEvent(event)
