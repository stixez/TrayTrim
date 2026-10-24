"""The right side after opening a zip ("Check a zip"): is it intact, what's missing, what you
already have, and the CC in it, before anything is installed."""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QHBoxLayout

from traytrim.gui import system
from traytrim.gui.cc_view import STATIC, CcBrowser, CcState
from traytrim.gui.describe import display_name, item_label, plural
from traytrim.gui.report_parts import MAX_CARDS, NoticeStack, ReportPage, Verdict
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import Notice, label, link_button
from traytrim.names import ItemName
from traytrim.received import Received
from traytrim.tray import TrayRef
from traytrim.units import human_size

STEPS = {"check": "Checking every file in it", "copy": "Reading the zip",
         "index": "Reading its packages", "compare": "Comparing it with your Mods and game"}


def verdict_text(r: Received) -> str:
    if not r.integrity.ok:
        return "The zip is damaged"
    if not r.items and not r.cc:
        return "Nothing to install"
    if r.packs_missing:
        n = len(r.packs_missing)
        return f"Needs {plural(n, 'pack')} you don't have"
    if r.not_included:
        n = len(r.not_included)
        return f"{plural(n, 'item')} {'is' if n == 1 else 'are'} missing from it"
    return "Ready to install"


def contents_text(r: Received) -> str:
    """"1 lot · 14 CC items in 1 package · 30.5 MB"."""
    kinds: dict[str, int] = {}
    for i in r.items:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    parts = [plural(n, kind) for kind, n in kinds.items()]
    if r.cc:
        parts.append(f"{plural(len(r.cc), 'CC item')} in {plural(len(r.packages), 'package')}")
    if r.package_bytes:
        parts.append(human_size(r.package_bytes))
    return " · ".join(parts)


def _packs(packs: list[str]) -> str:
    return ", ".join("the base game" if p == "base" else p for p in packs)


class ReceivedView(ReportPage):
    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.received: Received | None = None
        self.user_dir: Path | None = None
        self._names: dict[TrayRef, ItemName] = {}
        self._thumbs: OrderedDict[int, QImage] = OrderedDict()
        self._thumbs_done = False
        self.browser: CcBrowser | None = None
        self._state: CcState | None = None
        self._view, self._sort, self._filters = "cards", "category", ("", "all", "all")
        self._working_for: Path | None = None

    # States

    def show_working(self, zip_path: Path, step: str = "", done: int = 0, total: int = 0) -> None:
        if self._working_for != zip_path:
            self.received = None
            self._working_for = zip_path
            col = self._head(zip_path.stem, f"{zip_path.name}")
            self.verdict = Verdict()
            col.addWidget(self.verdict)
            col.addStretch(1)
        self.verdict.show_working("Checking the zip", STEPS.get(step, ""), done, total)

    def show_error(self, zip_path: Path, reason: str) -> None:
        self.received = None
        self._working_for = None
        col = self._head(zip_path.stem, zip_path.name)
        verdict = Verdict()
        verdict.set_result("Couldn't check the zip", f"{reason}. Nothing was installed or "
                           "changed.")
        col.addWidget(verdict)
        col.addStretch(1)

    def show_received(self, r: Received) -> None:
        self.received = r
        self._working_for = None
        self._names, self._thumbs, self._thumbs_done = {}, OrderedDict(), False
        self._filters = ("", "all", "all")  # a new zip: no search (view and sort stay)
        self._build()

    def set_names(self, r: Received, names: dict[TrayRef, ItemName]) -> None:
        if r is not self.received or not names:
            return
        self._names.update(names)
        if self.browser is not None:
            self.browser.refresh()

    def set_thumbnails(self, r: Received, images: dict[int, QImage], finished: bool) -> None:
        if r is not self.received:
            return
        self._thumbs.update(images)
        self._thumbs_done = self._thumbs_done or finished
        if self._state is not None:
            self._state.all_tried = self._thumbs_done
        if self.browser is not None:
            self.browser.repaint_items()

    def refresh_theme(self) -> None:
        if self.received is not None:
            self._build()

    # Page

    def _head(self, title: str, line: str):
        col = self.new_page()
        row = QHBoxLayout()
        row.setSpacing(SPACE["l"])
        row.addWidget(label(title, "headline", wrap=True), 1)
        close = link_button("Close")
        close.setAccessibleName("Close the zip and go back")
        close.setToolTip("Go back (Esc). Nothing was installed or changed.")
        close.clicked.connect(self.closed.emit)
        row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(row)
        col.addSpacing(SPACE["m"])
        col.addWidget(label(f"Received · {line}", "secondary", wrap=True))
        col.addSpacing(SPACE["xl"])
        return col

    def _build(self) -> None:
        r = self.received
        col = self._head(r.name, f"{r.zip_path.name} · {human_size(r.zip_size)}")
        self.verdict = Verdict()
        self.verdict.set_result(verdict_text(r), contents_text(r))
        col.addWidget(self.verdict)
        col.addSpacing(SPACE["l"])
        notices = NoticeStack()
        col.addWidget(notices)
        self._fill_notices(notices)

        self.browser = self._state = None
        if r.cc:
            col.addSpacing(SPACE["xl"])
            bar = QHBoxLayout()
            bar.setSpacing(SPACE["s"])
            bar.addWidget(label("Custom content", "title"))
            bar.addWidget(label(str(len(r.cc)), "secondary"), 0, Qt.AlignmentFlag.AlignBaseline)
            bar.addStretch(1)
            col.addLayout(bar)
            col.addSpacing(SPACE["xs"])
            col.addWidget(label("What the zip brings, as it is in its package.", "secondary",
                                wrap=True))
            col.addSpacing(SPACE["l"])
            state = CcState(STATIC, any(i.kind == "household" for i in r.items), self._thumbs)
            state.names = self._names
            # Pictures are read from the zip all at once, and not at all for a big one.
            state.all_tried = self._thumbs_done or len(r.cc) > MAX_CARDS
            browser = CcBrowser(r.cc, state, view=self._view, sort=self._sort,
                                usage=bool(r.items), placeholder="Search names",
                                filters=self._filters)
            browser.view_changed.connect(self._remember_view)
            self.browser, self._state = browser, state
            col.addWidget(browser)
        col.addStretch(1)

    def _remember_view(self) -> None:
        if self.browser is not None:
            self._view, self._sort = self.browser.view, self.browser.sort
            self._filters = self.browser.filters()

    def _fill_notices(self, notices: NoticeStack) -> None:
        r = self.received
        for check in r.integrity.checks:
            if not check.passed:
                notices.add(Notice("alert", "danger_text", f"Check failed: {check.name}",
                                   check.detail,
                                   [(p, "", p) for p in check.problems[:50]]))
        if r.packs_missing:
            n = len(r.packs_missing)
            notices.add(Notice(
                "alert", "warning_text",
                f"{_packs(r.packs_missing)} {'isn' if n == 1 else 'aren'}'t installed",
                f"Some of what it uses comes from {'that pack' if n == 1 else 'those packs'}. "
                "Without it, those objects or outfits are missing in your game; the rest works."))
        if r.not_included:
            n = len(r.not_included)
            notices.add(Notice(
                "alert", "danger_text",
                f"{plural(n, 'item')} {'isn' if n == 1 else 'aren'}'t in the zip or your game",
                ("It's not in the zip, your Mods folder or your game, so it'll look different or "
                 "be missing. It may come from a pack you don't have, or the sender didn't have "
                 "it either." if n == 1 else
                 "They're not in the zip, your Mods folder or your game, so they'll look "
                 "different or be missing. They may come from a pack you don't have, or the "
                 "sender didn't have them either."),
                [(item_label(x), f"{x.ref.instance:016X}", f"Resource instance "
                  f"{x.ref.instance:016X}") for x in r.not_included],
                expanded=n <= 5))
        if r.overlap:
            same = [o for o in r.overlap if o.same]
            differ = [o for o in r.overlap if not o.same]
            n = len(r.overlap)
            if not differ:
                body = ("It's the same as your copy, so installing changes nothing for it."
                        if n == 1 else "They're the same as the copies in your Mods folder, so "
                        "installing changes nothing for them.")
            else:
                theirs = sum(o.theirs_loads for o in differ)
                who = ("the zip's copy" if theirs == len(differ) else "your copy" if not theirs
                       else "the copy that loads last")
                body = (f"{plural(len(same), 'is', 'are')} the same. For {len(differ):,}, your "
                        f"copy is different: put into Mods as it is, the game uses {who}. "
                        "Packages load in alphabetical order, and the last one wins.")
            details = [(display_name(o.ref, self._names.get(o.ref.ref)),
                        "the same as yours" if o.same
                        else "differs: the zip's copy is used" if o.theirs_loads
                        else "differs: your copy is used",
                        f"Yours: Mods/{o.yours.rel}") for o in r.overlap]
            have = ("You already have all of it" if n == len(r.cc)
                    else f"You already have {n:,} of its {plural(len(r.cc), 'item')}")
            notices.add(Notice("copies" if differ else "info",
                               "warning_text" if differ else "text_secondary", have, body,
                               details, expanded=bool(differ) and len(details) <= 5))
        if not r.game_known and (r.items or r.cc):
            notices.add(Notice(
                "info", "text_secondary", "Game content isn't checked",
                "TrayTrim doesn't know where the game is installed, so it can't tell which "
                "packs and game items you have. Choose the game folder in the bar at the bottom "
                "and check the zip again."))
        if r.gameplay_mods:
            n = len(r.gameplay_mods)
            notices.add(Notice(
                "info", "text_secondary",
                f"{plural(n, 'gameplay mod')} {'is' if n == 1 else 'are'} also needed",
                "It uses traits, moods or other gameplay content from mods that aren't in the "
                "zip. Install them from their creators.",
                [(m, "", m) for m in r.gameplay_mods]))
        if r.integrity.ok and (r.items or r.cc):
            notices.add(self._install_notice())

    def _install_notice(self) -> Notice:
        r = self.received
        # The real folder when it's known (OneDrive, Linux and Proton put it elsewhere).
        sims = str(self.user_dir) if self.user_dir is not None else \
            "Documents/Electronic Arts/The Sims 4"
        if r.items:
            body = (f"1. Copy the files in the zip's Tray folder into {os.path.join(sims, 'Tray')}.\n"
                    f"2. Copy its package{'s' if len(r.packages) > 1 else ''} into "
                    f"{os.path.join(sims, 'Mods')}.\n"
                    "3. Start the game and find it in the Library.")
        else:
            body = (f"Copy the package{'s' if len(r.packages) > 1 else ''} from the zip into "
                    f"{os.path.join(sims, 'Mods')}, then start the game.")
        actions = []
        if self.user_dir is not None:
            for name in (["Tray", "Mods"] if r.items else ["Mods"]):
                folder = self.user_dir / name
                actions.append((f"Open {name} folder",
                                lambda _b, f=folder: system.open_path(f)))
        actions.append(("Show the zip", lambda _b: system.reveal(r.zip_path)))
        return Notice("folder", "text_secondary", "How to install", body, actions=actions)
