"""The right side in the Mods tab: one package, what needs it, and the CC items in it."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path, PurePosixPath

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from traytrim import restypes
from traytrim.gui import system
from traytrim.gui.cc_view import EXPORT, MAX_THUMBNAILS, CcBrowser, CcState
from traytrim.gui.describe import plural
from traytrim.gui.flow import FlowLayout
from traytrim.gui.report_parts import NoticeStack, ReportPage, Verdict, item_menu
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import Notice, ShowMore, label, link_button
from traytrim.names import ItemName
from traytrim.pkgreport import Dependent, PackageReport
from traytrim.resolve import LocatedRef
from traytrim.tray import TrayItem, TrayRef
from traytrim.units import human_size

PAGE_USERS = 60  # "Used by" links shown at a time
CAVEAT = "Only your Tray is checked: Sims, lots and rooms that are only in a save game aren't."


def users_text(items: list[TrayItem]) -> str:
    """"Used by 3 households and 1 lot"."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    parts = [plural(n, kind) for kind, n in counts.items()]
    if not parts:
        return "Nothing in your Tray uses it"
    return "Used by " + (parts[0] if len(parts) == 1
                         else f"{', '.join(parts[:-1])} and {parts[-1]}")


def needs_text(dep: Dependent) -> str:
    """"12 items use its mesh and textures", or for one that needs it through another
    package, "4 items use Soft_Bob_Mesh, which needs it"."""
    n = len({n.by for n in dep.needs})
    if dep.through:
        return (f"{plural(n, 'item')} {'uses' if n == 1 else 'use'} "
                f"{PurePosixPath(dep.through).stem}, which needs it")
    kinds = []
    types = {n.key.type for n in dep.needs}
    if types & {restypes.GEOM, restypes.MODEL, restypes.MODEL_LOD}:
        kinds.append("mesh")
    if types & restypes.TEXTURE_TYPES:
        kinds.append("textures")
    if types - restypes.TEXTURE_TYPES - {restypes.GEOM, restypes.MODEL, restypes.MODEL_LOD}:
        kinds.append("other files")
    what = " and ".join(kinds) if len(kinds) <= 2 else f"{kinds[0]}, {kinds[1]} and {kinds[2]}"
    return f"{plural(n, 'item')} {'uses' if n == 1 else 'use'} its {what}"


class PackageView(ReportPage):
    export_item_requested = Signal(object, object)  # LocatedRef, ItemName or None
    package_requested = Signal(object)              # Path of another package
    tray_item_requested = Signal(object)            # TrayItem
    thumbnails_wanted = Signal(object, list)        # package Path, refs of cards on screen

    def __init__(self, parent=None):
        super().__init__(parent)
        self.report: PackageReport | None = None
        self._names: dict[TrayRef, ItemName] = {}
        self._thumbs: OrderedDict[int, QImage] = OrderedDict()
        self._tried: set[int] = set()
        self._asked: set[int] = set()
        self.browser: CcBrowser | None = None
        self._state: CcState | None = None
        self._view, self._sort = "cards", "category"  # kept from package to package
        self._filters = ("", "all", "all")  # kept while it's the same package
        self._users: list[TrayItem] | None = None       # None until worked out
        self._dependents: list[Dependent] | None = None
        self._unreadable: list[Path] = []
        self._failed = ""  # why what needs the package couldn't be worked out
        self._tray_read = True
        self._tray_unreadable: list[str] = []
        self.show_idle()

    # States

    def show_idle(self, progress: str = "", done: int = 0, total: int = 0) -> None:
        """Nothing selected; `progress` says what's being worked out meanwhile."""
        self.report = None
        body = "Pick a package on the left to see what's in it and what needs it."
        self.show_message("Nothing selected", f"{body}\n\n{progress}" if progress else body,
                          bool(progress), done, total)

    def clear_progress(self) -> None:
        """The background work is done: the idle message loses its progress line (any other
        message, like a deleted package's, stays)."""
        if self.report is None and self._idle():
            self.show_idle()

    def _idle(self) -> bool:
        return (self.stack.currentWidget() is self.message_page
                and self.message_title.text() == "Nothing selected")

    def show_loading(self, title: str) -> None:
        self.report = None
        self.show_message(title, "Reading the package.", loading=True)

    def show_report(self, report: PackageReport) -> None:
        """A package, read just now. What needs it is worked out again every time (the index
        changed, or another package was picked); names, thumbnails and the search stay when
        it's the same package."""
        same = self.report is not None and self.report.path == report.path
        self.report = report
        if not same:
            self._names, self._thumbs, self._tried, self._asked = {}, OrderedDict(), set(), set()
            self._filters = ("", "all", "all")
        self._users = self._dependents = None
        self._unreadable, self._failed, self._tray_read = [], "", True
        self._tray_unreadable = []
        self._build()

    @property
    def failed(self) -> bool:
        return bool(self._failed)

    def show_progress(self, text: str, line: str = "", done: int = 0, total: int = 0) -> None:
        if self.report is not None and self._users is None and not self._failed:
            self.verdict.show_working(text, line, done, total)
        elif self.report is None and self._idle():
            self.show_idle(line, done, total)

    def set_usage(self, path: Path, users: list[TrayItem], dependents: list[Dependent],
                  unreadable: list[Path] | None = None, tray_read: bool = True,
                  tray_unreadable: list[str] | None = None) -> None:
        """What needs the package. `unreadable`: packages that couldn't be read, so what needs
        this one may be incomplete; `tray_unreadable` likewise for tray items. tray_read False:
        no Tray to check users against."""
        if self.report is None or self.report.path != path:
            return
        self._users, self._dependents, self._failed = users, dependents, ""
        self._unreadable = list(unreadable or [])
        self._tray_unreadable = list(tray_unreadable or [])
        self._tray_read = tray_read
        self._fill_usage()

    def set_usage_failed(self, path: Path, reason: str) -> None:
        if self.report is None or self.report.path != path:
            return
        self._users = self._dependents = None
        self._failed = reason or "Something went wrong"
        self._fill_usage()

    def set_names(self, path: Path, names: dict[TrayRef, ItemName]) -> None:
        if self.report is None or self.report.path != path or not names:
            return
        self._names.update(names)
        if self.browser is not None:
            self.browser.refresh()

    def set_thumbnails(self, path: Path, images: dict[int, QImage], tried: set[int]) -> None:
        """Thumbnails loaded for the instances `tried`; those without one get the
        placeholder."""
        if self.report is None or self.report.path != path:
            return
        state = self._state or CcState(EXPORT, False, self._thumbs, self._tried, MAX_THUMBNAILS,
                                       asked=self._asked)
        state.add_thumbnails(images, tried)
        if self.browser is not None:
            self.browser.repaint_items()

    def refresh_theme(self) -> None:
        if self.report is not None:
            self._build()

    # Page

    def _build(self) -> None:
        r = self.report
        assert r is not None
        position = self.scroll.verticalScrollBar().value()
        col = self.new_page()
        col.addWidget(label(PurePosixPath(r.rel).stem, "headline", wrap=True))
        col.addSpacing(SPACE["m"])
        folder = str(PurePosixPath(r.rel).parent)
        facts = [f"Mods/{folder}" if folder != "." else "Mods", human_size(r.size)]
        if r.items:
            facts.append(plural(len(r.items), "item"))
        facts.append(plural(r.files, "file"))
        col.addWidget(label(" · ".join(facts), "secondary", wrap=True))

        col.addSpacing(SPACE["xl"])
        self.verdict = Verdict("Show in folder", lambda: system.reveal(r.path), "folder")
        self.verdict.button.setToolTip("Show the package in your file manager. TrayTrim never "
                                       "deletes or moves anything in Mods.")
        col.addWidget(self.verdict)

        col.addSpacing(SPACE["l"])
        self.notices = NoticeStack()
        col.addWidget(self.notices)
        self.users_box = QWidget()
        self._users_col = QVBoxLayout(self.users_box)
        self._users_col.setContentsMargins(0, 0, 0, 0)
        self._users_col.setSpacing(SPACE["m"])
        links = QWidget()
        self._users_flow = FlowLayout(links, spacing=SPACE["m"])
        self._users_col.addWidget(links)
        self._users_more: ShowMore | None = None
        self.users_head = QWidget()
        head = QHBoxLayout(self.users_head)
        head.setContentsMargins(0, SPACE["xl"], 0, SPACE["s"])
        head.setSpacing(SPACE["s"])
        head.addWidget(label("Used by", "title"))
        self.users_count = label("", "secondary")
        head.addWidget(self.users_count, 0, Qt.AlignmentFlag.AlignBaseline)
        head.addStretch(1)
        col.addWidget(self.users_head)
        col.addWidget(self.users_box)
        self._fill_usage()

        col.addSpacing(SPACE["xl"])
        bar = QHBoxLayout()
        bar.setSpacing(SPACE["s"])
        bar.addWidget(label("What's inside", "title"))
        bar.addWidget(label(str(len(r.items)), "secondary"), 0, Qt.AlignmentFlag.AlignBaseline)
        bar.addStretch(1)
        col.addLayout(bar)
        col.addSpacing(SPACE["xs"])
        self.browser = None
        if not r.items:
            col.addWidget(label("No CAS parts, objects or build items: only files other CC or "
                                "the game uses, like meshes, textures or tuning.", "secondary",
                                wrap=True))
        else:
            col.addWidget(label("Click an item to export it on its own, with everything it "
                                "needs.", "secondary", wrap=True))
            col.addSpacing(SPACE["l"])
            path = r.path
            # Pictures are read as cards are shown; a big package keeps the latest ones.
            state = CcState(EXPORT, False, self._thumbs, self._tried, MAX_THUMBNAILS,
                            asked=self._asked)
            state.names = self._names
            state.on_wanted = lambda refs: self.thumbnails_wanted.emit(path, refs)
            browser = CcBrowser(r.items, state, view=self._view, sort=self._sort, usage=False,
                                placeholder="Search names and creators", filters=self._filters)
            browser.item_activated.connect(self._export_ref)
            browser.menu_requested.connect(self._card_menu)
            browser.view_changed.connect(self._remember_view)
            self.browser, self._state = browser, state
            col.addWidget(browser)
        col.addStretch(1)
        self.scroll.verticalScrollBar().setValue(position)

    def _remember_view(self) -> None:
        if self.browser is not None:
            self._view, self._sort = self.browser.view, self.browser.sort
            self._filters = self.browser.filters()

    def _fill_usage(self) -> None:
        r = self.report
        self.notices.clear()
        users, deps = self._users, self._dependents
        if self._unreadable:
            n = len(self._unreadable)
            self.notices.add(Notice(
                "alert", "warning_text",
                f"{plural(n, 'package')} couldn't be read",
                f"{'It was' if n == 1 else 'They were'} locked by another program or "
                f"{'is' if n == 1 else 'are'} damaged, so what needs this package may be missing "
                "below. TrayTrim tries again the next time it checks.",
                [(p.name, "", str(p)) for p in self._unreadable], expanded=n <= 3))
        if self._tray_unreadable and users is not None:
            n = len(self._tray_unreadable)
            self.notices.add(Notice(
                "alert", "warning_text", f"{plural(n, 'tray item')} couldn't be read",
                f"{'It isn' if n == 1 else 'They aren'}'t counted in Used by. If the game is "
                "running, close it, then come back to TrayTrim to check again.",
                [(name, "", name) for name in self._tray_unreadable], expanded=n <= 3))
        if deps:
            n = len(deps)
            details = [(d.rel, needs_text(d),
                        "\n".join(sorted({f"{restypes.name(x.by.type)} {x.by}" for x in d.needs})),
                        ("Open", lambda _b, p=d.path: self.package_requested.emit(p)))
                       for d in deps]
            self.notices.add(Notice(
                "alert", "danger_text",
                f"{plural(n, 'other package')} {'needs' if n == 1 else 'need'} files from it",
                "Their CC uses a mesh or texture that only this package has"
                + (", directly or through another package" if any(d.through for d in deps)
                   else "")
                + ". Delete it and that CC shows up broken or invisible in the game.", details,
                expanded=len(details) <= 5))
        if r.shadowed:
            by_other: dict[str, int] = {}
            for _key, hit in r.shadowed:
                by_other[hit.rel] = by_other.get(hit.rel, 0) + 1
            details = [(rel, f"loads instead for {plural(k, 'file')}", rel)
                       for rel, k in sorted(by_other.items(), key=lambda kv: -kv[1])]
            if not r.loaded:
                title = "None of its files are loaded"
                body = ("Every file in it is also in a package that loads later. Packages load "
                        "in alphabetical order and the last copy wins, so the game uses those "
                        "copies, and deleting this one shouldn't change anything in the game.")
            else:
                n = len(r.shadowed)
                title = f"{n:,} of its files {'is' if n == 1 else 'are'} never loaded"
                body = ("Packages that load later hold the same files, and the game uses their "
                        "copies. Deleting this package doesn't change those.")
            self.notices.add(Notice("copies", "warning_text", title, body, details,
                                    expanded=len(details) <= 3))
        if r.replaces_game:
            n = len(r.replaces_game)
            self.notices.add(Notice(
                "info", "text_secondary",
                f"It replaces {plural(n, 'game file')}",
                "It changes content of the game itself, for every Sim or lot (a default "
                "replacement or an override). Deleting it brings back the game's version."))
        self.notices.setVisible(bool(deps or r.shadowed or r.replaces_game or self._unreadable
                                     or self._tray_unreadable))

        while self._users_flow.count():
            w = self._users_flow.takeAt(0).widget()
            if w is not None:
                w.deleteLater()
        if users is None:
            if self._failed:
                self.verdict.set_result("Couldn't work out what needs it",
                                        f"{self._failed}. Pick the package again to try again.")
            else:
                self.verdict.show_working("Working out what uses it")
            self.users_head.hide()
            self.users_box.hide()
            return
        if not self._tray_read:
            self.verdict.set_result("Not checked against your Tray",
                                    "Your Tray folder couldn't be read, so TrayTrim can't tell "
                                    "which households, lots or rooms use it.")
            self.users_head.hide()
            self.users_box.hide()
            return
        self.verdict.set_result(users_text(users), CAVEAT)
        self.users_count.setText(str(len(users)))
        if self._users_more is not None:
            self._users_more.deleteLater()
        self._users_more = ShowMore(len(users), self._add_users, PAGE_USERS)
        self._users_col.addWidget(self._users_more)
        self._users_shown = 0
        self._add_users()
        self.users_head.setVisible(bool(users))
        self.users_box.setVisible(bool(users))

    def _add_users(self) -> None:
        """The next page of "Used by" links (a popular package can be used by hundreds)."""
        start = self._users_shown
        for item in self._users[start:start + PAGE_USERS]:
            name = item.name or f"Unnamed {item.kind}"
            text = name if item.kind == "household" else f"{name} ({item.kind})"
            link = link_button(text)
            link.setAccessibleName(f"Open {text}")
            link.clicked.connect(lambda _c=False, i=item: self.tray_item_requested.emit(i))
            self._users_flow.addWidget(link)
        self._users_shown = min(start + PAGE_USERS, len(self._users))
        self._users_more.set_shown(self._users_shown)

    def _card_menu(self, ref: LocatedRef, pos) -> None:
        item_menu(self, pos, lambda: self._export_ref(ref))

    def _export_ref(self, ref: LocatedRef) -> None:
        self.export_item_requested.emit(ref, self._names.get(ref.ref))

