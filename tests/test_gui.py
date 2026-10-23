"""Widget behaviour that doesn't need a visible window (Qt's offscreen platform)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
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
    view.setCurrentIndex(view.proxy.index(0, 0))
    assert picked == [items[0]]

    view.set_filter("go")  # hides Smith: nothing else may be picked on the user's behalf
    assert picked == [items[0]]
    assert view.visible_count() == 2 and not view.currentIndex().isValid()

    view.set_filter("")  # Smith is back and still selected
    assert picked == [items[0]]
    assert view.currentIndex().data(ItemRole) is items[0]



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
    assert [h.text() for h in dialog._headings] == ["Households", "Lots"]
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
    assert len(view._all_cards) == 2
    view.set_names(5, {refs[0].ref: ItemName("Long Waves", "Me", "Me_yfHair_LongWaves")})
    assert view._all_cards[0].title._text == "Long Waves"
    view._search.setText("waves")
    assert [c.isHidden() for c in view._all_cards] == [False, True]
    assert view.match_label.text() == "1 of 2"
    view._search.setText("nothing like this")
    assert view.no_match.isVisibleTo(view)
    view._search.setText("")
    view._set_sort("name")
    assert [c.title._text for c in view._all_cards] == ["Long Waves", "recolor"]

    # Sorted by size, the order stays put when an item is left out and the plan comes back.
    from traytrim.plan import build_plan
    view._set_sort("size")
    view.set_plan(5, build_plan(refs, index))
    order = [c.ref.ref for c in view._all_cards]
    view.set_plan(5, build_plan(refs, index, excluded={order[0]}))
    assert [c.ref.ref for c in view._all_cards] == order

    # A plan error survives a rebuild (names arriving, a sort change...).
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
    view.show_report(report)
    assert view.verdict.text.text() == "Working out what uses it"
    view.set_usage(report.path, [home], broken)
    assert view.verdict.text.text() == "Used by 1 household"
    assert view.notices.isVisibleTo(view) and view.users_box.isVisibleTo(view)
    view.set_usage(tmp_path / "other.package", [], [])  # for another package: ignored
    assert view.verdict.text.text() == "Used by 1 household"

    wanted = []
    view.export_item_requested.connect(lambda ref, name: wanted.append(ref))
    view.show_report(recolors)
    assert len(view._cards) == 1
    view._cards[0]._activate()
    assert [r.ref.instance for r in wanted] == [recolor.instance]
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


def test_long_lists_are_shown_a_page_at_a_time(app):
    from traytrim.gui import report_parts
    from traytrim.gui.household_view import PAGE_CARDS, HouseholdReport, HouseholdView
    from traytrim.gui.report_parts import PAGE_ROWS, PagedCcList
    from traytrim.gui.widgets import PAGE_DETAILS, Notice
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    notice = Notice("info", "text_secondary", "Lots", "body",
                    [(f"row {i}", "", "") for i in range(PAGE_DETAILS * 2 + 5)], expanded=True)
    assert notice._shown == PAGE_DETAILS and notice._more.isVisibleTo(notice)
    notice._add_rows()
    notice._add_rows()
    assert notice._shown == PAGE_DETAILS * 2 + 5 and not notice._more.isVisibleTo(notice)

    refs = [LocatedRef(TrayRef(RefKind.OBJECT, 0xAAAA000000000000 + i)) for i in range(PAGE_ROWS + 10)]
    paged = PagedCcList(False, set(), {})
    paged.set_groups([("Objects", refs)])
    assert len(paged.list.rows) == PAGE_ROWS and paged.total == PAGE_ROWS + 10
    paged._show_more()
    assert len(paged.list.rows) == PAGE_ROWS + 10
    paged.set_groups([("Objects", refs[:3]), ("Empty", [])])  # a search
    assert len(paged.list.rows) == 3 and paged.list.topLevelItemCount() == 1

    # A lot with more CC than a page of cards: a page is built, the search covers all of it,
    # and only the cards on screen ask for thumbnails.
    many = 700
    lot_refs = [LocatedRef(TrayRef(RefKind.OBJECT, 0xBBBB000000000000 + i),
                           usage={1: {"Placed"}}) for i in range(many)]
    for r in lot_refs:
        r.mods = [type("Hit", (), {"rel": f"Objects/obj_{r.ref.instance:x}.package",
                                   "path": None, "load_order": 0})()]
    wanted = []
    view = HouseholdView()
    view.thumbnails_wanted.connect(lambda _i, refs: wanted.extend(refs))
    contents = HouseholdContents("Seaside", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
    view.show_report(HouseholdReport(TrayItem(0x0010175A327A0833, 2, "Seaside"), contents, lot_refs, {1}))
    assert len(view._all_cards) == PAGE_CARDS and len(wanted) == PAGE_CARDS
    view._show_more_cards()
    assert len(view._all_cards) == PAGE_CARDS * 2 and len(wanted) == PAGE_CARDS * 2
    view._search.setText(f"{lot_refs[-1].ref.instance:x}")
    view._rebuild()  # what the search's pause timer does
    assert [c.ref for c in view._all_cards] == [lot_refs[-1]] and view._matching == 1
    view.deleteLater()
    assert report_parts.MAX_CARDS == PAGE_CARDS


def test_thumbnails_kept_in_memory_are_capped(app, monkeypatch):
    from PySide6.QtGui import QImage

    from traytrim.gui import household_view
    from traytrim.gui.household_view import HouseholdView

    monkeypatch.setattr(household_view, "MAX_THUMBNAILS", 3)
    view = HouseholdView()
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    view._thumbs_asked = {1, 2, 3, 4, 5}
    view.set_thumbnails(0, {i: image for i in (1, 2, 3, 4, 5)}, {1, 2, 3, 4, 5})
    assert list(view._thumbs) == [3, 4, 5]  # the oldest went
    assert 1 not in view._thumbs_asked and 1 not in view._thumbs_tried  # loaded again if shown
    view.deleteLater()


def test_keyboard_focus_goes_to_the_first_new_item(app):
    from traytrim.gui.household_view import PAGE_CARDS, HouseholdReport, HouseholdView
    from traytrim.gui.report_parts import PAGE_ROWS, PagedCcList
    from traytrim.resolve import LocatedRef
    from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayRef

    refs = [LocatedRef(TrayRef(RefKind.OBJECT, 0xBBBB000000000000 + i), usage={1: {"Placed"}})
            for i in range(PAGE_CARDS + 5)]
    for r in refs:  # in Mods, so they're CC
        r.mods = [type("Hit", (), {"rel": f"Objects/obj_{r.ref.instance:x}.package",
                                   "path": None, "load_order": 0})()]
    view = HouseholdView()
    view.show()
    contents = HouseholdContents("Seaside", [SimContents(TraySim(1, "", ""))], 2, kind="lot")
    view.show_report(HouseholdReport(TrayItem(0x0010175A327A0833, 2, "Seaside"), contents, refs,
                                     {1}))
    view._show_more_cards()
    assert view.focusWidget() is view._all_cards[PAGE_CARDS]
    # A rebuild (here a sort) keeps focus on the same card.
    card_ref = view._all_cards[3].ref.ref
    view._all_cards[3].setFocus()
    view._set_sort("name")
    assert view.focusWidget().ref.ref == card_ref
    view.deleteLater()

    paged = PagedCcList(False, set(), {})
    paged.show()
    paged.set_groups([("Objects", [LocatedRef(TrayRef(RefKind.OBJECT, 0xCCCC000000000000 + i))
                                   for i in range(PAGE_ROWS + 1)])])
    paged._show_more()
    assert paged.list.currentItem() is paged.list.rows[PAGE_ROWS][0]
    assert paged.focusWidget() is paged.list
    paged.deleteLater()


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

    from traytrim.gui.household_view import PAGE_CARDS, HouseholdReport, HouseholdView
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
    assert view._all_cards[0].thumb._image is image  # the answer for the first lot shows here

    many = _cc([LocatedRef(TrayRef(RefKind.OBJECT, 0xEEEE000000000000 + i), usage={1: {"Placed"}})
                for i in range(PAGE_CARDS + 10)])
    view.show_report(lot(0x10000000003, many))
    view._search.setText(f"{many[0].ref.instance:x}")
    view._rebuild()
    [section] = view._sections
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
    [link] = [b for b in line.findChildren(QPushButton) if b.text() == "buy me a coffee"]
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


def test_a_paged_list_counts_whole_sections(app):
    from traytrim.gui.report_parts import PAGE_ROWS, PagedCcList
    from traytrim.resolve import LocatedRef
    from traytrim.tray import RefKind, TrayRef

    refs = [LocatedRef(TrayRef(RefKind.OBJECT, 0xCCCC000000000000 + i))
            for i in range(PAGE_ROWS + 10)]
    paged = PagedCcList(False, set(), {})
    paged.set_groups([("Objects", refs)])
    assert paged.list.topLevelItem(0).text(0) == f"Objects ({PAGE_ROWS + 10:,})"
    paged.deleteLater()


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
    buttons = {b.text(): b for b in view.scroll.widget().findChildren(QPushButton)}

    buttons["Leave all out"].click()
    assert sent[-1] == {r.ref for r in refs}
    assert not any(c.included for c in view._all_cards)
    assert view.excluded_label.text() == "2 left out"
    buttons["Include all"].click()
    assert sent[-1] == set() and all(c.included for c in view._all_cards)

    # While searching, only the matches.
    view._search.setText("waves")
    buttons["Leave all out"].click()
    assert sent[-1] == {refs[0].ref}
    assert [c.included for c in view._all_cards] == [False, True]
    count = len(sent)
    buttons["Leave all out"].click()  # nothing changes: nothing is sent
    assert len(sent) == count

    # The list shows what's left out as it's built: it's built again.
    view._search.setText("")
    view._set_mode("list")
    buttons = {b.text(): b for b in view.scroll.widget().findChildren(QPushButton)}
    buttons["Include all"].click()
    assert sent[-1] == set() and view._list is not None
    buttons = {b.text(): b for b in view.scroll.widget().findChildren(QPushButton)}
    buttons["Leave all out"].click()
    assert sent[-1] == {r.ref for r in refs} and view._list._excluded == sent[-1]
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
    [link] = [b for b in win.statusBar().findChildren(QPushButton)
              if b.text() == "Buy me a coffee"]
    link.click()
    assert opened == [SUPPORT_URL]
    assert win._check_updates is False  # tests never ask GitHub
    win.deleteLater()
