"""Export several tray items: one verified zip each, or together in one zip that shares one
package (a lot with the household that lives there)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QDialog, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QProgressBar, QPushButton, QScrollArea,
                               QStackedWidget, QVBoxLayout, QWidget)

from traytrim import tray
from traytrim.batch import Choice, Outcome, export_many, export_together
from traytrim.export import ExportOptions, forbidden_reason
from traytrim.gui import icons, system, theme
from traytrim.gui.export_dialog import STEPS
from traytrim.gui.gallery import subtitle
from traytrim.gui.household_view import Notice, SimChip, plural
from traytrim.gui.tasks import BackgroundTask, describe_error
from traytrim.gui.theme import RADIUS, SPACE
from traytrim.gui.widgets import ElidedLabel, label, link_button
from traytrim.index import ResourceIndex
from traytrim.settings import Settings
from traytrim.tray import TrayItem
from traytrim.units import human_size


class PickRow(QAbstractButton):
    """One tray item: a tick to pick it, its name and size line, and later its export status."""

    HEIGHT = 48

    def __init__(self, item: TrayItem, parent=None):
        super().__init__(parent)
        self.item = item
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(item.name or f"Unnamed {item.kind}")
        self.setFixedHeight(self.HEIGHT)
        self._hover = False
        self._status = ""
        self._status_icon: tuple[str, str] | None = None  # icon name, colour token
        self._running = False

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(400, self.HEIGHT)

    def set_running(self) -> None:
        """From now on the row shows progress instead of being a choice."""
        self._running = True
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.update()

    def set_status(self, text: str, icon: tuple[str, str] | None = None) -> None:
        self._status, self._status_icon = text, icon
        self.setAccessibleDescription(text)
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if not self._running:
            super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._running:
            super().keyPressEvent(event)

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
        r = QRectF(self.rect()).adjusted(0, 1, 0, -1)
        if self._hover and not self._running:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.surface_hover))
            painter.drawRoundedRect(r, RADIUS["control"], RADIUS["control"])
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p.focus), 2))
            painter.drawRoundedRect(r.adjusted(1, 1, -1, -1), RADIUS["control"],
                                    RADIUS["control"])
        x = SPACE["s"]
        mid = self.height() / 2
        dpr = self.devicePixelRatioF()
        if self._running:
            if self._status_icon is not None:
                name, token = self._status_icon
                img = icons.image(name, getattr(p, token), 16, dpr)
                painter.drawImage(QRectF(x, mid - 8, 16, 16), img)
        elif self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.accent))
            painter.drawEllipse(QRectF(x, mid - 8, 16, 16))
            img = icons.image("check", p.on_accent, 12, dpr)
            painter.drawImage(QRectF(x + 2, mid - 6, 12, 12), img)
        else:
            painter.setPen(QPen(QColor(p.border_strong), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(x + 0.75, mid - 7.25, 14.5, 14.5))

        left = x + 16 + SPACE["m"]
        right = self.width() - SPACE["s"]
        status_width = 0
        if self._status:
            painter.setFont(theme.ui_font("caption"))
            fm = painter.fontMetrics()
            status_width = min(fm.horizontalAdvance(self._status), int((right - left) * 0.55))
            text = fm.elidedText(self._status, Qt.TextElideMode.ElideRight, status_width)
            painter.setPen(QColor(p.text_secondary))
            painter.drawText(QRectF(right - status_width, 0, status_width, self.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
            status_width += SPACE["l"]
        width = int(right - left - status_width)
        painter.setFont(theme.ui_font("body", QFont.Weight.Medium))
        painter.setPen(QColor(p.text))
        name = painter.fontMetrics().elidedText(self.accessibleName(),
                                                Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(left, mid - 19, width, 20),
                         Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft, name)
        painter.setFont(theme.ui_font("caption"))
        painter.setPen(QColor(p.text_secondary))
        second = painter.fontMetrics().elidedText(subtitle(self.item),
                                                  Qt.TextElideMode.ElideRight, width)
        painter.drawText(QRectF(left, mid + 1, width, 18),
                         Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, second)


_KIND_HEADINGS = ((tray.ITEM_HOUSEHOLD, "Households"), (tray.ITEM_LOT, "Lots"),
                  (tray.ITEM_ROOM, "Rooms"))


class BatchDialog(QDialog):
    """Pick tray items of any kind, export them, and show how each one went."""

    def __init__(self, items: list[TrayItem], choices: dict[int, Choice],
                 settings: Settings, user_dir: Path | None, game_dir: Path | None,
                 preselect: int | None = None, parent=None):
        super().__init__(parent)
        self.items, self.choices = items, choices
        self.settings, self.user_dir, self.game_dir = settings, user_dir, game_dir
        self.out_dir = Path(settings.output_dir) if settings.output_dir else system.desktop()
        self._task: BackgroundTask | None = None
        self._rows: list[PickRow] = []
        self._running: list[PickRow] = []
        self.setWindowTitle("Export several")
        self.setMinimumSize(600, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        outer.setSpacing(SPACE["s"])
        self.title = label("Export several", "title")
        outer.addWidget(self.title)
        self.intro = label(
            "Each one gets its own zip, checked like a single export, or put them together in "
            "one zip that shares one package, like a lot with the household that lives there. "
            "What you chose in the app (Sims, left-out items, copies) is kept; the rest start "
            "from the defaults.", "secondary", wrap=True)
        outer.addWidget(self.intro)
        outer.addSpacing(SPACE["s"])

        head = QHBoxLayout()
        self.count_label = label("", "secondary")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self.all_button = link_button("Select all")
        self.all_button.clicked.connect(lambda: self._set_all(True))
        self.none_button = link_button("Clear")
        self.none_button.clicked.connect(lambda: self._set_all(False))
        head.addWidget(self.all_button)
        head.addSpacing(SPACE["m"])
        head.addWidget(self.none_button)
        outer.addLayout(head)

        holder = QWidget()
        holder.setObjectName("content")
        self.list_col = QVBoxLayout(holder)
        self.list_col.setContentsMargins(0, 0, SPACE["s"], 0)
        self.list_col.setSpacing(0)
        self._headings: list[QWidget] = []
        for item_type, heading in _KIND_HEADINGS:
            of_kind = [i for i in items if i.type == item_type]
            if not of_kind:
                continue
            title = label(heading, "noticeTitle")
            title.setContentsMargins(0, SPACE["m"] if self._rows else 0, 0, SPACE["xs"])
            self._headings.append(title)
            self.list_col.addWidget(title)
            for item in of_kind:
                row = PickRow(item)
                row.setChecked(item.instance == preselect)
                row.toggled.connect(self._update_count)
                self._rows.append(row)
                self.list_col.addWidget(row)
        self.list_col.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setWidget(holder)
        outer.addWidget(self.scroll, 1)

        self.pages = QStackedWidget()
        self.pages.addWidget(self._options())
        self.pages.addWidget(self._progress())
        self.pages.addWidget(self._done())
        outer.addWidget(self.pages)
        self._update_count()

    # Pages under the list

    def _options(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, SPACE["m"], 0, 0)
        col.setSpacing(SPACE["m"])
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["l"])
        grid.setColumnStretch(1, 1)
        grid.addWidget(label("Save to", "secondary"), 0, 0)
        where = QHBoxLayout()
        self.dir_label = ElidedLabel(str(self.out_dir), None, max_width=380)
        where.addWidget(self.dir_label, 1)
        change = link_button("Change")
        change.setAccessibleName("Change the folder to save to")
        change.clicked.connect(self._choose_dir)
        where.addWidget(change)
        grid.addLayout(where, 0, 1)
        col.addLayout(grid)
        self.together_chip = SimChip("Together in one zip")
        self.together_chip.setAccessibleName("Export them together in one zip")
        self.together_chip.setToolTip(
            "One zip with all their tray files and one package with the CC of all of them; "
            "CC they share is in it once.")
        self.together_chip.toggled.connect(self._update_count)
        self.kit_chip = SimChip("In-game test kit")
        self.kit_chip.setAccessibleName("Also make an in-game test kit")
        self.kit_chip.setToolTip("A folder next to each zip with only its package and steps to "
                                 "try it in your game with nothing else installed.")
        chips = QHBoxLayout()
        chips.setSpacing(SPACE["s"])
        chips.addWidget(self.together_chip)
        chips.addWidget(self.kit_chip)
        chips.addStretch(1)
        col.addLayout(chips)
        col.addWidget(Notice(
            "info", "text_secondary", "For your own use",
            "Exports are meant for your own backups and transfers, or for CC you have the "
            "rights to share. Many creators don't allow re-uploading their CC."))
        self.options_error = label("", "danger", wrap=True)
        self.options_error.hide()
        col.addWidget(self.options_error)
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
        col.addLayout(buttons)
        return page

    def _progress(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, SPACE["m"], 0, 0)
        col.setSpacing(SPACE["s"])
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        self.bar.setAccessibleName("Export progress")
        col.addWidget(self.bar)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop after nothing more is saved. The item being exported "
                                    "leaves nothing behind.")
        self.stop_button.clicked.connect(self._stop)
        buttons.addWidget(self.stop_button)
        col.addStretch(1)
        col.addLayout(buttons)
        return page

    def _done(self) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, SPACE["m"], 0, 0)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        show = QPushButton("Show folder")
        show.clicked.connect(lambda: system.open_path(self.out_dir))
        done = QPushButton("Done")
        done.setProperty("kind", "primary")
        done.setDefault(True)
        done.clicked.connect(self.accept)
        buttons.addWidget(show)
        buttons.addWidget(done)
        col.addStretch(1)
        col.addLayout(buttons)
        self.done_button = done
        return page

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self.pages.currentIndex() == 0:
            # Start on the list, without a focus ring on a link the user didn't choose.
            self.scroll.setFocus()

    # Choosing

    def _picked(self) -> list[PickRow]:
        return [r for r in self._rows if r.isChecked()]

    def _update_count(self) -> None:
        n = len(self._picked())
        self.count_label.setText(f"{n} of {len(self._rows)} selected" if n
                                 else plural(len(self._rows), "item"))
        together = self.together_chip.isChecked()
        self.export_button.setEnabled(n > 1 if together else n > 0)
        self.export_button.setText("Export together" if together
                                   else f"Export {n}" if n else "Export")

    def _set_all(self, checked: bool) -> None:
        for row in self._rows:
            row.blockSignals(True)
            row.setChecked(checked)
            row.blockSignals(False)
            row.update()
        self._update_count()

    def _choose_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to save the exports",
                                                  str(self.out_dir))
        if chosen:
            self.out_dir = Path(chosen)
            self.dir_label.setText(chosen)

    # Exporting

    def _start(self) -> None:
        reason = forbidden_reason(self.out_dir, self.user_dir, self.game_dir)
        if reason:
            self.options_error.setText(reason)
            self.options_error.show()
            return
        self._running = self._picked()
        items = [r.item for r in self._running]
        self._together = self.together_chip.isChecked()
        for row in self._rows:
            row.setVisible(row in self._running)
            row.set_running()
        for heading in self._headings:
            heading.hide()
        for row in self._running:
            row.set_status("Waiting")
        self.all_button.hide()
        self.none_button.hide()
        self.title.setText(f"Exporting {plural(len(items), 'item')}"
                           + (" together" if self._together else ""))
        self.intro.setText(f"To {self.out_dir}")
        self.count_label.setText("")
        self.bar.setRange(0, 100 if self._together else len(items) * 100)
        self.bar.setValue(0)
        self.pages.setCurrentIndex(1)
        options = ExportOptions(self.out_dir, "", self.kit_chip.isChecked(), self.user_dir,
                                self.game_dir)
        choices = self.choices
        # Filled by the worker as each export is final (list.append is atomic), so whatever
        # ends the run, the rows show what was really saved.
        self._outcomes: list[Outcome] = []
        collected = self._outcomes

        def work(task: BackgroundTask) -> None:
            with ResourceIndex() as ix:
                if not self._together:
                    export_many(items, options, ix, choices,
                                lambda *args: task.report(*args, force=args[1] == "done"),
                                task.cancelled, lambda _position, o: collected.append(o))
                    return
                result = export_together(items, options, ix, choices,
                                         lambda step, d, t: task.report(-1, step, d, t),
                                         task.cancelled)
                collected.extend(Outcome(item, result) for item in items)


        task = BackgroundTask(work, keep_late_result=True)
        task.progress.connect(self._on_progress)
        task.finished.connect(lambda _none: self._show_outcomes())
        task.failed.connect(self._show_outcomes)
        task.aborted.connect(self._show_outcomes)
        self._task = task
        task.start()

    def _on_progress(self, args: tuple) -> None:
        position, step, done, total = args
        if position < 0:  # together: one export for every row
            text = ("Working out the export" if step == "plan"
                    else f"{STEPS[step]}: {done:,} of {total:,}")
            for row in self._running:
                row.set_status(text)
            share = {"plan": 0.0, "package": 0.2, "verify": 0.6, "zip": 0.9}[step]
            self.bar.setValue(int(share * 100))
            return
        row = self._running[position]
        if step == "done":
            self.bar.setValue((position + 1) * 100)
            return
        self.scroll.ensureWidgetVisible(row)
        if step == "plan":
            row.set_status("Working out the export")
            share = 0.0
        else:
            row.set_status(f"{STEPS[step]}: {done:,} of {total:,}")
            share = {"package": 0.2, "verify": 0.6, "zip": 0.9}[step]
        self.bar.setValue(position * 100 + int(share * 100))

    def _show_outcomes(self, error: Exception | None = None) -> None:
        self._task = None
        if self._together:
            self._show_together(error)
            return
        by_item = {id(o.item): o for o in self._outcomes}
        saved = verified = 0
        for row in self._running:
            o = by_item.get(id(row.item))
            if o is None:
                row.set_status("Not exported (stopped)", ("excluded", "text_tertiary"))
            elif o.result is not None:
                saved += 1
                if o.result.verification.ok:
                    verified += 1
                    row.set_status(f"Verified · {human_size(o.result.zip_size)}",
                                   ("check", "success_text"))
                else:
                    row.set_status("Saved, but a check failed", ("alert", "danger_text"))
                row.setToolTip(str(o.result.zip_path))
            elif o.skipped:
                row.set_status(o.skipped, ("excluded", "text_tertiary"))
            else:
                row.set_status(f"Failed: {o.error}", ("alert", "danger_text"))
                row.setToolTip(o.error)
        n = len(self._running)
        if error is not None:
            self.title.setText("The exports stopped")
        elif verified == n:
            self.title.setText(f"{plural(n, 'item')} exported and verified"
                               if n > 1 else "Exported and verified")
        else:
            self.title.setText(f"{saved} of {n} exported")
        failed = saved - verified
        where = f"Each zip is in {self.out_dir}." if saved else "Nothing was saved."
        if error is not None:
            where = f"{describe_error(error)}. {where}"
        self.intro.setText(where + (f" {plural(failed, 'export')} saved with a failed check: "
                                    "open its report.json for the details." if failed else ""))
        self._finish(saved > 0)

    def _show_together(self, error: Exception | None) -> None:
        result = self._outcomes[0].result if self._outcomes else None
        if result is not None:
            ok = result.verification.ok
            for row in self._running:
                row.set_status("In the zip" if ok else "Saved, a check failed",
                               ("check", "success_text") if ok else ("alert", "danger_text"))
                row.setToolTip(str(result.zip_path))
            self.title.setText("Exported together and verified" if ok
                               else "Exported together, but a check failed")
            self.intro.setText(f"{result.zip_path.name}, {human_size(result.zip_size)}, in "
                               f"{self.out_dir}." + ("" if ok else " Open its report.json for "
                                                     "the details."))
        else:
            stopped = error is None
            for row in self._running:
                row.set_status("Not exported", ("excluded", "text_tertiary"))
            self.title.setText("Export stopped" if stopped else "The export failed")
            self.intro.setText("Nothing was saved." if stopped
                               else f"{describe_error(error)}. Nothing was saved.")
        self._finish(result is not None)

    def _finish(self, saved: bool) -> None:
        self.bar.setValue(self.bar.maximum())
        if saved:
            self.settings.output_dir = str(self.out_dir)
            try:
                self.settings.save()
            except OSError:
                pass
        self.pages.setCurrentIndex(2)
        self.done_button.setFocus()

    def _stop(self) -> None:
        if self._task is not None:
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Stopping")
            self._task.cancel()

    def reject(self) -> None:
        if self._task is not None:  # finish cleanly: nothing half-written is left behind
            self._stop()
            return
        super().reject()
