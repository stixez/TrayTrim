"""The export dialog: where and how to export, progress, and the verified result."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit,
                               QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from traytrim.export import (ExportOptions, ExportResult, export_household, export_item,
                             forbidden_reason, safe_name)
from traytrim.gui import system
from traytrim.gui.describe import plural
from traytrim.gui.tasks import BackgroundTask, describe_error
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import (ElidedLabel, IconLabel, Notice, SimChip, SupportLine, label,
                                  link_button)
from traytrim.index import ResourceIndex
from traytrim.plan import Plan
from traytrim.resolve import GameplayMod, LocatedRef
from traytrim.settings import Settings
from traytrim.tray import HouseholdContents, TrayItem
from traytrim.units import human_size

STEPS = {"package": "Copying the CC into one package", "verify": "Checking the export",
          "zip": "Zipping"}


@dataclass
class ExportRequest:
    item: TrayItem | None            # None: one CC item on its own (refs holds it)
    contents: HouseholdContents | None
    refs: list[LocatedRef]
    plan: Plan
    gameplay: list[GameplayMod]
    sim_ids: set[int]
    base_name: str
    user_dir: Path | None
    game_dir: Path | None = None
    item_name: str = ""              # the CC item's name, when exporting one on its own


class ExportDialog(QDialog):
    def __init__(self, request: ExportRequest, settings: Settings, parent=None):
        super().__init__(parent)
        self.request = request
        self.settings = settings
        self._task: BackgroundTask | None = None
        self._result: ExportResult | None = None
        self.out_dir = Path(settings.output_dir) if settings.output_dir else system.desktop()
        self.setWindowTitle("Export")
        self.setMinimumWidth(560)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.pages.addWidget(self._options_page())
        self.pages.addWidget(self._progress_page())
        self.result_page = QWidget()
        self.pages.addWidget(self.result_page)

    # Pages

    def _options_page(self) -> QWidget:
        r = self.request
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        noun = r.contents.kind if r.contents is not None else "item"
        names = ([s.sim.name for s in r.contents.sims if s.sim.sim_id in r.sim_ids]
                 if r.contents is not None else [])
        self._intro(col, noun, names)
        col.addSpacing(SPACE["l"])

        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["l"])
        grid.setVerticalSpacing(SPACE["m"])
        grid.setColumnStretch(1, 1)
        grid.addWidget(label("Save to", "secondary"), 0, 0)
        where = QHBoxLayout()
        where.setSpacing(SPACE["s"])
        self.dir_label = ElidedLabel(str(self.out_dir), None, max_width=380)
        where.addWidget(self.dir_label, 1)
        change = link_button("Change")
        change.setAccessibleName("Change the folder to save to")
        change.clicked.connect(self._choose_dir)
        where.addWidget(change)
        grid.addLayout(where, 0, 1)
        grid.addWidget(label("File name", "secondary"), 1, 0)
        name_row = QHBoxLayout()
        name_row.setSpacing(SPACE["s"])
        self.name_edit = QLineEdit(r.base_name)
        self.name_edit.setAccessibleName("File name")
        name_row.addWidget(self.name_edit, 1)
        name_row.addWidget(label("_TrayTrim.zip", "secondary"))
        grid.addLayout(name_row, 1, 1)
        col.addLayout(grid)
        col.addSpacing(SPACE["s"])
        self.kit_chip = SimChip("Also make an in-game test kit")
        self.kit_chip.setChecked(True)
        self.kit_chip.setAccessibleName("Also make an in-game test kit")
        self.kit_chip.setToolTip("A folder with only the exported package and steps to try the "
                                 f"{noun} in your game with nothing else installed.")
        kit_row = QHBoxLayout()
        kit_row.addWidget(self.kit_chip)
        kit_row.addStretch(1)
        col.addLayout(kit_row)
        if r.item is None:
            self.kit_chip.setChecked(False)
            self.kit_chip.hide()

        col.addSpacing(SPACE["s"])
        if r.gameplay:
            if noun == "household":
                by = "the Sims"
                uses = (f"{'This Sim uses' if len(names) == 1 else 'These Sims use'} traits, "
                        "moods or other gameplay content")
            else:
                by, uses = f"the {noun}", f"This {noun} uses lot traits"
            details = [(m.rel, plural(m.ids, "id") + f" used by {by}", m.rel)
                       for m in r.gameplay]
            col.addWidget(Notice(
                "info", "text_secondary",
                f"{plural(len(r.gameplay), 'gameplay mod')} also needed",
                f"{uses} from mods that can't be trimmed. They're listed in the README; the "
                "recipient needs them installed.",
                details))
        col.addWidget(Notice(
            "info", "text_secondary", "For your own use",
            "Exports are meant for your own backups and transfers, or for CC you have the "
            "rights to share. Many creators don't allow re-uploading their CC."))
        self.options_error = label("", "danger", wrap=True)
        self.options_error.hide()
        col.addWidget(self.options_error)
        col.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.export_button = QPushButton("Export")
        self.export_button.setProperty("kind", "primary")
        self.export_button.setDefault(True)
        self.export_button.clicked.connect(self._start)
        buttons.addWidget(cancel)
        buttons.addWidget(self.export_button)
        col.addSpacing(SPACE["m"])
        col.addLayout(buttons)
        return page

    def _intro(self, col: QVBoxLayout, noun: str, names: list[str]) -> None:
        r = self.request
        size = human_size(r.plan.export_size)
        if r.item is None:
            col.addWidget(label(f"Export {r.item_name}", "title", wrap=True))
            col.addWidget(label(
                f"A {size} package with this item and everything it needs: "
                f"{plural(len(r.plan.resources), 'file')} from "
                f"{plural(len(r.plan.source_packages), 'CC package')}. It goes straight into "
                "Mods; there are no tray files. Every file is checked against its source "
                "afterwards.", "secondary", wrap=True))
            return
        if noun != "household":
            who, users = r.item.name or noun, f"this {noun} uses"
        else:
            who = names[0] if len(names) == 1 else r.item.name or "household"
            users = "this Sim uses" if len(names) == 1 else "these Sims use"
        col.addWidget(label(f"Export {who}", "title", wrap=True))
        col.addWidget(label(
            f"A {size} package with only the CC {users}, plus the tray files, in one zip. "
            "Every file is checked against its source afterwards.", "secondary", wrap=True))
        if noun == "household" and len(names) < len(r.contents.sims):
            col.addWidget(label(
                "The tray files always hold the whole household; CC is only included for the "
                "Sims you picked.", "secondary", wrap=True))

    def _progress_page(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        col.addWidget(label("Exporting", "title"))
        self.step_label = label("", "secondary", wrap=True)
        col.addWidget(self.step_label)
        col.addSpacing(SPACE["s"])
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setAccessibleName("Export progress")
        col.addWidget(self.bar)
        col.addStretch(1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel export")
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        col.addLayout(buttons)
        return page

    def _show_result(self, result: ExportResult) -> None:
        old = self.result_page
        self.result_page = QWidget()
        self.pages.insertWidget(2, self.result_page)
        self.pages.removeWidget(old)
        old.deleteLater()
        col = QVBoxLayout(self.result_page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        ok = result.verification.ok
        head = QHBoxLayout()
        head.setSpacing(SPACE["m"])
        head.addWidget(IconLabel("check" if ok else "alert",
                                 "success_text" if ok else "danger_text", 28),
                       0, Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(label("Export verified" if ok else "Export saved, but a check failed",
                             "title"), 1)
        col.addLayout(head)
        col.addWidget(label(f"{result.zip_path.name}, {human_size(result.zip_size)}",
                            "secondary", wrap=True))
        col.addSpacing(SPACE["m"])
        for check in result.verification.checks:
            row = QHBoxLayout()
            row.setSpacing(SPACE["m"])
            row.addWidget(IconLabel("check" if check.passed else "alert",
                                    "success_text" if check.passed else "danger_text", 20),
                          0, Qt.AlignmentFlag.AlignTop)
            text = QVBoxLayout()
            text.setSpacing(2)
            text.addWidget(label(check.name, "noticeTitle", wrap=True))
            text.addWidget(label(check.detail, "secondary", wrap=True))
            for problem in check.problems[:8]:
                text.addWidget(label(problem, "caption", wrap=True))
            if len(check.problems) > 8:
                text.addWidget(label(f"and {len(check.problems) - 8} more in report.json",
                                     "caption"))
            row.addLayout(text, 1)
            col.addLayout(row)
            col.addSpacing(SPACE["xs"])
        if result.test_kit:
            col.addSpacing(SPACE["s"])
            col.addWidget(label(
                f"The in-game test kit is in {result.test_kit.name}, next to the zip. Its "
                "HOW_TO_TEST.txt has the steps.", "secondary", wrap=True))
        col.addStretch(1)
        if ok:
            col.addWidget(SupportLine())
            col.addSpacing(SPACE["s"])
        buttons = QHBoxLayout()
        if result.test_kit:
            steps = QPushButton("Open test steps")
            steps.clicked.connect(lambda: system.open_path(result.test_kit / "HOW_TO_TEST.txt"))
            buttons.addWidget(steps)
        buttons.addStretch(1)
        show = QPushButton("Show in folder")
        show.clicked.connect(lambda: system.reveal(result.zip_path))
        done = QPushButton("Done")
        done.setProperty("kind", "primary")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        buttons.addWidget(show)
        buttons.addWidget(done)
        col.addSpacing(SPACE["m"])
        col.addLayout(buttons)
        self.pages.setCurrentWidget(self.result_page)
        done.setFocus()  # Enter closes it; no focus ring on a link the user didn't choose

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.pages.currentIndex() == 0:
            self.export_button.setFocus()  # Enter exports; no focus ring on a side control

    # Actions

    def _choose_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to save the export",
                                                  str(self.out_dir))
        if chosen:
            self.out_dir = Path(chosen)
            self.dir_label.setText(chosen)

    def _start(self) -> None:
        base = safe_name(self.name_edit.text(), "")
        if not base:
            self._options_message("Type a file name with at least one letter or digit.")
            return
        r = self.request
        reason = forbidden_reason(self.out_dir, r.user_dir, r.game_dir)
        if reason:
            self._options_message(reason)
            return
        self.options_error.hide()
        options = ExportOptions(self.out_dir, base, self.kit_chip.isChecked(), r.user_dir,
                                r.game_dir)

        def work(task: BackgroundTask) -> ExportResult:
            def report(step, done, total):
                task.report(step, done, total)

            with ResourceIndex() as ix:
                if r.item is None:
                    return export_item(r.refs[0], r.item_name, r.plan, options, ix, report,
                                       task.cancelled)
                return export_household(r.item, r.contents, r.refs, r.plan, r.gameplay,
                                        r.sim_ids, options, ix, report, task.cancelled)

        self.step_label.setText(STEPS["package"])
        self.bar.setRange(0, 0)
        self.cancel_button.setEnabled(True)
        self.pages.setCurrentIndex(1)
        task = BackgroundTask(work, keep_late_result=True)
        task.progress.connect(self._progress)
        task.finished.connect(self._finished)
        task.failed.connect(self._failed)
        task.aborted.connect(self._aborted)
        self._task = task
        task.start()

    def _progress(self, args: tuple) -> None:
        step, done, total = args
        self.step_label.setText(f"{STEPS[step]}: {done:,} of {total:,}")
        self.bar.setRange(0, max(total, 1))
        self.bar.setValue(done)

    def _finished(self, result: ExportResult) -> None:
        self._task = None
        self._result = result
        self.settings.output_dir = str(self.out_dir)
        try:
            self.settings.save()
        except OSError:
            pass
        self._show_result(result)

    def _failed(self, error: Exception) -> None:
        self._task = None
        self.pages.setCurrentIndex(0)
        self._options_message(f"The export failed: {describe_error(error)}. Nothing was saved.")

    def _aborted(self) -> None:
        self._task = None
        self.pages.setCurrentIndex(0)
        self._options_message("Export cancelled. Nothing was saved.")

    def _options_message(self, text: str) -> None:
        self.options_error.setText(text)
        self.options_error.show()

    def _cancel(self) -> None:
        if self._task is not None:
            self.cancel_button.setEnabled(False)
            self.step_label.setText("Cancelling")
            self._task.cancel()

    def reject(self) -> None:
        if self._task is not None:  # don't leave a half-written export behind
            self._cancel()
            return
        super().reject()

    @property
    def result(self) -> ExportResult | None:
        return self._result
