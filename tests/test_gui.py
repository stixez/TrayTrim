"""Widget behaviour that doesn't need a visible window (Qt's offscreen platform)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from traytrim.gui.gallery import ItemRole, TrayList, subtitle  # noqa: E402
from traytrim.settings import Settings  # noqa: E402
from traytrim.tray import TrayItem, TraySim  # noqa: E402


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
    from test_plan import located, world  # noqa: F401
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
