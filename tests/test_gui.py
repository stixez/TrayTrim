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
