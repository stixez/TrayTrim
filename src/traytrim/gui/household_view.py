"""The right side of the window: what the selected household uses and what an export would be."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLineEdit, QMenu,
                               QPushButton, QScrollArea, QStackedWidget, QVBoxLayout, QWidget)

from traytrim import restypes
from traytrim.gui import icons, theme
from traytrim.gui.describe import (THUMB_TYPES, card_caption, display_name, group_refs,
                                   item_label, item_subtitle, plural, usage_summary, usage_text)
from traytrim.gui.report_parts import (MAX_CARDS, CcCard, Hero, PagedCcList, Picture, Section,
                                       first_items, item_menu)
from traytrim.gui.theme import SPACE
from traytrim.gui.widgets import Notice, ShowMore, SimChip, label
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


PAGE_CARDS = MAX_CARDS  # cards built at a time
# Pictures kept in memory: ~600 x 248 x 248 px x 4 bytes, about 150 MB.
MAX_THUMBNAILS = 600


class HouseholdView(QWidget):
    """Empty, loading and error states, and the report for one household."""

    sims_changed = Signal(set)          # Sim ids to include
    exclusion_changed = Signal(set)     # TrayRefs left out of the export
    copies_chosen = Signal(object)  # {ResourceKey: package Path, or None for the default}
    export_requested = Signal()
    export_item_requested = Signal(object, object)  # LocatedRef, ItemName or None
    thumbnails_wanted = Signal(object, list)  # tray item (64-bit), refs of cards on screen

    SORTS = (("category", "Category"), ("size", "Size"), ("name", "Name"))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self._mode = "cards"
        self._sort = "category"
        self._query = ""  # as typed; matching ignores outer spaces
        self._names: dict[TrayRef, ItemName] = {}
        # Size order, fixed once worked out: leaving an item out mustn't move its card.
        self._size_rank: dict[TrayRef, int] | None = None
        self._plan_error: tuple[str, object] | None = None  # reason, retry
        self._expanded: set[str] = set()  # notices whose details are open
        self._search: QLineEdit | None = None
        self._list: PagedCcList | None = None
        self._pictures: dict[int, QImage] = {}  # tray item -> its own picture
        self._head: QHBoxLayout | None = None
        self._head_picture: Picture | None = None
        self._report: HouseholdReport | None = None
        self._plan: Plan | None = None
        self._gameplay: list = []
        self._cards: dict[int, list[CcCard]] = {}
        self._all_cards: list[CcCard] = []
        self._sections: list[Section] = []
        # Loaded thumbnails, the most recently shown last. Kept across households (going back
        # to one shows its pictures at once), up to MAX_THUMBNAILS: browsing many households
        # would otherwise keep every picture ever seen.
        self._thumbs: OrderedDict[int, QImage] = OrderedDict()
        # Thumbnails are loaded for the cards on screen (a big lot shows a page at a time):
        # asked for, and tried (asked and answered, with or without an image).
        self._thumbs_asked: set[int] = set()
        self._thumbs_tried: set[int] = set()
        self._card_limit = PAGE_CARDS  # cards on screen when there are more than a page
        self._groups: list[tuple[str, list[LocatedRef]]] = []
        self._matching = 0
        self._search_timer = QTimer(self, singleShot=True, interval=250)
        self._search_timer.timeout.connect(self._rebuild)
        self._theme_timer = QTimer(self, singleShot=True, interval=0)
        self._theme_timer.timeout.connect(self._rebuild)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.message_page, self.message_title, self.message_body = self._message_page()
        self.stack.addWidget(self.message_page)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        # Never sideways: anything too wide wraps or is shortened instead.
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.stack.addWidget(self.scroll)
        self.show_message("Nothing selected",
                          "Pick a household, lot or room on the left to see which CC it uses.")

    # States

    def _message_page(self):
        page = QWidget()
        page.setObjectName("content")
        page.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(page)
        outer.setContentsMargins(SPACE["xxl"], SPACE["xxl"], SPACE["xxl"], SPACE["xxl"])
        outer.addStretch(2)
        box = QWidget()
        box.setFixedWidth(440)  # a fixed width lets wrapped labels report the right height
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        title = label("", "headline", wrap=True)
        body = label("", "secondary", wrap=True)
        col.addWidget(title)
        col.addWidget(body)
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(box)
        centred.addStretch(1)
        outer.addLayout(centred)
        outer.addStretch(3)
        return page, title, body

    def show_message(self, title: str, body: str) -> None:
        self._report = None
        self._plan = None
        self._gameplay = []
        self.message_title.setText(title)
        self.message_body.setText(body)
        self.message_body.setVisible(bool(body))
        self.stack.setCurrentWidget(self.message_page)

    def show_report(self, report: HouseholdReport) -> None:
        keep_scroll = (self._report is not None
                       and self._report.item.instance == report.item.instance)
        position = self.scroll.verticalScrollBar().value() if keep_scroll else 0
        if not keep_scroll:
            self._card_limit = PAGE_CARDS
            self._query = ""
            self._expanded.clear()
            self._size_rank = None
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
        if self._sort == "size" and self._size_rank is None:
            self._rebuild()  # the first sizes: now the order can be worked out
        else:
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
        sizes = self._owner_sizes()
        for card in self._all_cards:
            card.set_size(sizes.get(card.ref.ref) if card.included else None)
        excluded = self._report.excluded
        for section in self._sections:
            section.set_size(sum(sizes.get(x.ref, 0) for x in section.refs
                                 if x.ref not in excluded))

    def set_thumbnails(self, instance: int, images: dict[int, QImage],
                       tried: set[int]) -> None:
        """Thumbnails loaded for the instances `tried`; those without one get the
        placeholder."""
        self._thumbs.update(images)
        self._thumbs_tried |= tried
        self._forget_old_thumbnails()
        if self._report is not None:  # whichever household is on screen: it may share them
            self._show_thumbnails()

    def _show_thumbnails(self) -> None:
        for part, cards in self._cards.items():
            if part in self._thumbs:
                self._thumbs.move_to_end(part)  # on screen: the last to be forgotten
            for card in cards:
                if part in self._thumbs:
                    card.thumb.set_image(self._thumbs[part])
                elif part in self._thumbs_tried:
                    card.thumb.set_missing()

    def _forget_old_thumbnails(self) -> None:
        """Drop the least recently shown pictures past MAX_THUMBNAILS, never one on screen. A
        dropped one is loaded again if its card comes back."""
        on_screen = set(self._cards)
        for part in list(self._thumbs):
            if len(self._thumbs) <= MAX_THUMBNAILS:
                break
            if part not in on_screen:
                del self._thumbs[part]
                self._thumbs_asked.discard(part)
                self._thumbs_tried.discard(part)

    def _ask_thumbnails(self) -> None:
        """Ask for the thumbnails of the cards on screen that nobody asked for yet."""
        wanted = [cards[0].ref for part, cards in self._cards.items()
                  if part not in self._thumbs_asked]
        if wanted and self._report is not None:
            self._thumbs_asked |= {r.ref.instance for r in wanted}
            self.thumbnails_wanted.emit(self._report.item.instance, wanted)

    def set_names(self, instance: int, names: dict[TrayRef, ItemName]) -> None:
        """Item names read from their packages arrive after the report is shown. Cards take
        them in place (and each section re-sorts); the list and the name sort are rebuilt."""
        self._names.update(names)
        if self._report is None or self._report.item.instance != instance or not names:
            return
        if self._mode == "list" or self._sort == "name" or self._paged:
            self._rebuild()
            return
        for card in self._all_cards:
            card.set_name(self._names.get(card.ref.ref))
        if self._sort == "category":
            for section in self._sections:
                section.reorder(lambda c: (item_label(c.ref), self._title(c.ref).casefold()))
        self._apply_filter()

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
        if not self._theme_timer.isActive():
            self._theme_timer.start()

    def _rebuild(self) -> None:
        """Build the page again (theme, view, sort or names changed), keeping the scroll
        position, the plan on screen and the keyboard focus: the search box (and its cursor),
        the card or the control that had it, so keyboard users don't lose their place."""
        if self._report is None:
            return
        position = self.scroll.verticalScrollBar().value()
        searching = self._search is not None and self._search.hasFocus()
        cursor = self._search.cursorPosition() if searching else 0
        focus = self.window().focusWidget()  # also right while the window isn't active
        page = self.scroll.widget()
        on_page = focus is not None and page is not None and page.isAncestorOf(focus)
        focused_ref = focus.ref.ref if on_page and isinstance(focus, CcCard) else None
        focused_name = focus.accessibleName() if on_page and not focused_ref else ""
        self._build()
        self._apply_plan()
        self.scroll.verticalScrollBar().setValue(position)
        if searching and self._search is not None:
            self._search.setFocus()
            self._search.setCursorPosition(cursor)
        elif focused_ref is not None:
            card = next((c for c in self._all_cards if c.ref.ref == focused_ref), None)
            if card is not None:
                card.setFocus()
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
        self.hero.show_working()
        col.addWidget(self.hero)

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
        bar.addWidget(self._view_toggle())
        col.addLayout(bar)
        col.addSpacing(SPACE["xs"])
        hint = ("Click an item to leave it out of the export, or right-click it to export it on "
                "its own." if self._mode == "cards"
                else "Switch to cards to choose what to leave out. Right-click an item to export "
                "it on its own.")
        col.addWidget(label(f"{self._source_line(summary)} {hint}", "secondary", wrap=True))
        col.addSpacing(SPACE["l"])

        self._search, self._list = None, None
        if summary.cc:
            tools = QHBoxLayout()
            tools.setSpacing(SPACE["m"])
            search = QLineEdit(self._query)  # as typed, trailing space and all
            search.setPlaceholderText("Search names and packages")
            search.setAccessibleName("Search the CC")
            search.setClearButtonEnabled(True)
            search.setFixedWidth(280)
            search.addAction(icons.icon("search", theme.current().text_tertiary, 16),
                             QLineEdit.ActionPosition.LeadingPosition)
            search.textChanged.connect(self._query_changed)
            self._search = search
            tools.addWidget(search)
            self.match_label = label("", "secondary")
            tools.addWidget(self.match_label)
            tools.addStretch(1)
            tools.addWidget(label("Sort by", "secondary"))
            tools.addWidget(self._sort_toggle())
            col.addLayout(tools)
            col.addSpacing(SPACE["l"])
            self.no_match = label("", "secondary", wrap=True)
            self.no_match.hide()
            col.addWidget(self.no_match)

        groups = self._sorted_groups(summary.cc)
        self._groups = groups
        self._cards, self._all_cards, self._sections = {}, [], []
        if not summary.cc:
            col.addWidget(label(f"This {r.noun} doesn't use any CC from your Mods folder.",
                                "secondary", wrap=True))
        elif self._mode == "cards":
            # Up to a page of cards; past that, the search picks what's built (not just shown).
            shown, full, whole = groups, {}, dict(groups)
            if self._paged:
                full = {t: [x for x in refs if self._matches(x)] for t, refs in groups}
                shown = first_items([(t, refs) for t, refs in full.items() if refs],
                                    self._card_limit)
            for title, refs in shown:
                cards = [CcCard(x, x.ref not in r.excluded, self._names.get(x.ref))
                         for x in refs]
                for card in cards:
                    card.toggled.connect(self._card_toggled)
                    card.menu_requested.connect(self._card_menu)
                    self._all_cards.append(card)
                    if card.ref.ref.kind in THUMB_TYPES:
                        self._cards.setdefault(card.ref.ref.instance, []).append(card)
                    else:
                        card.thumb.set_missing()
                section = Section(title, cards, refs=whole.get(title))
                if self._paged:
                    section.show_matching(len(full[title]))  # "3 of 40", as without pages
                self._sections.append(section)
                col.addWidget(section)
            if self._paged:
                self._matching = sum(len(refs) for refs in full.values())
                more = ShowMore(self._matching, self._show_more_cards, PAGE_CARDS)
                more.set_shown(len(self._all_cards))
                col.addWidget(more)
            self._show_thumbnails()
            self._ask_thumbnails()
        else:
            self._list = PagedCcList(
                len(r.sim_ids) > 1, r.excluded, self._names,
                lambda ref: self.export_item_requested.emit(ref, self._names.get(ref.ref)))
            col.addWidget(self._list)
        col.addStretch(1)
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(page)
        self._apply_filter()

    @property
    def _paged(self) -> bool:
        """More CC than one page of cards: they're built a page at a time."""
        return self._report is not None and len(self._summary.cc) > PAGE_CARDS

    def _show_more_cards(self) -> None:
        first_new = len(self._all_cards)
        self._card_limit += PAGE_CARDS
        self._rebuild()
        if first_new < len(self._all_cards):  # keyboard focus goes to where the list goes on
            card = self._all_cards[first_new]
            card.setFocus()
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(card, 0, SPACE["xl"]))

    # Search and sort

    def _title(self, ref: LocatedRef) -> str:
        return display_name(ref, self._names.get(ref.ref))

    def _sorted_groups(self, refs: list[LocatedRef]) -> list[tuple[str, list[LocatedRef]]]:
        if self._sort == "size":
            if self._size_rank is None and self._plan is not None:
                sizes = self._owner_sizes()
                ranked = sorted(refs, key=lambda x: (-sizes.get(x.ref, 0),
                                                     self._title(x).casefold()))
                self._size_rank = {x.ref: i for i, x in enumerate(ranked)}
            rank = self._size_rank or {}
            ordered = sorted(refs, key=lambda x: (rank.get(x.ref, len(rank)),
                                                  self._title(x).casefold()))
            return [("Largest first", ordered)]
        if self._sort == "name":
            return [("A to Z", sorted(refs, key=lambda x: self._title(x).casefold()))]
        return group_refs(refs, self._title)

    def _matches(self, ref: LocatedRef) -> bool:
        if not self._query.strip():
            return True
        name = self._names.get(ref.ref)
        haystack = " ".join([self._title(ref), name.creator if name else "",
                             name.raw if name else "", ref.used.rel if ref.used else "",
                             item_label(ref), card_caption(ref)]).casefold()
        return all(word in haystack for word in self._query.casefold().split())

    def _query_changed(self, text: str) -> None:
        self._query = text
        if self._mode == "cards" and self._paged:
            self._card_limit = PAGE_CARDS
            self._search_timer.start()  # cards are built for the matches: wait for a pause
        else:
            self._apply_filter()

    def _apply_filter(self) -> None:
        if self._search is None:
            return
        total = len(self._summary.cc)
        if self._list is not None:  # the matches, a page at a time
            self._list.set_groups([(t, [x for x in refs if self._matches(x)])
                                   for t, refs in self._groups])
            shown = self._list.total
        elif self._paged:  # only matching cards were built
            shown = self._matching
        else:
            shown = 0
            for section in self._sections:
                matching = 0
                for card in section.cards:
                    ok = self._matches(card.ref)
                    card.setVisible(ok)
                    matching += ok
                section.show_matching(matching)
                shown += matching
        query = self._query.strip()
        self.match_label.setText(f"{shown} of {total}" if query else "")
        self.no_match.setText(f"Nothing matches \u201c{self._query}\u201d. Search looks at "
                              "names, creators and packages.")
        self.no_match.setVisible(bool(query) and shown == 0)

    def _sort_toggle(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        for i, (key, text) in enumerate(self.SORTS):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self._sort == key)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else
                          "last" if i == len(self.SORTS) - 1 else "middle")
            b.setAccessibleName(f"Sort by {text.lower()}")
            b.clicked.connect(lambda _c=False, k=key: self._set_sort(k))
            group.addButton(b)
            row.addWidget(b)
        return box

    def _set_sort(self, key: str) -> None:
        if key != self._sort and self._report is not None:
            self._sort = key
            self._size_rank = None  # choosing Size orders by the sizes as they are now
            self._rebuild()

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
        return Notice(*args, expanded=key in self._expanded,
                      on_expand=lambda shown: (self._expanded.add(key) if shown
                                               else self._expanded.discard(key)), **kwargs)

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
            details = [(item_label(x), display_name(x, self._names.get(x.ref)),
                        x.used.rel if x.used else "") for x in s.cc if x.ref in r.overrides]
            self._add_notice(Notice(
                "info", "text_secondary",
                f"Your mods change {plural(n, 'EA item')} this {r.noun} uses",
                "Mostly to unlock or retune them, which rarely changes how it looks. Exported, "
                f"{'it' if n == 1 else 'they'}'d change the item everywhere in the recipient's "
                f"game, so {'it starts' if n == 1 else 'they start'} out of the export. To "
                "include one, click its card (marked Not exported).", details), "tray")
        for note in (r.contents.sims[0].notes if r.contents.sims else []):
            self._add_notice(Notice("info", "text_secondary", "Partly read", note), "tray")

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
                what = (display_name(ref, self._names.get(owner)) if ref is not None
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
            self._add_notice(Notice(
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
            self._add_notice(Notice(
                "info", "text_secondary",
                f"{plural(n, 'gameplay mod')} {'is' if n == 1 else 'are'} also needed",
                f"{who} {what} from these mods. They can't be trimmed, so they're not "
                "exported; the README lists them so the recipient can install them.",
                details), "plan")
        if plan.unreadable:
            n = len(plan.unreadable)
            details = [(restypes.name(k.type), Path(p).name, reason)
                       for k, p, reason in plan.unreadable]
            self._add_notice(Notice(
                "alert", "danger_text", f"{plural(n, 'file')} couldn't be read",
                "Their own references couldn't be followed, so something they need may be "
                "missing from the export.", details), "plan")

    # Interaction

    def _view_toggle(self) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        group = QButtonGroup(box)
        for i, (mode, text) in enumerate((("cards", "Cards"), ("list", "List"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(self._mode == mode)
            b.setProperty("kind", "segment")
            b.setProperty("position", "first" if i == 0 else "last")
            b.setAccessibleName(f"Show CC as {text.lower()}")
            b.clicked.connect(lambda _c=False, m=mode: self._set_mode(m))
            group.addButton(b)
            row.addWidget(b)
        return box

    def _set_mode(self, mode: str) -> None:
        if mode != self._mode and self._report is not None:
            self._mode = mode
            self._rebuild()

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

    def _card_menu(self, card: CcCard, pos) -> None:
        item_menu(self, pos, lambda: self.export_item_requested.emit(card.ref, card.name))

    def _card_toggled(self, ref: TrayRef, included: bool) -> None:
        r = self._report
        if r is None:
            return
        if included:
            r.excluded.discard(ref)
        else:
            r.excluded.add(ref)
        n_out = len(r.excluded & {x.ref for x in self._summary.cc})
        self.excluded_label.setText(f"{n_out} left out" if n_out else "")
        self._plan = None  # the shown plan no longer matches; nothing may export it
        self.hero.show_working()
        self.exclusion_changed.emit(set(r.excluded))

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(800, 600)


def _set_text_if_alive(button, text: str) -> None:
    try:
        button.setText(text)
    except RuntimeError:  # the notice was rebuilt meanwhile
        pass
