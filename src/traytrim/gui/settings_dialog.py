"""Settings: the theme, the folders, how exports are made, updates, what TrayTrim keeps on disk,
and what TrayTrim is. Every change is saved at once."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QApplication, QButtonGroup, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QMessageBox, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from traytrim import SUPPORT_URL, __version__
from traytrim.export import clear_history, load_history
from traytrim.gui import system
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import CoffeeButton, ElidedLabel, SimChip, label, link_button
from traytrim.paths import cache_dir
from traytrim.settings import Settings
from traytrim.units import human_size
from traytrim.updates import RELEASES_PAGE, REPO, is_newer, latest_release

THEME_CHOICES = (("system", "Like the system"), ("light", "Light"), ("dark", "Dark"))


def folder_size(path: Path) -> int:
    """Bytes in a file, or in every file under a folder; 0 if it isn't there."""
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    except OSError:
        return 0


class _Answer(QObject):
    """Carries the update check's answer from its thread to the dialog."""
    done = Signal(object)  # Release, or None when GitHub couldn't be reached


class SettingsDialog(QDialog):
    """on_choose_user / on_choose_game: the window's folder choosers (they re-read everything);
    on_refresh_index: read the Mods folder and the game again; folders() -> (Sims 4, game)."""

    def __init__(self, settings: Settings, folders: Callable[[], tuple[Path | None, Path | None]],
                 on_choose_user: Callable[[], None], on_choose_game: Callable[[], None],
                 on_refresh_index: Callable[[], None], parent=None):
        super().__init__(parent)
        self.settings = settings
        self._folders = folders
        self.setWindowTitle("Settings")
        self.setMinimumSize(620, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xl"], SPACE["xl"], SPACE["xl"], SPACE["l"])
        outer.setSpacing(SPACE["s"])
        outer.addWidget(label("Settings", "title"))
        outer.addWidget(label("Changes are saved as you make them.", "secondary"))
        outer.addSpacing(SPACE["s"])

        holder = QWidget()
        holder.setObjectName("content")
        self.col = QVBoxLayout(holder)
        self.col.setContentsMargins(0, 0, SPACE["s"], 0)
        self.col.setSpacing(SPACE["s"])
        self._appearance()
        self._folders_section(on_choose_user, on_choose_game, on_refresh_index)
        self._exports()
        self._updates()
        self._storage()
        self._about()
        self.col.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(holder)
        outer.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        outer.addSpacing(SPACE["s"])
        outer.addLayout(buttons)

    # Layout helpers

    def _section(self, title: str, line: str = "") -> QVBoxLayout:
        if self.col.count():
            divider = QFrame(objectName="divider")
            divider.setFixedHeight(1)
            self.col.addSpacing(SPACE["m"])
            self.col.addWidget(divider)
            self.col.addSpacing(SPACE["m"])
        self.col.addWidget(label(title, "noticeTitle"))
        if line:
            self.col.addWidget(label(line, "secondary", wrap=True))
        body = QVBoxLayout()
        body.setSpacing(SPACE["s"])
        self.col.addSpacing(SPACE["xs"])
        self.col.addLayout(body)
        return body

    def _save(self) -> None:
        try:
            self.settings.save()
        except OSError as e:
            self.setWindowTitle(f"Settings (couldn't save: {e.strerror or e})")

    # Sections

    def _appearance(self) -> None:
        body = self._section("Appearance")
        row = QHBoxLayout()
        row.setSpacing(0)
        group = QButtonGroup(self)
        self.theme_buttons: dict[str, QPushButton] = {}
        for i, (key, text) in enumerate(THEME_CHOICES):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self.settings.theme == key)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else
                          "last" if i == len(THEME_CHOICES) - 1 else "middle")
            b.setAccessibleName(f"Theme: {text}")
            b.clicked.connect(lambda _c=False, k=key: self._set_theme(k))
            group.addButton(b)
            row.addWidget(b)
            self.theme_buttons[key] = b
        row.addStretch(1)
        body.addLayout(row)

    def _set_theme(self, key: str) -> None:
        from traytrim.gui.app import set_theme  # the app module builds the window: import late

        self.settings.theme = key
        set_theme(QApplication.instance(), key)
        self._save()

    def _folders_section(self, on_choose_user, on_choose_game, on_refresh_index) -> None:
        body = self._section("Folders", "TrayTrim only reads these. It finds them by itself; "
                             "change them if it found the wrong ones.")
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["l"])
        grid.setVerticalSpacing(SPACE["s"])
        grid.setColumnStretch(1, 1)
        self.user_label = ElidedLabel("", None, max_width=380)
        self.game_label = ElidedLabel("", None, max_width=380)
        for row, (name, value, choose) in enumerate((
                ("Sims 4 folder", self.user_label, on_choose_user),
                ("Game folder", self.game_label, on_choose_game))):
            grid.addWidget(label(name, "secondary"), row, 0)
            grid.addWidget(value, row, 1)
            change = link_button("Change")
            change.setAccessibleName(f"Change the {name.lower()}")
            change.clicked.connect(lambda _c=False, f=choose: (f(), self._show_folders()))
            grid.addWidget(change, row, 2)
        body.addLayout(grid)
        self._show_folders()
        again = QPushButton("Read Mods and the game again")
        again.setToolTip("Look for packages that were added, changed or removed (TrayTrim also "
                         "does this when it starts and when you come back to its window).")
        again.clicked.connect(lambda: (on_refresh_index(), again.setText("Reading…"),
                                       again.setEnabled(False)))
        self.refresh_button = again
        body.addWidget(again, 0, Qt.AlignmentFlag.AlignLeft)

    def refresh_finished(self, *_args) -> None:
        """The read the button started is over: it can be started again."""
        self.refresh_button.setText("Read Mods and the game again")
        self.refresh_button.setEnabled(True)

    def _show_folders(self) -> None:
        user, game = self._folders()
        self.user_label.setText(str(user) if user else "Not found")
        self.game_label.setText(str(game) if game else "Not found: TrayTrim can't tell EA items "
                                "from CC without it")

    def _exports(self) -> None:
        body = self._section("Exports")
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["l"])
        grid.setColumnStretch(1, 1)
        grid.addWidget(label("Save exports to", "secondary"), 0, 0)
        self.out_label = ElidedLabel(self.settings.output_dir or str(system.desktop()), None,
                                     max_width=380)
        grid.addWidget(self.out_label, 0, 1)
        change = link_button("Change")
        change.setAccessibleName("Change where exports are saved")
        change.clicked.connect(self._choose_output)
        grid.addWidget(change, 0, 2)
        body.addLayout(grid)
        self.kit_chip = SimChip("Make an in-game test kit with each export")
        self.kit_chip.setChecked(self.settings.export_test_kit)
        self.kit_chip.setToolTip("A folder next to the zip with only the exported packages and "
                                 "steps to try it in your game with nothing else installed. "
                                 "You can still turn it on or off for one export.")
        self.kit_chip.toggled.connect(self._set_kit)
        body.addWidget(self.kit_chip, 0, Qt.AlignmentFlag.AlignLeft)

    def _choose_output(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Choose where to save exports",
                                                  self.out_label.full_text())
        if chosen:
            self.settings.output_dir = chosen
            self.out_label.setText(chosen)
            self._save()

    def _set_kit(self, on: bool) -> None:
        self.settings.export_test_kit = on
        self._save()

    def _updates(self) -> None:
        body = self._section("Updates", f"You have TrayTrim {__version__}.")
        self.update_chip = SimChip("Check for a new version when TrayTrim starts")
        self.update_chip.setChecked(self.settings.check_updates)
        self.update_chip.setToolTip("Asks GitHub whether a newer release is out. Nothing about "
                                    "you or your files is sent, and nothing is installed unless "
                                    "you choose to.")
        self.update_chip.toggled.connect(self._set_updates)
        body.addWidget(self.update_chip, 0, Qt.AlignmentFlag.AlignLeft)
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        self.check_button = QPushButton("Check now")
        self.check_button.clicked.connect(self._check_now)
        row.addWidget(self.check_button)
        self.update_status = label("", "secondary", wrap=True)
        row.addWidget(self.update_status, 1)
        self.download_button = link_button("Download")
        self.download_button.hide()
        row.addWidget(self.download_button)
        body.addLayout(row)
        self._answer = _Answer(self)
        self._answer.done.connect(self._checked)

    def _set_updates(self, on: bool) -> None:
        self.settings.check_updates = on
        self._save()

    def _check_now(self) -> None:
        self.check_button.setEnabled(False)
        self.update_status.setText("Asking GitHub…")
        self.download_button.hide()
        answer = self._answer

        def run() -> None:
            release = latest_release()
            try:
                answer.done.emit(release)
            except RuntimeError:  # the dialog was closed meanwhile
                pass

        threading.Thread(target=run, daemon=True, name="update-check-now").start()

    def _checked(self, release) -> None:
        self.check_button.setEnabled(True)
        if release is None:
            self.update_status.setText("Couldn't reach GitHub. Check your connection, or see "
                                       f"{RELEASES_PAGE}")
        elif is_newer(release.version):
            self.update_status.setText(f"TrayTrim {release.version} is out.")
            self.download_button.show()
            try:
                self.download_button.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.download_button.clicked.connect(lambda: system.open_url(release.download))
        else:
            self.update_status.setText("You have the latest version.")

    def _storage(self) -> None:
        body = self._section("Storage", "What TrayTrim keeps to be fast. It's safe to delete: "
                             "it's built again, which takes a few minutes for a big Mods "
                             "folder.")
        cache = cache_dir()
        rows = [("Index of Mods and the game", folder_size(cache / "index.sqlite")),
                ("What needs what (the Mods tab)", folder_size(cache / "deps.sqlite")),
                ("Icons", folder_size(cache / "icons"))]
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["l"])
        grid.setColumnStretch(0, 1)
        for i, (name, size) in enumerate(rows):
            grid.addWidget(label(name, "secondary"), i, 0)
            grid.addWidget(label(human_size(size) if size else "None yet", None), i, 1,
                           Qt.AlignmentFlag.AlignRight)
        body.addLayout(grid)
        row = QHBoxLayout()
        row.setSpacing(SPACE["m"])
        open_cache = QPushButton("Open cache folder")
        open_cache.clicked.connect(lambda: system.open_path(cache))
        row.addWidget(open_cache)
        row.addStretch(1)
        body.addLayout(row)
        body.addSpacing(SPACE["s"])
        history = QHBoxLayout()
        self.history_label = label("", "secondary")
        history.addWidget(self.history_label, 1)
        self.clear_history_button = QPushButton("Clear export history")
        self.clear_history_button.setToolTip("Forget the list of earlier exports. The zips "
                                             "themselves stay where they are.")
        self.clear_history_button.clicked.connect(self._clear_history)
        history.addWidget(self.clear_history_button)
        body.addLayout(history)
        self._show_history()

    def _show_history(self) -> None:
        n = len(load_history())
        self.history_label.setText(f"Export history: {n:,} export{'' if n == 1 else 's'}"
                                   if n else "Export history: empty")
        self.clear_history_button.setEnabled(n > 0)

    def _clear_history(self) -> None:
        answer = QMessageBox.question(
            self, "Clear export history",
            "Forget every export in the history? The zips stay where they are.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                clear_history()
            except OSError as e:
                self.history_label.setText(f"Couldn't clear it: {e.strerror or e}")
                return
            self._show_history()

    def _about(self) -> None:
        body = self._section("About")
        body.addWidget(label(f"TrayTrim {__version__} · free and open source, MIT license",
                             None, wrap=True))
        body.addWidget(label("A fan-made tool, not affiliated with or endorsed by Electronic Arts "
                             "or Maxis.", "secondary", wrap=True))
        row = QHBoxLayout()
        row.setSpacing(SPACE["l"])
        source = link_button("Source code and releases")
        source.setToolTip(f"https://github.com/{REPO}")
        source.clicked.connect(lambda: system.open_url(f"https://github.com/{REPO}"))
        issues = link_button("Report a problem")
        issues.setToolTip(f"https://github.com/{REPO}/issues")
        issues.clicked.connect(lambda: system.open_url(f"https://github.com/{REPO}/issues"))
        for w in (source, issues):
            w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            row.addWidget(w)
        row.addStretch(1)
        coffee = CoffeeButton()
        coffee.setToolTip(SUPPORT_URL)
        row.addWidget(coffee)
        body.addLayout(row)
