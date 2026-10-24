"""Widget behaviour that doesn't need a visible window (Qt's offscreen platform)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from traytrim.gui.gallery import ItemRole, TrayList, subtitle
from traytrim.settings import Settings
from traytrim.tray import TrayItem, TraySim


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_search_keeps_the_chosen_household(app):
    view = TrayList()
    items = [TrayItem(i, 1, name, sims=[TraySim(i, name, "")])
             for i, name in enumerate(["Smith", "Goth", "Gofer"])]
    view.source.set_items(items)
    picked = []
    view.item_selected.connect(picked.append)
    view.select_item(items[0])
    assert picked == [items[0]]

    view.set_filter("go")  # hides Smith: nothing else may be picked on the user's behalf
    assert picked == [items[0]]
    assert view.visible_count() == 2 and not view.currentIndex().isValid()

    view.set_filter("")  # Smith is back and still selected
    assert picked == [items[0]]
    assert view.currentIndex().data(ItemRole) is items[0]


def test_the_tray_list_sorts_and_keeps_the_choice(app):
    view = TrayList()
    items = [TrayItem(1, 1, "Smith", sims=[TraySim(1, "A", "")], modified=3.0),
             TrayItem(2, 1, "Goth", sims=[TraySim(2, "B", ""), TraySim(3, "C", "")],
                      modified=1.0),
             TrayItem(3, 1, "Gofer", sims=[TraySim(4, "D", "")], modified=2.0)]
    view.source.set_items(items)

    def order():
        return [view.proxy.index(r, 0).data(ItemRole).name for r in range(view.visible_count())]

    assert order() == ["Smith", "Gofer", "Goth"]  # newest first by default, as before
    view.select_item(items[1])
    picked = []
    view.item_selected.connect(picked.append)
    for key, expected in (("name", ["Gofer", "Goth", "Smith"]),
                          ("name_desc", ["Smith", "Goth", "Gofer"]),
                          ("newest", ["Smith", "Gofer", "Goth"]),
                          ("oldest", ["Goth", "Gofer", "Smith"]),
                          ("sims", ["Goth", "Gofer", "Smith"])):
        view.set_sort(key)
        assert order() == expected, key
        assert view.currentIndex().data(ItemRole) is items[1]  # still chosen
    assert picked == []  # re-sorting isn't a new choice



def test_gallery_shows_one_kind_at_a_time(app):
    view = TrayList()
    home = TrayItem(1, 1, "Goth", sims=[TraySim(1, "Bella", "Goth")])
    lot = TrayItem(2, 2, "Goth Manor", size=(40, 30), bedrooms=3)
    room = TrayItem(3, 3, "Nursery", size=(6, 4))
    view.source.set_items([home, lot, room])
    view.set_filter("", 1)
    assert [view.proxy.index(r, 0).data(ItemRole) for r in range(view.visible_count())] == [home]
    view.set_filter("goth", 2)
    assert view.visible_count() == 1 and view.proxy.index(0, 0).data(ItemRole) is lot
    assert subtitle(lot) == "40 \u00d7 30 \u00b7 3 bedrooms" and subtitle(home) == "Bella Goth"
    view.set_filter("goth", 3)
    assert view.visible_count() == 0


def test_settings_keep_only_known_tray_kinds(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"tray_kind": "lot", "theme": "neon"}', encoding="utf-8")
    assert Settings.load(path).tray_kind == "lot" and Settings.load(path).theme == "system"
    path.write_text('{"tray_kind": "garden"}', encoding="utf-8")
    assert Settings.load(path).tray_kind == "household"

def test_index_refresh_while_running_restarts_after_abort(app, tmp_path, monkeypatch):
    import time

    from traytrim.dbpf import PackageWriter, ResourceKey
    from traytrim.gui.index_status import IndexController, IndexStatus

    monkeypatch.setattr("traytrim.index.cache_dir", lambda: tmp_path / "cache")
    mods = tmp_path / "Mods"
    mods.mkdir()
    for i in range(40):
        with PackageWriter(mods / f"p{i:02}.package") as w:
            w.add(ResourceKey(0x034AEECB, 0, i + 1), b"x" * 100)
    other = tmp_path / "Other" / "Mods"
    other.mkdir(parents=True)
    with PackageWriter(other / "only.package") as w:
        w.add(ResourceKey(0x034AEECB, 0, 999), b"y")

    controller = IndexController(IndexStatus(lambda: None, lambda: None))
    results = []
    controller.result_ready.connect(results.append)
    controller.refresh(mods, None)
    controller.refresh(other, None)  # supersedes the first one
    end = time.time() + 20
    while not results and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    assert len(results) == 1 and results[0].mods_packages == 1


def test_history_dates():
    from datetime import datetime

    from traytrim.gui.history_dialog import when_text

    now = datetime(2026, 10, 22, 23, 30)
    assert when_text("2026-10-22T23:16:11", now) == "Today, 23:16"
    assert when_text("2026-10-21T09:02:00", now) == "Yesterday, 09:02"
    assert when_text("2026-10-03T14:00:00", now) == "3 Oct 2026, 14:00"
    assert when_text("not a date", now) == ""


def test_batch_dialog_picking(app, tmp_path):
    from traytrim.gui.batch_dialog import BatchDialog

    lots = [TrayItem(i, 2, f"Lot {i}", size=(20, 20)) for i in range(1, 3)]
    home = TrayItem(9, 1, "Goth", sims=[TraySim(9, "Bella", "Goth")])
    dialog = BatchDialog(lots + [home], {}, Settings(output_dir=str(tmp_path)), None, None,
                         preselect=2)
    # Households first, under their own heading
    assert [r.item.name for r in dialog._rows] == ["Goth", "Lot 1", "Lot 2"]
    assert [h.text() for h, _t in dialog._headings] == ["Households", "Lots"]
    assert [r.isChecked() for r in dialog._rows] == [False, False, True]
    assert dialog.export_button.text() == "Export 1"
    dialog.together_chip.setChecked(True)  # together needs at least two, and says so
    assert not dialog.export_button.isEnabled()
    assert dialog.count_label.text() == "Pick at least two to export together"
    dialog._rows[0].setChecked(True)
    assert dialog.export_button.isEnabled() and dialog.export_button.text() == "Export together"
    dialog._set_all(True)
    assert dialog.count_label.text() == "3 of 3 selected"
    dialog.together_chip.setChecked(False)
    dialog._set_all(False)
    assert not dialog.export_button.isEnabled() and dialog.count_label.text() == "3 items"

    # Search and kind: Select all picks only what's shown; what's picked stays picked.
    dialog.search.setText("lot 1")
    assert [r.isHidden() for r in dialog._rows] == [True, False, True]
    dialog._set_all(True)
    assert [r.isChecked() for r in dialog._rows] == [False, True, False]
    dialog.search.setText("")
    dialog.kind_button.set_options(dialog.kind_button._options, "1")
    dialog._filter()
    assert [r.isHidden() for r in dialog._rows] == [False, True, True]
    assert dialog._rows[1].isChecked()
    dialog.search.setText("nobody")
    assert dialog.no_match.isVisibleTo(dialog)
    dialog.deleteLater()


def test_items_with_the_same_name_say_when_they_were_saved(app):
    import time as time_module

    from traytrim.gui.describe import saved_text

    now = time_module.mktime((2026, 10, 24, 12, 0, 0, 0, 0, -1))
    a = TrayItem(1, 1, "Angel", sims=[TraySim(1, "Victoria", "Angel")],
                 modified=time_module.mktime((2026, 10, 3, 9, 0, 0, 0, 0, -1)))
    b = TrayItem(2, 1, "angel ", sims=[TraySim(2, "Victoria", "Angel")],
                 modified=time_module.mktime((2025, 1, 5, 9, 0, 0, 0, 0, -1)))
    c = TrayItem(3, 1, "Nora", sims=[TraySim(3, "Nora", "Hayes")])
    assert saved_text(a, now) == "Saved 3 Oct, 09:00"
    assert saved_text(b, now) == "Saved 5 Jan 2025, 09:00"
    view = TrayList()
    view.source.set_items([a, b, c])
    assert view.source.subtitle(a).startswith("Saved ")
    assert view.source.subtitle(a).endswith("Victoria Angel")
    assert view.source.subtitle(c) == "Nora Hayes"  # a name of its own: no date


def test_detail_view_search_sort_and_names(app, tmp_path):
    from test_plan import located, world
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.names import ItemName
    from traytrim.tray import HouseholdContents, RefKind, SimContents

    gen = world.__wrapped__(tmp_path)
    index, k = next(gen)
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"])]
    for r in refs:
        r.usage = {1: {"Everyday outfit 1"}}
        r.names = {1: "Camille Moreau"}
    contents = HouseholdContents("Moreau", [SimContents(TraySim(1, "Camille", "Moreau"))], 2)
    view = HouseholdView()
    view.show_report(HouseholdReport(TrayItem(5, 1, "Moreau"), contents, refs, {1}))
    b = view.browser
    assert len(b.shown()) == 2
    view.set_names(5, {refs[0].ref: ItemName("Long Waves", "Me", "Me_yfHair_LongWaves")})
    assert b.state.title(refs[0]) == "Long Waves"
    # The card also says which package it's from: a part's own name is often an EA one.
    assert "Long Waves, hair." in b.state.accessible(refs[0])
    assert b.state.subtitle(refs[1]) == ""  # named after its package already
    assert b.state.subtitle(refs[0]) == "hair"
    named = b.state.names[refs[0].ref]
    b.state.names[refs[0].ref] = ItemName("Hair", "", "Hair")  # says the same, any case
    assert b.state.subtitle(refs[0]) == ""
    b.state.names[refs[0].ref] = named
    b.search.setText("waves")
    assert b.shown() == [refs[0]] and b.match_label.text() == "1 of 2 shown"
    b.search.setText("nothing like this")
    assert b.no_match.isVisibleTo(view)
    b.search.setText("")
    page = view.scroll.widget()
    b.set_sort("name")
    assert [b.state.title(r) for r in b.shown()] == ["Long Waves", "recolor"]
    b.set_sort("name_desc")
    assert [b.state.title(r) for r in b.shown()] == ["recolor", "Long Waves"]
    assert view.scroll.widget() is page and view.browser is b  # sorting rebuilt nothing else
    group = refs[0].ref.group
    assert (group, f"{group} (2)") in b.category_button._options
    b._category_chosen(group)
    assert len(b.shown()) == 2  # both are in it
    b._category_chosen("no such category")
    assert b.shown() == [] and b.no_match.isVisibleTo(view)
    b._category_chosen("all")

    # Sorted by size, the order stays put when an item is left out and the plan comes back.
    from traytrim.plan import build_plan
    b.set_sort("size")
    view.set_plan(5, build_plan(refs, index))
    order = [r.ref for r in view.browser.shown()]
    view.browser._activated(view.browser.shown()[0])  # leave the largest out
    view.set_plan(5, build_plan(refs, index, excluded={order[0]}))
    assert [r.ref for r in view.browser.shown()] == order
    b._show_chosen("left_out")
    assert [r.ref for r in b.shown()] == [order[0]]
    b._show_chosen("all")

    # A plan error survives a rebuild (a theme change).
    view.show_plan_error(5, "Disk on fire", lambda: None)
    view._rebuild()
    assert view.hero.number.text() == "Couldn't work out the export"
    view.deleteLater()
    next(gen, None)


def test_mods_list_search_and_choice(app, tmp_path):
    from traytrim.gui.mods_list import ModsList, package_subtitle
    from traytrim.index import ModsPackageInfo

    packages = [ModsPackageInfo(tmp_path / "a.package", "Merged/zz_Hair.package", 0, 2_000_000,
                                0, 40, 12),
                ModsPackageInfo(tmp_path / "b.package", "Walls.package", 1, 500, 0, 2, 1)]
    view = ModsList()
    picked = []
    view.package_selected.connect(picked.append)
    view.set_packages(packages)
    assert package_subtitle(packages[0]) == "Merged · 1.9 MB · 12 items"
    assert view.select_path(packages[1].path) and picked == [packages[1]]
    view.set_filter("merged hair")
    assert view.visible_count() == 1 and picked == [packages[1]]  # a search isn't a choice
    view.set_filter("")
    assert view.currentIndex().data(Qt.ItemDataRole.UserRole + 1) is packages[1]


def test_package_view_shows_what_needs_it(app, tmp_path):
    from PySide6.QtTest import QTest

    from test_plan import CC, casp_bytes, write
    from traytrim import restypes
    from traytrim.dbpf import ResourceKey
    from traytrim.depindex import DependencyIndex
    from traytrim.gui.package_view import PackageView, users_text
    from traytrim.index import ResourceIndex
    from traytrim.pkgreport import dependents, package_report

    mods = tmp_path / "Mods"
    mesh = ResourceKey(restypes.GEOM, CC, 0x201)
    recolor = ResourceKey(restypes.CASP, CC, 0x200)
    write(mods / "mesh.package", [(mesh, b"mesh")])
    write(mods / "recolor.package", [(recolor, casp_bytes([mesh]))])
    home = TrayItem(1, 1, "Moreau")
    lot = TrayItem(2, 2, "Seaside")
    assert users_text([home, lot]) == "Used by 1 household and 1 lot"
    assert users_text([]) == "Nothing in your Tray uses it"
    with ResourceIndex(tmp_path / "index.sqlite") as ix, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        ix.update(mods, None)
        deps.update(ix)
        report = package_report(mods / "mesh.package", ix)
        broken = dependents(report, deps, ix)
        recolors = package_report(mods / "recolor.package", ix)
    view = PackageView()
    view.resize(900, 700)
    view.show()
    view.show_report(report)
    assert view.verdict.text.text() == "Working out what uses it"
    view.set_usage(report.path, [home], broken)
    assert view.verdict.text.text() == "Used by 1 household"
    assert view.notices.isVisibleTo(view) and view.users_box.isVisibleTo(view)
    view.set_usage(tmp_path / "other.package", [], [])  # for another package: ignored
    assert view.verdict.text.text() == "Used by 1 household"

    wanted, asked = [], []
    view.export_item_requested.connect(lambda ref, name: wanted.append(ref))
    view.thumbnails_wanted.connect(lambda path, refs: asked.extend(refs))
    view.show_report(recolors)
    assert len(view.browser.shown()) == 1
    grid = view.browser.sections[0].grid
    view.scroll.ensureWidgetVisible(grid)
    app.processEvents()
    QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton,
                     pos=grid.visualRect(grid.model().index(0, 0)).center())
    assert [r.ref.instance for r in wanted] == [recolor.instance]
    view.grab()  # painting the card asks for its thumbnail
    QTest.qWait(60)
    assert [r.ref.instance for r in asked] == [recolor.instance]
    view.deleteLater()


def test_received_view_states(app, tmp_path):
    from traytrim.gui.received_view import ReceivedView, verdict_text
    from traytrim.received import Received
    from traytrim.verify import Check, Verification

    ok = Verification([Check("The zip is intact", True, "")])
    r = Received(tmp_path / "Moreau.zip", 1000, tmp_path, ok, packs_missing=["EP09"])
    assert verdict_text(r) == "Nothing to install"
    damaged = Received(tmp_path / "x.zip", 10, tmp_path,
                       Verification([Check("The zip is intact", False, "1 file is damaged.")]))
    assert verdict_text(damaged) == "The zip is damaged"
    view = ReceivedView()
    closed = []
    view.closed.connect(lambda: closed.append(True))
    view.show_working(r.zip_path, "copy", 1, 2)
    assert view.verdict.text.text() == "Checking the zip"
    view.show_received(damaged)
    assert view.verdict.text.text() == "The zip is damaged"
    view.show_error(r.zip_path, "Permission denied")
    assert view.received is None
    view.deleteLater()


def test_no_task_starts_once_the_app_is_quitting(app, monkeypatch):
    from traytrim.gui import tasks

    monkeypatch.setattr(tasks, "_accepting", False)  # as after tasks.shutdown()
    task = tasks.BackgroundTask(lambda _t: 1 / 0)
    task.start()
    assert not task.running and task not in tasks._live


def test_labels_and_tooltips_show_names_as_plain_text(app):
    from PySide6.QtGui import Qt as GuiQt

    from traytrim.gui.widgets import ElidedLabel, label

    name = "<img src='file:///etc/hosts'><b>Soft Bob</b>"  # from someone's CC package
    assert label(name).textFormat() == Qt.TextFormat.PlainText
    assert ElidedLabel(name).textFormat() == Qt.TextFormat.PlainText
    assert GuiQt.mightBeRichText(name)  # what PlainToolTips escapes before showing


def test_long_lists_are_all_there_and_only_cards_on_screen_ask_for_pictures(app):
    from PySide6.QtTest import QTest

    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.gui.widgets import PAGE_DETAILS, Notice
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    notice = Notice("info", "text_secondary", "Lots", "body",
                    [(f"row {i}", "", "") for i in range(PAGE_DETAILS * 2 + 5)], expanded=True)
    assert notice._shown == PAGE_DETAILS and notice._more.isVisibleTo(notice)
    notice._add_rows()
    notice._add_rows()
    assert notice._shown == PAGE_DETAILS * 2 + 5 and not notice._more.isVisibleTo(notice)

    # A lot with 700 CC items: every one is there at once (no pages), and only the cards that
    # are painted ask for their thumbnails.
    many = 700
    lot_refs = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xBBBB000000000000 + i),
                               usage={1: {"Placed"}}) for i in range(many)])
    wanted = []
    view = HouseholdView()
    view.resize(1000, 800)
    view.thumbnails_wanted.connect(lambda _i, refs: wanted.extend(refs))
    contents = HouseholdContents("Seaside", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
    view.show_report(HouseholdReport(TrayItem(0x0010175A327A0833, 2, "Seaside"), contents,
                                     lot_refs, {1}))
    view.show()
    b = view.browser
    assert len(b.shown()) == many and len(b.sections) == 1
    view.grab()
    QTest.qWait(60)
    assert 0 < len(wanted) < many
    b.search.setText(f"{lot_refs[-1].ref.instance:x}")
    assert b.shown() == [lot_refs[-1]]
    assert b.sections[0].count_label.text() == f"1 of {many}"
    view.deleteLater()


def test_thumbnails_kept_in_memory_are_capped(app, monkeypatch):
    from PySide6.QtGui import QImage

    from traytrim.gui import household_view
    from traytrim.gui.household_view import HouseholdView

    monkeypatch.setattr(household_view, "MAX_THUMBNAILS", 3)
    view = HouseholdView()
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    view.set_thumbnails(0, {i: image for i in (1, 2, 3, 4, 5)}, {1, 2, 3, 4, 5})
    assert list(view._thumbs) == [3, 4, 5]  # the oldest went
    assert 1 not in view._thumbs_tried  # loaded again if it's shown again
    view.deleteLater()


def test_the_keyboard_works_the_cards(app):
    from PySide6.QtTest import QTest

    from traytrim.gui.cc_view import CardGrid
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    refs = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xBBBB000000000000 + i),
                           usage={1: {"Placed"}}) for i in range(5)])
    view = HouseholdView()
    view.show()
    sent = []
    view.exclusion_changed.connect(sent.append)
    contents = HouseholdContents("Seaside", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
    view.show_report(HouseholdReport(TrayItem(0x0010175A327A0833, 2, "Seaside"), contents, refs,
                                     {1}))
    grid = view.browser.sections[0].grid
    grid.setFocus()
    grid.setCurrentIndex(grid.model().index(0, 0))  # where the keyboard is
    first = view.browser.shown()[0]
    QTest.keyClick(grid, Qt.Key.Key_Space)
    assert sent[-1] == {first.ref}
    QTest.keyClick(grid, Qt.Key.Key_Space)
    assert sent[-1] == set()
    view.browser.set_sort("name")  # a sort keeps the focus in the cards
    assert isinstance(view.focusWidget(), CardGrid)
    view.deleteLater()


def test_the_mods_sort_is_remembered(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"mods_sort": "size"}', encoding="utf-8")
    assert Settings.load(path).mods_sort == "size"
    path.write_text('{"mods_sort": "colour"}', encoding="utf-8")
    assert Settings.load(path).mods_sort == "name"


def test_install_steps_use_the_real_sims_folder(app, tmp_path):
    from traytrim.gui.received_view import ReceivedView
    from traytrim.received import Received, ReceivedItem
    from traytrim.verify import Check, Verification

    r = Received(tmp_path / "Moreau.zip", 1000, tmp_path,
                 Verification([Check("The zip is intact", True, "")]),
                 items=[ReceivedItem(TrayItem(1, 1, "Moreau"), "household")],
                 packages=["Moreau_TrayTrim.package"])
    view = ReceivedView()
    view.user_dir = tmp_path / "OneDrive" / "Documents" / "Electronic Arts" / "The Sims 4"
    view.show_received(r)
    from traytrim.gui.widgets import TextLabel

    notice = view._install_notice()
    body = notice.findChildren(TextLabel)
    assert any(str(view.user_dir / "Tray") in w.text().replace("​", "") for w in body)
    view.deleteLater()


def _cc(refs):
    for r in refs:  # in Mods, so they count as CC
        r.mods = [type("Hit", (), {"rel": f"Objects/obj_{r.ref.instance:x}.package",
                                   "path": None, "load_order": 0})()]
    return refs


def test_thumbnails_reach_the_household_on_screen_and_counts_cover_the_search(app):
    from PySide6.QtGui import QImage

    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    def lot(instance, refs):
        contents = HouseholdContents("Lot", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
        return HouseholdReport(TrayItem(instance, 2, "Lot"), contents, refs, {1})

    shared = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xDDDD000000000001), usage={1: {"Placed"}})])
    view = HouseholdView()
    view.show_report(lot(0x10000000001, shared))   # asks for the shared object's thumbnail
    view.show_report(lot(0x10000000002, shared))   # another lot with the same object
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    view.set_thumbnails(0x10000000001, {shared[0].ref.instance: image}, {shared[0].ref.instance})
    assert view.browser.state.thumbnail(shared[0]) is image  # the first lot's answer shows here

    many = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xEEEE000000000000 + i), usage={1: {"Placed"}})
                for i in range(310)])
    view.show_report(lot(0x10000000003, many))
    view.browser.search.setText(f"{many[0].ref.instance:x}")
    [section] = view.browser.sections
    assert section.count_label.text() == f"1 of {len(many)}"
    view.deleteLater()


def test_a_mods_only_check_keeps_the_games_skipped_packages(app, tmp_path):
    from traytrim.gui.index_status import IndexController, IndexStatus
    from traytrim.index import UpdateResult

    controller = IndexController(IndexStatus(lambda: None, lambda: None))
    game = tmp_path / "Game"
    controller.last_result = UpdateResult(failed=[(str(game / "Data" / "broken.package"), "bad")])
    controller._game_root, controller._game_skipped = game, True
    controller._on_finished(UpdateResult(failed=[(str(tmp_path / "Mods" / "x.package"), "bad")]))
    assert [f[0] for f in controller.last_result.failed] == [
        str(game / "Data" / "broken.package"), str(tmp_path / "Mods" / "x.package")]


def test_the_support_link_opens_buy_me_a_coffee(app, monkeypatch):
    from traytrim import SUPPORT_URL
    from traytrim.gui import system
    from traytrim.gui.widgets import SupportLine

    opened = []
    monkeypatch.setattr(system, "open_url", opened.append)
    line = SupportLine()
    from traytrim.gui.widgets import CoffeeButton

    [link] = line.findChildren(CoffeeButton)
    link.click()
    assert opened == [SUPPORT_URL] == ["https://buymeacoffee.com/stixe"]
    line.deleteLater()


def test_thumbnails_load_from_packages(app, tmp_path):
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    from test_plan import write
    from traytrim import restypes
    from traytrim.dbpf import ResourceKey
    from traytrim.gui.images import load_thumbnails
    from traytrim.index import ResourceIndex

    image = QImage(40, 40, QImage.Format.Format_RGB32)
    image.fill(QColor(1, 2, 3))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    write(tmp_path / "Mods" / "hair.package", [
        (ResourceKey(restypes.THUM, 1, 0xAB), bytes(data)),
        (ResourceKey(restypes.THUM, 1, 0xCD), b"not an image")])
    with ResourceIndex(tmp_path / "index.sqlite") as index:
        index.update(tmp_path / "Mods", None)
        images = load_thumbnails(index, [0xAB, 0xCD, 0xEF], restypes.THUM, 32)
    assert set(images) == {0xAB} and images[0xAB].width() == 32


def test_history_check_opens_the_zip(app, tmp_path, monkeypatch):
    import json

    from traytrim.gui.history_dialog import HistoryDialog

    zip_path = tmp_path / "Moreau_TrayTrim.zip"
    zip_path.write_bytes(b"zip")
    monkeypatch.setattr("traytrim.export.config_dir", lambda: tmp_path / "config")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "history.json").write_text(json.dumps([
        {"time": "2026-10-23T10:00:00", "name": "Moreau", "kind": "household",
         "zip": str(zip_path), "bytes": 3, "verified": True},
        {"time": "2026-10-22T10:00:00", "name": "Gone", "kind": "lot",
         "zip": str(tmp_path / "gone.zip"), "bytes": 3, "verified": True}]), encoding="utf-8")
    opened = []
    dialog = HistoryDialog(opened.append)
    assert [r.check_button.isEnabled() for r in dialog.rows] == [True, False]  # one is gone
    dialog.rows[0].check_button.click()
    assert opened == [zip_path] and dialog.result() == dialog.DialogCode.Accepted
    dialog.deleteLater()


def test_exporting_one_item_asks_only_what_applies(app, tmp_path):
    from unittest.mock import MagicMock

    from traytrim.gui.export_dialog import ExportDialog, ExportRequest

    plan = MagicMock(export_size=2_100_000, resources={1: 1, 2: 2}, source_packages={1})
    user = tmp_path / "The Sims 4"
    (user / "Mods").mkdir(parents=True)
    dialog = ExportDialog(ExportRequest(None, None, [], plan, [], set(), "Soft_Bob", user,
                                        item_name="Soft Bob"), Settings())
    texts = [w.text().replace("​", "") for w in dialog.findChildren(QLabel)]
    assert "Export Soft Bob" in texts
    assert any("this item and everything it needs: 2 files from 1 CC package" in t
               for t in texts)
    assert dialog.kit_chip.isHidden()  # no tray files: no in-game test kit
    dialog.out_dir = user / "Mods"     # never into the Sims 4 folder
    dialog._start()
    assert "never writes to" in dialog.options_error.text()
    dialog.deleteLater()


def test_the_list_ticks_and_sorts(app, tmp_path):
    from test_plan import located, world
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.names import ItemName
    from traytrim.tray import HouseholdContents, RefKind, SimContents

    gen = world.__wrapped__(tmp_path)
    index, k = next(gen)
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"])]
    contents = HouseholdContents("Moreau", [SimContents(TraySim(1, "Camille", "Moreau"))], 2)
    view = HouseholdView()
    sent = []
    view.exclusion_changed.connect(sent.append)
    view.show_report(HouseholdReport(TrayItem(5, 1, "Moreau"), contents, refs, {1}))
    view.set_names(5, {refs[0].ref: ItemName("Long Waves", "Me", "Me_yfHair_LongWaves")})
    b = view.browser
    b.set_view("list")
    table = b.table
    assert table is not None and b.sections == []
    assert table.source.columns == ["Name", "Type", "Package", "Used in", "Notes"]
    first = table.source.index(0, 0)
    assert first.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    table.clicked.emit(first)  # the tick
    assert sent[-1] == {first.data(Qt.ItemDataRole.UserRole + 1).ref}
    assert b.table.source.index(0, 4).data() == "Not exported"
    b.table._heading_clicked(0)  # Name: A to Z, then Z to A
    assert b.sort == "name"
    b.table._heading_clicked(0)
    assert b.sort == "name_desc"
    assert [b.state.title(r) for r in b.shown()] == ["recolor", "Long Waves"]
    view.deleteLater()
    next(gen, None)


def test_include_all_and_leave_all_out(app, tmp_path):
    from test_plan import located, world
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.names import ItemName
    from traytrim.tray import HouseholdContents, RefKind, SimContents

    gen = world.__wrapped__(tmp_path)
    index, k = next(gen)
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"])]
    contents = HouseholdContents("Moreau", [SimContents(TraySim(1, "Camille", "Moreau"))], 2)
    view = HouseholdView()
    sent = []
    view.exclusion_changed.connect(sent.append)
    view.show_report(HouseholdReport(TrayItem(5, 1, "Moreau"), contents, refs, {1}))
    view.set_names(5, {refs[0].ref: ItemName("Long Waves", "Me", "Me_yfHair_LongWaves")})
    b = view.browser
    buttons = {btn.text(): btn for btn in b.all_buttons}

    buttons["Leave all out"].click()
    assert sent[-1] == {r.ref for r in refs}
    assert not any(b.state.included(r) for r in refs)
    assert view.excluded_label.text() == "2 left out"
    buttons["Include all"].click()
    assert sent[-1] == set() and all(b.state.included(r) for r in refs)

    # While searching, only the matches.
    b.search.setText("waves")
    buttons["Leave all out"].click()
    assert sent[-1] == {refs[0].ref}
    assert [b.state.included(r) for r in refs] == [False, True]
    count = len(sent)
    buttons["Leave all out"].click()  # nothing changes: nothing is sent
    assert len(sent) == count

    # The same in the list.
    b.search.setText("")
    b.set_view("list")
    buttons["Include all"].click()
    assert sent[-1] == set() and b.table is not None
    buttons["Leave all out"].click()
    assert sent[-1] == {r.ref for r in refs}
    assert all(b.table.source.index(i, 0).data(Qt.ItemDataRole.CheckStateRole)
               == Qt.CheckState.Unchecked for i in range(2))
    view.deleteLater()
    next(gen, None)


def test_waiting_shows_a_loader(app):
    from traytrim.gui.household_view import HouseholdView
    from traytrim.gui.package_view import PackageView

    view = HouseholdView()
    view.show_message("Seaside", "Reading its objects and build items.", loading=True)
    assert view.message_page.spinner.isVisibleTo(view)
    view.show_message("Nothing selected", "Pick a household.")
    assert not view.message_page.spinner.isVisibleTo(view)
    packages = PackageView()
    packages.show_progress("Working out what uses it", "Reading: 3 of 10 packages.", 3, 10)
    panel = packages.message_page
    assert panel.spinner.isVisibleTo(packages) and panel.bar.isVisibleTo(packages)
    assert panel.bar.value() == 3 and panel.bar.maximum() == 10
    packages.clear_progress()
    assert not panel.spinner.isVisibleTo(packages)
    view.deleteLater()
    packages.deleteLater()


def test_the_window_always_offers_buy_me_a_coffee(app, tmp_path, monkeypatch):
    from traytrim import SUPPORT_URL
    from traytrim.gui import main_window, system

    opened = []
    monkeypatch.setattr(system, "open_url", opened.append)
    monkeypatch.setattr(main_window.paths, "resolve_folders",
                        lambda *_a: main_window.paths.Sims4Folders(None, None))
    win = main_window.MainWindow(Settings())
    from traytrim.gui.widgets import CoffeeButton, FooterAction

    [link] = win.statusBar().findChildren(CoffeeButton)
    assert link.text() == "Buy me a coffee" and link.sizeHint().height() < 32
    tiles = [a.text() for a in win._sidebar.findChildren(FooterAction)]
    assert tiles == ["Export several", "Check a zip", "History"]
    assert not win.batch_button.isEnabled() and "once the index" in win.batch_button.toolTip()
    link.click()
    assert opened == [SUPPORT_URL]
    assert win._check_updates is False  # tests never ask GitHub
    win.deleteLater()


def test_the_mods_list_sorts_and_shows_kinds(app, tmp_path):
    from traytrim import restypes
    from traytrim.gui.mods_list import ModsList
    from traytrim.index import ModsPackageInfo

    packages = [
        ModsPackageInfo(tmp_path / "a.package", "Hair.package", 0, 2_000, 30, 5, 1,
                        {restypes.CASP: 1}),
        ModsPackageInfo(tmp_path / "b.package", "Sofa.package", 1, 9_000, 10, 4, 2,
                        {restypes.OBJECT_DEFINITION: 2}),
        ModsPackageInfo(tmp_path / "c.package", "Tuning.package", 2, 500, 20, 9, 0, {}),
    ]
    view = ModsList()
    view.set_packages(packages)

    def order():
        return [view.proxy.index(r, 0).data() for r in range(view.visible_count())]

    assert order() == ["Hair", "Sofa", "Tuning"]  # load order
    view.set_sort("size_asc")
    assert order() == ["Tuning", "Hair", "Sofa"]
    view.set_sort("newest")
    assert order() == ["Hair", "Tuning", "Sofa"]
    view.set_sort("items")
    assert order()[0] == "Sofa"
    view.set_show("objects")
    assert order() == ["Sofa"]
    view.set_show("none")
    assert order() == ["Tuning"]
    view.set_show("all")
    view.set_filter("ha")
    assert order() == ["Hair"]
    view.deleteLater()


def _household(tmp_path, n_sims=1):
    from test_plan import located, world
    from traytrim.gui.household_view import HouseholdReport
    from traytrim.tray import HouseholdContents, RefKind, SimContents

    gen = world.__wrapped__(tmp_path)
    index, k = next(gen)
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"])]
    sims = [SimContents(TraySim(i + 1, f"Sim {i}", "Moreau")) for i in range(n_sims)]
    contents = HouseholdContents("Moreau", sims, 2)
    return gen, index, refs, HouseholdReport(TrayItem(5, 1, "Moreau"), contents, refs,
                                              {s.sim.sim_id for s in sims})


def test_names_that_arrive_late_put_each_section_in_order(app, tmp_path):
    from traytrim.gui.household_view import HouseholdView
    from traytrim.names import ItemName

    gen, _index, refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    b = view.browser
    assert b.sort == "category"
    view.set_names(5, {refs[0].ref: ItemName("Zz last", "", "Zz last"),
                       refs[1].ref: ItemName("Aa first", "", "Aa first")})
    assert [b.state.title(r) for r in b.shown()] == ["Aa first", "Zz last"]
    view.deleteLater()
    next(gen, None)


def test_the_list_gets_sizes_when_the_plan_comes_and_filters_survive_a_rebuild(app, tmp_path):
    from traytrim.gui.household_view import HouseholdView
    from traytrim.plan import build_plan

    gen, index, refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    view.browser.set_view("list")
    assert "Size" not in view.browser.table.source.columns  # no plan yet
    view.set_plan(5, build_plan(refs, index))
    assert "Size" in view.browser.table.source.columns
    view.browser.search.setText("hair")
    view.browser._show_chosen("copies")
    view._rebuild()  # a theme change
    assert view.browser.filters() == ("hair", "copies", "all")
    view.show_report(report)  # the same item again (a Sim chosen)
    assert view.browser.search.text() == "hair"
    view.deleteLater()
    next(gen, None)


def test_a_remembered_size_sort_shows_before_the_plan(app, tmp_path):
    from traytrim.gui.household_view import HouseholdView

    gen, _index, _refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    view.browser.set_sort("size")
    view.show_report(report)  # built again, before its plan
    assert view.browser.sort == "size" and view.browser.sort_button.current == "size"
    assert "Largest first" in view.browser.sort_button.text()
    view.deleteLater()
    next(gen, None)


def test_leaving_an_item_out_keeps_the_keyboard_on_it(app, tmp_path):
    from PySide6.QtTest import QTest

    from traytrim.gui.household_view import HouseholdView

    gen, _index, _refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show()
    view.show_report(report)
    b = view.browser
    b.set_sort("name")
    grid = b.sections[0].grid
    second = b.shown()[1]
    grid.setFocus()
    grid.setCurrentIndex(grid.model().index(1, 0))
    QTest.keyClick(grid, Qt.Key.Key_Space)
    assert not b.state.included(second)
    b.search.setText("")  # a refill: the focus stays on the same card
    b.refresh()
    focused = view.focusWidget()
    assert focused.currentIndex().data(Qt.ItemDataRole.UserRole + 1) is second
    view.deleteLater()
    next(gen, None)


def test_a_click_in_a_packages_list_exports_like_a_card(app):
    from traytrim.gui.cc_view import EXPORT, CcBrowser, CcState
    from traytrim.resolve import LocatedRef
    from traytrim.tray import RefKind, TrayRef

    refs = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xABAB000000000001))])
    browser = CcBrowser(refs, CcState(EXPORT, False), view="list", usage=False)
    exported = []
    browser.item_activated.connect(exported.append)
    browser.table.clicked.emit(browser.table.source.index(0, 2))  # the Package column
    assert exported == refs
    assert [k for k, _t in browser._sorts()] == ["category", "name", "name_desc", "package"]
    browser.deleteLater()


def test_sort_and_show_choices_are_the_ones_settings_keep():
    from traytrim import settings
    from traytrim.gui.gallery import HOUSEHOLD_SORTS, LOT_SORTS
    from traytrim.gui.mods_list import MODS_SHOWS, MODS_SORTS

    assert {o[0] for o in HOUSEHOLD_SORTS + LOT_SORTS} == set(settings.TRAY_SORTS)
    assert [o[0] for o in MODS_SORTS] == list(settings.MODS_SORTS)
    assert [o[0] for o in MODS_SHOWS] == list(settings.MODS_SHOWS)


def test_menu_buttons_read_left_to_right(app):
    from traytrim.gui.widgets import MenuButton

    b = MenuButton("Category", [("all", "All (1,234)")], "all")
    assert b.text() == "‎Category: All (1,234)‎"
    b.deleteLater()


def _lot_view(n, width=1000, height=600):
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    refs = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xBBBB000000000000 + i),
                           usage={1: {"Placed"}}) for i in range(n)])
    view = HouseholdView()
    view.resize(width, height)
    view.show()
    contents = HouseholdContents("Seaside", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
    view.show_report(HouseholdReport(TrayItem(0x0010175A327A0833, 2, "Seaside"), contents, refs,
                                     {1}))
    return view, refs


def test_every_card_row_fits_at_any_width(app):
    from PySide6.QtTest import QTest

    view, refs = _lot_view(25)
    grid = view.browser.sections[0].grid
    cell = grid.gridSize().width()
    for columns in (3, 6, 7):
        for extra in (0, 1, cell - 1):  # exactly a multiple of the cell, and either side
            grid.setFixedWidth(columns * cell + extra)
            QTest.qWait(10)
            last = grid.visualRect(grid.model().index(len(refs) - 1, 0))
            assert last.bottom() < grid.height(), (columns, extra)  # Qt's layout, not ours
    view.deleteLater()


def test_the_page_follows_the_keyboard(app):
    from PySide6.QtTest import QTest

    view, _refs = _lot_view(100, height=500)
    grid = view.browser.sections[0].grid
    grid.setFocus()
    grid.setCurrentIndex(grid.model().index(0, 0))
    for _ in range(12):
        QTest.keyClick(grid, Qt.Key.Key_Down)
    assert view.scroll.verticalScrollBar().value() > 0
    rect = grid.visualRect(grid.currentIndex())
    top = grid.viewport().mapTo(view.scroll.widget(), rect.topLeft()).y()
    shown = view.scroll.verticalScrollBar().value()
    assert shown <= top and top + rect.height() <= shown + view.scroll.viewport().height()
    view.deleteLater()


def test_the_list_heading_arrow_shows_the_sort_applied(app):
    from PySide6.QtTest import QTest

    view, _refs = _lot_view(5)
    b = view.browser
    b.set_view("list")

    def click(column: str) -> None:
        header = b.table.header()
        x = header.sectionViewportPosition(b.table.source.columns.index(column)) + 10
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x, 5))

    def arrow():
        header = b.table.header()
        return header.sortIndicatorSection(), header.sortIndicatorOrder()

    assert arrow()[0] == b.table.source.columns.index("Type")  # by category
    before = arrow()
    click("Notes")  # not a sort: nothing changes, the arrow included
    assert b.sort == "category" and arrow() == before
    click("Type")  # already sorted by it
    assert b.sort == "category" and arrow() == before
    click("Name")
    assert b.sort == "name" and arrow() == (0, Qt.SortOrder.AscendingOrder)
    click("Name")
    assert b.sort == "name_desc" and arrow() == (0, Qt.SortOrder.DescendingOrder)
    view.deleteLater()


def test_a_double_click_acts_once_in_cards_and_list(app):
    from PySide6.QtTest import QTest

    view, _refs = _lot_view(3)
    b = view.browser
    sent = []
    view.exclusion_changed.connect(sent.append)
    grid = b.sections[0].grid
    centre = grid.visualRect(grid.model().index(0, 0)).center()
    # A double click as the system sends it: press, release, then a double click and its
    # release (QTest.mouseDClick sends the last two).
    QTest.mouseClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=centre)
    QTest.mouseDClick(grid.viewport(), Qt.MouseButton.LeftButton, pos=centre)
    assert len(sent) == 1 and len(sent[0]) == 1
    b.set_view("list")
    table = b.table
    tick = table.visualRect(table.model().index(1, 0)).center()
    QTest.mouseClick(table.viewport(), Qt.MouseButton.LeftButton, pos=tick)
    QTest.mouseDClick(table.viewport(), Qt.MouseButton.LeftButton, pos=tick)
    assert len(sent) == 2 and len(sent[-1]) == 2  # once, like the cards
    view.deleteLater()


def test_a_toggle_that_leaves_the_filter_moves_the_keyboard_to_the_next_item(app):
    from PySide6.QtTest import QTest

    view, _refs = _lot_view(6)
    b = view.browser
    b._show_chosen("included")
    grid = b.sections[0].grid
    grid.setFocus()
    grid.setCurrentIndex(grid.model().index(3, 0))
    fourth, fifth = b.shown()[3], b.shown()[4]
    QTest.keyClick(grid, Qt.Key.Key_Space)
    QTest.qWait(20)  # the view refills after the key
    assert fourth not in b.shown()
    focused = view.focusWidget()
    assert focused.currentIndex().data(Qt.ItemDataRole.UserRole + 1) is fifth
    view.deleteLater()


def test_the_size_order_survives_a_rebuild(app, tmp_path):
    from traytrim.gui.household_view import HouseholdView
    from traytrim.plan import build_plan

    gen, index, refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    view.browser.set_sort("size")
    view.set_plan(5, build_plan(refs, index))
    order = [r.ref for r in view.browser.shown()]
    view.browser._activated(view.browser.shown()[0])  # the largest left out: 0 bytes now
    view.set_plan(5, build_plan(refs, index, excluded={order[0]}))
    view._rebuild()  # a theme change
    assert [r.ref for r in view.browser.shown()] == order
    assert callable(view.browser.show)  # QWidget.show, not hidden by the filter
    view.deleteLater()
    next(gen, None)


def test_smallest_first_keeps_ties_a_to_z(app):
    from traytrim.gui.cc_view import TOGGLE, CcBrowser, CcState
    from traytrim.names import ItemName
    from traytrim.resolve import LocatedRef
    from traytrim.tray import RefKind, TrayRef

    refs = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xACAC000000000000 + i)) for i in range(4)])
    state = CcState(TOGGLE, False)
    state.names = {r.ref: ItemName(t, "", t) for r, t in zip(refs, "dbca", strict=True)}
    state.sizes = {refs[0].ref: 5, refs[1].ref: 1, refs[2].ref: 1, refs[3].ref: 1}
    browser = CcBrowser(refs, state, sort="size_asc")
    assert [state.title(r) for r in browser.shown()] == ["a", "b", "c", "d"]
    browser.set_sort("size")
    assert [state.title(r) for r in browser.shown()] == ["d", "a", "b", "c"]
    browser.deleteLater()


def test_the_export_bar_shows_once_the_size_is_scrolled_away(app, tmp_path):
    from PySide6.QtTest import QTest

    from traytrim.plan import build_plan

    view, _refs = _lot_view(120, height=500)
    assert not view.sticky.isVisible()
    gen, index, refs, _report = _household(tmp_path)
    view.set_plan(0x0010175A327A0833, build_plan(refs, index))  # any plan: the size shows
    view.scroll.verticalScrollBar().setValue(view.scroll.verticalScrollBar().maximum())
    QTest.qWait(10)
    assert view.sticky.isVisible() and view.sticky.title.text() == "Seaside"
    assert view.sticky.size_label.text() == view.hero.number.text()
    assert view.sticky.export_button.isEnabled() == view.hero.export_button.isEnabled()
    view.scroll.verticalScrollBar().setValue(0)
    assert not view.sticky.isVisible()
    view.deleteLater()
    next(gen, None)


def test_nothing_picked_shows_what_there_is_and_where_to_start(app, tmp_path, monkeypatch):
    from traytrim.gui import main_window

    monkeypatch.setattr(main_window.paths, "resolve_folders",
                        lambda *_a: main_window.paths.Sims4Folders(None, None))
    win = main_window.MainWindow(Settings())
    win._tray_ready = True
    win.trays.source.set_items([TrayItem(1, 1, "Goth"), TrayItem(2, 2, "Seaside"),
                                TrayItem(3, 2, "Dock")])
    win._select(None)
    assert win.household.showing_overview()
    tiles = [t.accessibleName() for t in win.household.overview.findChildren(QLabel)
             if t.text() in ("1", "2", "0")]
    assert tiles  # counts are shown
    names = [c.text() for c in win.household.overview.cards]
    assert names == ["Export several", "Check a zip", "Export history"]
    assert not win.household.overview.cards[0].isEnabled()  # no index yet
    win.deleteLater()


def test_packages_show_the_kind_they_hold_and_missing_folders_warn(app, tmp_path):
    from traytrim import restypes
    from traytrim.gui.main_window import FolderStatus
    from traytrim.gui.mods_list import kind_icon
    from traytrim.index import ModsPackageInfo

    def package(kinds, items):
        return ModsPackageInfo(tmp_path / "p.package", "p.package", 0, 1, 0, 1, items, kinds)

    assert kind_icon(package({restypes.CASP: 3, restypes.OBJECT_DEFINITION: 1}, 4)) == "cat-top"
    assert kind_icon(package({restypes.OBJECT_DEFINITION: 2}, 2)) == "cat-object"
    assert kind_icon(package({}, 0)) == "package"
    status = FolderStatus("Game", lambda: None, "Needed to tell EA items from CC.")
    status.set_path(None)
    assert status._alert.isVisibleTo(status) and "Needed to tell" in status.toolTip()
    status.set_path(tmp_path)
    assert not status._alert.isVisibleTo(status) and status.toolTip() == ""
    status.deleteLater()


def test_compact_notices_open_their_explanation_on_show(app):
    from traytrim.gui.widgets import Notice, TextLabel

    notice = Notice("info", "text_secondary", "3 files couldn't be read", "Why it matters.",
                    compact=True)
    notice.show()
    body = next(w for w in notice.findChildren(TextLabel) if w.text().replace("​", "")
                == "Why it matters.")
    assert not body.isVisible()
    [toggle] = [b for b in notice.findChildren(QPushButton) if b.text() == "Show"]
    toggle.click()
    assert body.isVisible() and toggle.text() == "Hide"
    notice.deleteLater()


def test_the_size_order_survives_a_sim_change_and_filters_stay_with_their_item(app, tmp_path):
    from traytrim.gui.household_view import HouseholdReport, HouseholdView
    from traytrim.plan import build_plan

    gen, index, refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    view.browser.set_sort("size")
    view.set_plan(5, build_plan(refs, index))
    order = [r.ref for r in view.browser.shown()]
    view.browser._activated(view.browser.shown()[0])  # the largest, left out
    view.show_report(report)  # a Sim chip: built again before the new plan
    view.set_plan(5, build_plan(refs, index, excluded={order[0]}))  # the left-out one is 0 now
    assert [r.ref for r in view.browser.shown()] == order  # it didn't move to the end

    view.browser.search.setText("hair")
    other = HouseholdReport(TrayItem(6, 1, "Other"), report.contents, refs, report.sim_ids)
    view.show_report(other)
    assert view.browser.search.text() == ""  # another item starts with no search
    view.show_report(report)
    assert view.browser.search.text() == ""  # and coming back doesn't bring an old one
    view.deleteLater()
    next(gen, None)


def test_the_overview_follows_the_kind_and_the_tray_folder(app, tmp_path, monkeypatch):
    from traytrim.gui import main_window

    monkeypatch.setattr(main_window.paths, "resolve_folders",
                        lambda *_a: main_window.paths.Sims4Folders(tmp_path, None))
    win = main_window.MainWindow(Settings())  # tmp_path has no Tray folder
    titles = [w.text() for w in win.household.overview.findChildren(QLabel)]
    assert "Tray folder not found" in titles
    win._tray_ready = True
    win._set_kind(main_window.KINDS[1])
    titles = [w.text() for w in win.household.overview.findChildren(QLabel)]
    assert "Pick a lot on the left" in titles
    win.deleteLater()


def test_the_overview_built_again_leaves_nothing_behind(app):
    from traytrim.gui.overview import Overview

    overview = Overview()
    overview.show()
    args = ("Title", "Line", [("1", "lot")], [("history", "History", "Text", lambda: None, True)],
            "Hint")
    overview.fill(*args)
    first = overview.findChildren(QLabel)
    overview.fill(*args)  # e.g. the index got ready: counts again
    assert all(not w.isVisible() for w in first)  # the old ones don't show through
    overview.deleteLater()


def test_settings_change_and_are_kept(app, tmp_path, monkeypatch):
    from traytrim.gui import settings_dialog
    from traytrim.gui.settings_dialog import SettingsDialog

    monkeypatch.setattr(settings_dialog, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr("traytrim.export.config_dir", lambda: tmp_path / "config")
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "index.sqlite").write_bytes(b"x" * 2048)
    path = tmp_path / "settings.json"
    settings = Settings()
    settings.save = lambda: Settings.save(settings, path)
    chosen = []
    dialog = SettingsDialog(settings, lambda: (tmp_path, None), lambda: chosen.append("user"),
                            lambda: chosen.append("game"), lambda: chosen.append("index"))
    assert "Not found" in dialog.game_label.full_text()
    dialog.kit_chip.setChecked(False)
    dialog.update_chip.setChecked(False)
    dialog._set_theme("dark")
    loaded = Settings.load(path)
    assert (loaded.export_test_kit, loaded.check_updates, loaded.theme) == (False, False, "dark")
    dialog._set_theme("system")
    dialog.refresh_button.click()
    assert chosen == ["index"]
    labels = [w.text() for w in dialog.findChildren(QLabel)]
    assert "2.0 KB" in labels and "Export history: empty" in labels
    dialog.deleteLater()


def test_switches_in_settings_load_only_as_switches(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"export_test_kit": "no", "check_updates": false}', encoding="utf-8")
    loaded = Settings.load(path)
    assert loaded.export_test_kit is True and loaded.check_updates is False


def test_the_export_bar_shows_a_failed_plan_with_its_retry(app):
    from PySide6.QtTest import QTest

    view, _refs = _lot_view(120, height=500)
    retried = []
    view.show_plan_error(0x0010175A327A0833, "Disk on fire", lambda: retried.append(1))
    view.scroll.verticalScrollBar().setValue(view.scroll.verticalScrollBar().maximum())
    QTest.qWait(10)
    assert view.sticky.isVisible()
    assert view.sticky.size_label.text() == "Couldn't work out the export"
    assert view.sticky.retry_button.isVisibleTo(view.sticky)
    view.sticky.retry_button.click()
    assert retried == [1]
    view.deleteLater()


def test_an_item_picked_again_after_leaving_it_starts_without_filters(app, tmp_path):
    from traytrim.gui.household_view import HouseholdView

    gen, _index, _refs, report = _household(tmp_path)
    view = HouseholdView()
    view.show_report(report)
    view.browser.search.setText("hair")
    view.show_report(report)  # the same item again without leaving it (a Sim chip)
    assert view.browser.search.text() == "hair"
    view.show_message("Moreau", "Reading its Sims.", loading=True)  # left, then picked again
    view.show_report(report)
    assert view.browser.search.text() == ""
    view.deleteLater()
    next(gen, None)


def test_thumbnails_asked_for_are_not_asked_again_after_a_rebuild(app):
    view, refs = _lot_view(5)
    asked = []
    view.thumbnails_wanted.connect(lambda _i, wanted: asked.extend(wanted))
    view.grab()
    app.processEvents()
    from PySide6.QtTest import QTest
    QTest.qWait(60)
    first = len(asked)
    assert first == len(refs)
    view._rebuild()  # a theme change before the answers came
    view.grab()
    QTest.qWait(60)
    assert len(asked) == first
    view.deleteLater()


def test_the_settings_read_again_button_comes_back(app, tmp_path, monkeypatch):
    from traytrim.gui import settings_dialog
    from traytrim.gui.settings_dialog import SettingsDialog

    monkeypatch.setattr(settings_dialog, "cache_dir", lambda: tmp_path)
    monkeypatch.setattr("traytrim.export.config_dir", lambda: tmp_path / "config")
    dialog = SettingsDialog(Settings(), lambda: (None, None), lambda: None, lambda: None,
                            lambda: None)
    dialog.refresh_button.click()
    assert not dialog.refresh_button.isEnabled()
    dialog.refresh_finished()
    assert dialog.refresh_button.isEnabled()
    assert dialog.refresh_button.text() == "Read Mods and the game again"
    dialog.deleteLater()


def test_export_several_on_the_start_page_says_why_it_is_off(app, monkeypatch):
    from traytrim.gui import main_window

    monkeypatch.setattr(main_window.paths, "resolve_folders",
                        lambda *_a: main_window.paths.Sims4Folders(None, None))
    win = main_window.MainWindow(Settings())
    win._tray_ready = win._index_ready = True  # an empty Tray, the index done
    win._show_overview()
    card = win.household.overview.cards[0]
    assert not card.isEnabled() and "Tray has a household" in card.accessibleDescription()
    win.deleteLater()
