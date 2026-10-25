"""The right side of the window: what the selected household uses and what an export would be."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QMenu, QScrollArea, QStackedWidget,
                               QVBoxLayout, QWidget)

from traytrim import restypes
from traytrim.gui.cc_view import MAX_THUMBNAILS as CC_MAX_THUMBNAILS
from traytrim.gui.cc_view import TOGGLE, CcBrowser, CcState
from traytrim.gui.describe import (full_name, item_label, item_subtitle, plural, usage_summary,
                                   usage_text)
from traytrim.gui.report_parts import Hero, Picture, StickyBar, item_menu
from traytrim.gui.overview import Overview
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import LoadingPanel, Notice, SimChip, label
from traytrim.names import ItemName
from traytrim.plan import Plan, PlannedResource
from traytrim.resolve import LocatedRef, Summary, ref_label, summarize
from traytrim.tray import HouseholdContents, TrayItem, TrayRef


@dataclass
class HouseholdReport:
    item: TrayItem
    contents: HouseholdContents
    refs: list[LocatedRef]
    sim_ids: set[int]
    excluded: set[TrayRef] = field(default_factory=set)
    overrides: set[TrayRef] = field(default_factory=set)  # left out unless the user opts in
    copies: dict = field(default_factory=dict)  # ResourceKey -> package path the user chose

    @property
    def noun(self) -> str:
        return self.contents.kind  # "household", "lot" or "room"


MAX_THUMBNAILS = CC_MAX_THUMBNAILS  # here so a test can lower it for this view


class HouseholdView(QWidget):
    """Empty, loading and error states, and the report for one household."""

    sims_changed = Signal(set)          # Sim ids to include
    exclusion_changed = Signal(set)     # TrayRefs left out of the export
    copies_chosen = Signal(object)  # {ResourceKey: package Path, or None for the default}
    export_requested = Signal()
    export_item_requested = Signal(object, object)  # LocatedRef, ItemName or None
    thumbnails_wanted = Signal(object, list)  # tray item (64-bit), refs of cards on screen

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        # How the CC is shown (cards or list, and the sort), kept from item to item.
        self._view = "cards"
        self._sort = "category"
        self.browser: CcBrowser | None = None
        self._state: CcState | None = None
        self._browser_item: int | None = None  # the tray item the browser on screen is for
        self._names: dict[TrayRef, ItemName] = {}
        self._plan_error: tuple[str, object] | None = None  # reason, retry
        self._expanded: set[str] = set()  # notices whose details are open
        self._pictures: dict[int, QImage] = {}  # tray item -> its own picture
        self._head: QHBoxLayout | None = None
        self._head_picture: Picture | None = None
        self._report: HouseholdReport | None = None
        self._plan: Plan | None = None
        self._gameplay: list = []
        # Loaded thumbnails, the most recently shown last. Kept across households (going back
        # to one shows its pictures at once), up to MAX_THUMBNAILS: browsing many households
        # would otherwise keep every picture ever seen.
        self._thumbs: OrderedDict[int, QImage] = OrderedDict()
        # Thumbnails are loaded for the cards painted on screen: asked (and not answered yet),
        # and tried (answered, with or without an image), kept across rebuilds.
        self._thumbs_asked: set[int] = set()
        self._thumbs_tried: set[int] = set()
        self._theme_timer = QTimer(self, singleShot=True, interval=0)
        self._theme_timer.timeout.connect(self._rebuild)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.message_page = LoadingPanel()
        self.message_title, self.message_body = self.message_page.title, self.message_page.body
        self.stack.addWidget(self.message_page)
        self.overview = Overview()
        self.stack.addWidget(self.overview)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Never sideways: anything too wide wraps or is shortened instead.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stack.addWidget(self.scroll)
        self.sticky = StickyBar(self.export_requested.emit, self.scroll)
        self.scroll.verticalScrollBar().valueChanged.connect(self._update_sticky)
        self.show_message("Nothing selected",
                          "Pick a household, lot or room on the left to see which CC it uses.")

    # States

    def show_overview(self, *args) -> None:
        """Nothing picked: what's there and where to start (Overview.fill's arguments)."""
        self.sticky.hide()
        self._browser_item = None  # picked again, the item starts with no search or filter
        self._report = None
        self._plan = None
        self._gameplay = []
        self.overview.fill(*args)
        self.stack.setCurrentWidget(self.overview)

    def showing_overview(self) -> bool:
        return self.stack.currentWidget() is self.overview

    def show_message(self, title: str, body: str, loading: bool = False) -> None:
        """loading: a loader screen with a spinner, while the item is read."""
        self.sticky.hide()
        self._browser_item = None  # the page was left: picked again, a fresh start
        self._report = None
        self._plan = None
        self._gameplay = []
        self.message_page.set_state(title, body, loading)
        self.stack.setCurrentWidget(self.message_page)

    def show_report(self, report: HouseholdReport) -> None:
        keep_scroll = (self._report is not None
                       and self._report.item.instance == report.item.instance)
        position = self.scroll.verticalScrollBar().value() if keep_scroll else 0
        if not keep_scroll:
            self._expanded.clear()
        self._report = report
        self._plan = None
        self._plan_error = None
        self._build()
        self.stack.setCurrentWidget(self.scroll)
        self.scroll.verticalScrollBar().setValue(position)

    def set_plan_progress(self, instance: int, done: int, total: int) -> None:
        if self._report is not None and self._report.item.instance == instance:
            self.hero.show_working(done, total)

    def show_plan_error(self, instance: int, reason: str, on_retry) -> None:
        if self._report is not None and self._report.item.instance == instance:
            self._plan = None  # whatever plan was on screen is for older choices
            self._plan_error = (reason, on_retry)
            self.hero.show_error(reason, on_retry)

    def set_plan(self, instance: int, plan: Plan, gameplay: list | None = None) -> None:
        if self._report is None or self._report.item.instance != instance:
            return
        self._plan = plan
        self._plan_error = None
        if gameplay is not None:
            self._gameplay = gameplay
        self._apply_plan()

    def plan_outdated(self) -> None:
        """The choices changed; nothing may export the plan on screen until the new one, and
        its notices (with their Choose links) go until then."""
        self._plan = None
        self._plan_error = None
        if self._report is not None:
            self.hero.show_working()
            self._clear_notices("plan")

    def _owner_sizes(self) -> dict[TrayRef, int]:
        sizes: dict[TrayRef, int] = {}
        if self._plan is not None:
            for res in self._plan.resources.values():
                sizes[res.owner] = sizes.get(res.owner, 0) + res.size
        return sizes

    def _apply_plan(self) -> None:
        plan = self._plan
        if plan is None:
            if self._plan_error is not None:
                self.hero.show_error(*self._plan_error)
            return
        self.hero.show_plan(plan)
        self._fill_plan_notices()
        if self.browser is not None:
            self.browser.set_sizes(self._owner_sizes())

    def set_thumbnails(self, instance: int, images: dict[int, QImage],
                       tried: set[int]) -> None:
        """Thumbnails loaded for the instances `tried`; those without one get the
        placeholder. Kept for whichever household is on screen: it may share them."""
        state = self._state or CcState(TOGGLE, False, self._thumbs, self._thumbs_tried,
                                       MAX_THUMBNAILS, asked=self._thumbs_asked)
        state.add_thumbnails(images, tried)
        if self.browser is not None:
            self.browser.repaint_items()

    def set_names(self, instance: int, names: dict[TrayRef, ItemName]) -> None:
        """Item names read from their packages arrive after the report is shown. Cards take
        them in place (and each section re-sorts); the list and the name sort are rebuilt."""
        self._names.update(names)
        if self._report is None or self._report.item.instance != instance or not names:
            return
        if self.browser is not None:
            self.browser.refresh()

    def forget_names(self) -> None:
        """The Mods folder changed: names may have too."""
        self._names.clear()

    def has_picture(self, instance: int) -> bool:
        return instance in self._pictures

    def set_picture(self, instance: int, picture: QImage | None) -> None:
        """The item's own picture, shown in the header once it's loaded."""
        if picture is None:
            return
        self._pictures[instance] = picture
        if (self._report is not None and self._report.item.instance == instance
                and self._head_picture is None):
            self._head_picture = Picture(picture, 150)
            self._head.insertWidget(0, self._head_picture, 0, Qt.AlignmentFlag.AlignTop)

    def current_item(self) -> TrayItem | None:
        return self._report.item if self._report else None

    def export_state(self):
        """(report, plan, gameplay) when an export can start, else None."""
        if self._report is None or self._plan is None or not self._plan.resources:
            return None
        return self._report, self._plan, self._gameplay

    def refresh_theme(self) -> None:
        """Rebuild what's painted from theme colours (icons, list colours). A theme switch
        sends several change events; they're folded into one rebuild that keeps the scroll
        position."""
        self.sticky.refresh_theme()
        self.overview.update()
        if not self._theme_timer.isActive():
            self._theme_timer.start()

    def _rebuild(self) -> None:
        """Build the page again (the theme changed), keeping the scroll position, the plan on
        screen and the keyboard focus (by the control's name), so keyboard users don't lose
        their place."""
        if self._report is None:
            return
        position = self.scroll.verticalScrollBar().value()
        focus = self.window().focusWidget()  # also right while the window isn't active
        page = self.scroll.widget()
        on_page = focus is not None and page is not None and page.isAncestorOf(focus)
        old = self.browser
        focused_ref = old.focused_ref() if old is not None and on_page else None
        searching = old is not None and old.search.hasFocus()
        cursor = old.search.cursorPosition() if searching else 0
        focused_name = focus.accessibleName() if on_page else ""
        self._build()
        self._apply_plan()
        self.scroll.verticalScrollBar().setValue(position)
        if searching and self.browser is not None:
            self.browser.search.setFocus()
            self.browser.search.setCursorPosition(cursor)
        elif focused_ref is not None and self.browser is not None:
            self.browser.focus_item(focused_ref)
        elif focused_name:
            same = next((w for w in self.scroll.widget().findChildren(QWidget)
                         if w.accessibleName() == focused_name and w.isVisible()), None)
            if same is not None:
                same.setFocus()

    # Report

    def _build(self) -> None:
        r = self._report
        assert r is not None
        page = QWidget()
        page.setObjectName("content")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        col = QVBoxLayout(page)
        col.setContentsMargins(SPACE["xxl"] + SPACE["s"], SPACE["xl"] + SPACE["s"],
                               SPACE["xxl"] + SPACE["s"], SPACE["xxl"])
        col.setSpacing(0)

        head = QHBoxLayout()
        head.setSpacing(SPACE["l"])
        self._head, self._head_picture = head, None
        picture = self._pictures.get(r.item.instance)
        if picture is not None:
            self._head_picture = Picture(picture, 150)
            head.addWidget(self._head_picture, 0, Qt.AlignmentFlag.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        head.addLayout(titles, 1)
        col.addLayout(head)
        titles.addWidget(label(r.item.name or f"Unnamed {r.noun}", "headline", wrap=True))
        titles.addSpacing(SPACE["m"])
        sims = r.contents.sims
        names = {s.sim.sim_id: s.sim.name or "Unnamed Sim" for s in sims}
        if r.noun != "household":
            self._chips = []
            line = item_subtitle(r.item, with_price=True)
            titles.addWidget(label(f"{r.noun.capitalize()} \u00b7 {line}" if line
                                else r.noun.capitalize(), "secondary"))
        elif len(sims) > 1:
            row = QHBoxLayout()
            row.setSpacing(SPACE["s"])
            self._chips: list[SimChip] = []
            for s in sims:
                chip = SimChip(names[s.sim.sim_id])
                chip.setChecked(s.sim.sim_id in r.sim_ids)
                chip.setProperty("sim_id", s.sim.sim_id)
                chip.toggled.connect(self._chip_toggled)
                self._chips.append(chip)
                row.addWidget(chip)
            row.addStretch(1)
            titles.addLayout(row)
        else:
            self._chips = []
            if sims:
                titles.addWidget(label(names[sims[0].sim.sim_id], "secondary"))

        col.addSpacing(SPACE["xl"])
        self.hero = Hero(self.export_requested.emit, r.noun)
        self.hero.changed.connect(self._sync_sticky)
        self.hero.show_working()
        col.addWidget(self.hero)
        self.sticky.set_title(r.item.name or f"Unnamed {r.noun}")

        summary = summarize(r.refs)
        col.addSpacing(SPACE["l"])
        self.notices = QVBoxLayout()
        self.notices.setSpacing(0)
        col.addLayout(self.notices)
        self._summary = summary
        self._fill_tray_notices()

        col.addSpacing(SPACE["xl"])
        bar = QHBoxLayout()
        bar.addWidget(label("Custom content", "title"))
        n_out = len(r.excluded & {x.ref for x in summary.cc})
        self.excluded_label = label(f"{n_out} left out" if n_out else "", "secondary")
        bar.addSpacing(SPACE["s"])
        bar.addWidget(self.excluded_label, 0, Qt.AlignmentFlag.AlignBaseline)
        bar.addStretch(1)
        col.addLayout(bar)
        col.addSpacing(SPACE["xs"])
        hint = ("Click an item, or its tick in the list, to leave it out of the export; "
                "right-click it to export it on its own.")
        col.addWidget(label(f"{self._source_line(summary)} {hint}", "secondary", wrap=True))
        col.addSpacing(SPACE["l"])

        old = self.browser
        same_item = old is not None and self._browser_item == r.item.instance
        self.browser = self._state = None
        if not summary.cc:
            col.addWidget(label(f"This {r.noun} doesn't use any CC from your Mods folder.",
                                "secondary", wrap=True))
        else:
            instance = r.item.instance
            state = CcState(TOGGLE, len(r.sim_ids) > 1, self._thumbs, self._thumbs_tried,
                            MAX_THUMBNAILS, asked=self._thumbs_asked)
            state.names = self._names
            state.excluded = r.excluded  # the same set: what's left out is the report's
            state.on_wanted = lambda refs: self.thumbnails_wanted.emit(instance, refs)
            if self._plan is not None:
                state.sizes = self._owner_sizes()
            # The same item shown again (a Sim chosen, the theme changed) keeps its search
            # and filters; another starts with none.
            filters = old.filters() if same_item else ("", "all", "all")
            browser = CcBrowser(summary.cc, state, view=self._view, sort=self._sort,
                                placeholder="Search names, creators and packages",
                                filters=filters, rank=old.rank() if same_item else None)
            browser.toggled.connect(self._card_toggled)
            browser.many_toggled.connect(lambda _refs, _included: self._exclusion_updated())
            browser.menu_requested.connect(self._card_menu)
            browser.view_changed.connect(self._remember_view)
            self.browser, self._state = browser, state
            self._browser_item = instance
            col.addWidget(browser)
        col.addStretch(1)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)

    def _remember_view(self) -> None:
        if self.browser is not None and self._report is not None:
            self._view, self._sort = self.browser.view, self.browser.sort

    @staticmethod
    def _source_line(s: Summary) -> str:
        parts = [f"{plural(len(s.cc), 'item')} from {plural(len(s.packages), 'package')} in "
                 "your Mods folder."]
        if s.game:
            packs = s.packs
            where = "the base game" if packs == ["base"] else ", ".join(
                "base game" if p == "base" else p for p in packs)
            n = len(s.game)
            parts.append(f"{plural(n, 'item')} {'comes' if n == 1 else 'come'} from the game "
                         f"({where}) and {'isn' if n == 1 else 'aren'}'t exported.")
        return " ".join(parts)


    # Notices

    def _notice(self, key: str, *args, **kwargs) -> Notice:
        """A notice whose details stay open across rebuilds once the user opened them."""
        return _compact_notice(*args, expanded=key in self._expanded,
                               on_expand=lambda shown: (self._expanded.add(key) if shown
                                                        else self._expanded.discard(key)),
                               **kwargs)

    def _copy_to_clipboard(self, lines: list[str], button) -> None:
        QGuiApplication.clipboard().setText("\n".join(lines) + "\n")
        button.setText("Copied")
        QTimer.singleShot(2000, lambda: _set_text_if_alive(button, "Copy list"))

    def _choose_copy(self, group: list[PlannedResource], button) -> None:
        """Pick the package an item's differing files come from: every package that holds a
        copy of any of them, the game's own choice first."""
        packages: dict[Path, str] = {}
        for res in group:
            for hit in reversed(res.copies):
                packages.setdefault(hit.path, hit.rel)
        chosen = any(res.chosen for res in group)
        current = {res.copy.path for res in group}
        menu = QMenu(self)
        game = menu.addAction("What the game loads (the copy that loads last, file by file)")
        game.setCheckable(True)
        game.setChecked(not chosen)
        game.triggered.connect(
            lambda: chosen and self.copies_chosen.emit({res.key: None for res in group}))
        menu.addSeparator()
        for path, rel in packages.items():
            action = menu.addAction(rel)
            action.setCheckable(True)
            action.setChecked(chosen and current == {path})
            action.triggered.connect(lambda _c=False, p=path: self._pick_package(group, p))
        menu.exec(button.mapToGlobal(button.rect().bottomLeft()) if button is not None
                  else QCursor.pos())
        menu.deleteLater()

    def _pick_package(self, group: list[PlannedResource], path: Path) -> None:
        """Take each file from `path` where it has a copy; the rest keep the game's choice."""
        picks = {}
        for res in group:
            has = any(h.path == path for h in res.copies)
            picks[res.key] = path if has and res.copies[-1].path != path else None
        if any(res.copy.path != (picks[res.key] or res.copies[-1].path) for res in group):
            self.copies_chosen.emit(picks)

    def _add_notice(self, notice: Notice, tag: str) -> None:
        notice.setProperty("phase", tag)
        if self.notices.count():
            line = QFrame()
            line.setObjectName("divider")
            line.setFixedHeight(1)
            line.setProperty("phase", tag)
            self.notices.addWidget(line)
        self.notices.addWidget(notice)

    def _clear_notices(self, tag: str) -> None:
        for i in reversed(range(self.notices.count())):
            w = self.notices.itemAt(i).widget()
            if w is not None and w.property("phase") == tag:
                self.notices.takeAt(i)
                w.deleteLater()
        # a leading divider left behind by removing the first notice
        first = self.notices.itemAt(0).widget() if self.notices.count() else None
        if first is not None and first.objectName() == "divider":
            self.notices.takeAt(0)
            first.deleteLater()

    def _fill_tray_notices(self) -> None:
        r, s = self._report, self._summary
        with_names = len(r.sim_ids) > 1
        if s.missing:
            n = len(s.missing)
            details = [(item_label(x), usage_summary(x, with_names),
                        f"Resource instance {x.ref.instance:016X}\n\n{usage_text(x)}")
                       for x in s.missing]
            if r.noun == "household":
                looks = "Sims look" if with_names else "Sim looks"
                extra = ""
            else:
                looks = f"{r.noun} looks"
                extra = (" Downloads from the gallery often use CC that doesn't come with "
                         "them.")
            lines = [f"{item_label(x)}\t{x.ref.instance:016X}\t{usage_summary(x, True)}"
                     for x in s.missing]
            self._add_notice(self._notice(
                "missing",
                "alert", "danger_text",
                f"{plural(n, 'item')} {'isn' if n == 1 else 'aren'}'t installed",
                f"They're not in your Mods folder or in the game, so the {looks} different "
                f"without them, here and in the export.{extra}", details,
                actions=[("Copy list", lambda b: self._copy_to_clipboard(lines, b))]), "tray")
        if r.overrides:
            n = len(r.overrides)
            details = [(item_label(x), full_name(x, self._names.get(x.ref)),
                        x.used.rel if x.used else "") for x in s.cc if x.ref in r.overrides]
            self._add_notice(_compact_notice(
                "info", "text_secondary",
                f"Your mods change {plural(n, 'EA item')} this {r.noun} uses",
                "Mostly to unlock or retune them, which rarely changes how it looks. Exported, "
                f"{'it' if n == 1 else 'they'}'d change the item everywhere in the recipient's "
                f"game, so {'it starts' if n == 1 else 'they start'} out of the export. To "
                "include one, click its card (marked Not exported).", details), "tray")
        for note in (r.contents.sims[0].notes if r.contents.sims else []):
            self._add_notice(_compact_notice("info", "text_secondary", "Partly read", note), "tray")

    def _fill_plan_notices(self) -> None:
        self._clear_notices("plan")
        plan = self._plan
        if plan is None:
            return
        if plan.missing:
            n = len(plan.missing)
            details, lines = [], []
            for key, needers in sorted(plan.missing.items(), key=lambda kv: str(kv[0])):
                parents = ", ".join(sorted({restypes.name(p.type) for p in needers}))
                details.append((restypes.name(key.type), f"Needed by: {parents}", str(key)))
                lines.append(f"{restypes.name(key.type)}\t{key}\tneeded by "
                             + ", ".join(str(p) for p in sorted(needers, key=str)))
            self._add_notice(self._notice(
                "missing-files",
                "alert", "danger_text", f"{plural(n, 'file')} that CC needs "
                f"{'is' if n == 1 else 'are'} missing",
                "Some CC refers to meshes or textures that aren't in your Mods folder, often a "
                "mesh that has to be downloaded separately. It already looks wrong in your game; "
                "the export can't fix that.", details[:200],
                actions=[("Copy list", lambda b: self._copy_to_clipboard(lines, b))]), "plan")
        if plan.conflicts:
            n = len(plan.conflicts)
            by_owner: dict[TrayRef, list[PlannedResource]] = {}
            for c in plan.conflicts:
                by_owner.setdefault(c.owner, []).append(c)
            located = {x.ref: x for x in self._report.refs}
            details = []
            for owner, group in by_owner.items():
                ref = located.get(owner)
                what = (full_name(ref, self._names.get(owner)) if ref is not None
                        else ref_label(owner))
                used = sorted({Path(c.copy.rel).name for c in group})
                files = plural(len(group), "file")
                details.append((
                    what, f"uses {', '.join(used)}"
                    + (" (your choice)" if any(c.chosen for c in group) else "")
                    + f" \u00b7 {files} with different copies",
                    "\n".join(f"{restypes.name(c.key.type)} {c.key}: "
                              + ", ".join(h.rel for h in c.copies) for c in group),
                    ("Choose", lambda b, g=group: self._choose_copy(g, b))))
            chosen = sum(1 for g in by_owner.values() if any(c.chosen for c in g))
            self._add_notice(self._notice(
                "conflicts",
                "copies", "warning_text",
                f"{plural(n, 'file')} {'has' if n == 1 else 'have'} different copies in Mods",
                "TrayTrim takes the copy the game uses: the one that loads last, in "
                "alphabetical order of its path in Mods. Choose another copy if you know it's "
                "the right one." + (f" You chose {chosen}." if chosen else ""), details),
                "plan")
        if plan.default_replacements:
            n = len(plan.default_replacements)
            details = [(restypes.name(d.key.type), Path(d.copy.rel).name, str(d.key))
                       for d in plan.default_replacements]
            if self._report.noun == "household":
                several = len(self._report.sim_ids) > 1
                who, wear, look = (("The Sims", "wear", "look") if several
                                   else ("The Sim", "wears", "looks"))
                body = (f"{who} {wear} these EA items in a version changed by your Mods "
                        f"(default replacements). They're exported so the {who.split()[1]} "
                        f"{look} the same, which changes the item for every Sim in the "
                        "recipient's game. Leave them out if that's not wanted.")
            else:
                body = ("You included EA items in a version changed by your Mods. They're "
                        "exported as they are, which changes them everywhere in the "
                        "recipient's game.")
            self._add_notice(_compact_notice(
                "info", "text_secondary",
                f"{plural(n, 'item')} {'replaces' if n == 1 else 'replace'} game content",
                body, details), "plan")
        if self._gameplay:
            n = len(self._gameplay)
            r = self._report
            if r.noun == "household":
                users = "the Sims"
                who = "The Sims use" if len(r.sim_ids) > 1 else "The Sim uses"
                what = "traits, moods or other gameplay content"
            else:
                users, who, what = f"the {r.noun}", f"The {r.noun} uses", "lot traits"
            details = [(m.rel, plural(m.ids, "id") + f" used by {users}", m.rel)
                       for m in self._gameplay]
            self._add_notice(_compact_notice(
                "info", "text_secondary",
                f"{plural(n, 'gameplay mod')} {'is' if n == 1 else 'are'} also needed",
                f"{who} {what} from these mods. They can't be trimmed, so they're not "
                "exported; the README lists them so the recipient can install them.",
                details), "plan")
        if plan.unreadable:
            n = len(plan.unreadable)
            details = [(restypes.name(k.type), Path(p).name, reason)
                       for k, p, reason in plan.unreadable]
            self._add_notice(_compact_notice(
                "alert", "danger_text", f"{plural(n, 'file')} couldn't be read",
                "Their own references couldn't be followed, so something they need may be "
                "missing from the export.", details), "plan")

    # Interaction

    def _chip_toggled(self) -> None:
        chosen = {c.property("sim_id") for c in self._chips if c.isChecked()}
        if not chosen:  # keep at least one Sim; an empty export makes no sense
            sender = self.sender()
            if isinstance(sender, SimChip):
                sender.blockSignals(True)
                sender.setChecked(True)
                sender.blockSignals(False)
            return
        self.sims_changed.emit(chosen)

    def _card_menu(self, ref: LocatedRef, pos) -> None:
        item_menu(self, pos, lambda: self.export_item_requested.emit(ref,
                                                                     self._names.get(ref.ref)))

    def _card_toggled(self, _ref: TrayRef, _included: bool) -> None:
        """The browser changed the report's set of what's left out (it's the same set)."""
        self._exclusion_updated()

    def _exclusion_updated(self) -> None:
        r = self._report
        if r is None:
            return
        n_out = len(r.excluded & {x.ref for x in self._summary.cc})
        self.excluded_label.setText(f"{n_out} left out" if n_out else "")
        self._plan = None  # the shown plan no longer matches; nothing may export it
        self.hero.show_working()
        self.exclusion_changed.emit(set(r.excluded))

    def _hero_on_page(self):
        hero = getattr(self, "hero", None)
        page = self.scroll.widget()
        if self._report is None or hero is None or page is None or not page.isAncestorOf(hero):
            return None
        return hero

    def _sync_sticky(self, *_args) -> None:
        """What the export bar says (the Hero changed) and where it sits (the page resized)."""
        hero = self._hero_on_page()
        if hero is not None:
            self.sticky.copy(hero)
        viewport = self.scroll.viewport().geometry()
        self.sticky.setGeometry(viewport.left(), viewport.top(), viewport.width(),
                                self.sticky.sizeHint().height())
        self._update_sticky()

    def _update_sticky(self, *_args) -> None:
        """The export bar over the top of the page, while the export size and its Export
        button are scrolled out of sight (on every scroll step: only shown or hidden)."""
        hero = self._hero_on_page()
        if hero is None:
            self.sticky.hide()
            return
        bottom = hero.mapTo(self.scroll.widget(), QPoint(0, hero.height())).y()
        show = (bottom < self.scroll.verticalScrollBar().value()
                and self.stack.currentWidget() is self.scroll)
        if show != self.sticky.isVisible():
            self.sticky.setVisible(show)
            if show:
                self.sticky.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_sticky()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(800, 600)


def _compact_notice(*args, **kwargs) -> Notice:
    """The page's notices are one line each until opened: the CC isn't pushed off screen by
    explanations that are read once."""
    return Notice(*args, compact=True, **kwargs)


def _set_text_if_alive(button, text: str) -> None:
    try:
        button.setText(text)
    except RuntimeError:  # the notice was rebuilt meanwhile
        pass
