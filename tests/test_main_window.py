"""The main window wired together, on a small invented Sims 4 folder (Qt's offscreen platform):
the background work, the views and the signals between them, which unit tests of the parts
don't reach."""

import os
import struct
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

import test_build as tb
import test_tray as tt
from test_gui import app  # noqa: F401  (fixture)
from test_plan import casp_bytes, geom_bytes, write
from traytrim import restypes
from traytrim.dbpf import ResourceKey

CC = 0x80000000


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A Sims 4 folder with a household wearing a recolor whose mesh is in another package, a
    lot with a chair and a wall, and the game; caches and settings kept in tmp_path."""
    user, game = tmp_path / "The Sims 4", tmp_path / "Game"
    mods, tray_dir = user / "Mods", user / "Tray"
    mesh = ResourceKey(restypes.GEOM, CC, 0xAAAA0000000000B1)
    write(mods / "Meshes" / "Soft_Bob_Mesh.package", [(mesh, geom_bytes([]))])
    write(mods / "Soft_Bob_Recolors.package",
          [(ResourceKey(restypes.CASP, CC, tt.TOP), casp_bytes([mesh]))])
    write(mods / "Rattan_Chair.package", [
        (ResourceKey(restypes.OBJECT_DEFINITION, 0, tb.CHAIR), b"objd" + struct.pack("<Q", 0)),
        (ResourceKey(restypes.OBJECT_CATALOG, 0, tb.CHAIR), b"cobj")])
    write(mods / "Walls.package", [(tb.WALL, b"wall")])
    write(game / "Data" / "Client" / "ClientFullBuild0.package",
          [(ResourceKey(restypes.CASP, 0, tt.EA_PART), b"ea")])
    tray_dir.mkdir(parents=True)
    (tray_dir / f"0x00000001!0x{tt.HH_ID:016x}.trayitem").write_bytes(tt.trayitem())
    (tray_dir / f"0x00000000!0x{tt.HH_ID:016x}.householdbinary").write_bytes(
        tt.householdbinary(2))
    (tray_dir / f"0x00000002!0x{tb.LOT_ID:016x}.trayitem").write_bytes(tb.lot_trayitem())
    (tray_dir / f"0x00000000!0x{tb.LOT_ID:016x}.blueprint").write_bytes(tb.blueprint(
        tb.objects_message([tb.CHAIR]), tb.documented_architecture([tb.WALL])))
    for module in ("traytrim.paths", "traytrim.index", "traytrim.depindex"):
        monkeypatch.setattr(f"{module}.cache_dir", lambda: tmp_path / "cache")
    for module in ("traytrim.export", "traytrim.settings"):
        monkeypatch.setattr(f"{module}.config_dir", lambda: tmp_path / "config")
    return user, game


def pump(until, timeout: float = 20) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if until():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the window")


def test_the_window_end_to_end(app, world, monkeypatch, tmp_path):  # noqa: F811
    from traytrim.batch import export_many
    from traytrim.export import ExportOptions
    from traytrim.gui import main_window
    from traytrim.gui.main_window import MainWindow
    from traytrim.index import ResourceIndex
    from traytrim.settings import Settings

    user, game = world
    dialogs = []
    monkeypatch.setattr(MainWindow, "_run_dialog", staticmethod(dialogs.append))
    win = MainWindow(Settings(user_dir=str(user), game_dir=str(game)))
    win.show()
    try:
        pump(lambda: win._index_ready and win._tray_ready)

        # A household (its tray id is 64-bit): its cards ask for thumbnails and get answers.
        household = next(i for i in win.trays.source.items if i.kind == "household")
        assert household.instance > 1 << 32
        assert win.trays.select_item(household)
        pump(lambda: win.household._plan is not None and win.household._thumbs_tried)

        # The Mods tab: the mesh package is used by the household, through the recolor, and
        # the recolor breaks without it.
        win._set_section("Mods")
        pump(lambda: win.mods_list.visible_count() == 4)
        assert win.mods_list.select_path(user / "Mods" / "Meshes" / "Soft_Bob_Mesh.package")
        pump(lambda: win.packages._users is not None)
        assert win.packages.verdict.text.text() == "Used by 1 household"
        assert [d.rel for d in win.packages._dependents] == ["Soft_Bob_Recolors.package"]

        # Exporting one item from a package opens the export dialog for just that item.
        win.mods_list.select_path(user / "Mods" / "Soft_Bob_Recolors.package")
        pump(lambda: win.packages.report is not None and win.packages.browser is not None)
        win.packages.browser._activated(win.packages.browser.shown()[0])
        pump(lambda: dialogs)
        request = dialogs[0].request
        assert request.item is None and len(request.refs) == 1 and request.plan.resources

        # Check a zip: the lot as someone would receive it; Esc goes back.
        lot = next(i for i in win.trays.source.items if i.kind == "lot")
        with ResourceIndex() as ix:
            [outcome] = export_many([lot], ExportOptions(tmp_path / "Desktop", "", False), ix)
        win._open_zip(outcome.result.zip_path)
        pump(lambda: win.received.received is not None)
        assert win.right.currentWidget() is win.received
        assert win.received.verdict.text.text() == "Ready to install"
        workdir = win._received.workdir
        win._leave_received()
        assert win.right.currentWidget() is win.packages
        pump(lambda: not workdir.exists())  # the temporary folder goes once nothing reads it

        # "Export this item alone" from a household plans with the copies chosen for it.
        planned = []
        real_build_plan = main_window.build_plan
        monkeypatch.setattr(main_window, "build_plan",
                            lambda refs, ix, **kw: planned.append(kw) or real_build_plan(refs, ix, **kw))
        win._set_section("Tray")
        win._choice(household.instance).copies[ResourceKey(1, 2, 3)] = tmp_path / "chosen.package"
        win.household.export_item_requested.emit(win.household._summary.cc[0], None)
        pump(lambda: len(dialogs) == 2)
        assert planned[-1]["copies_chosen"] == {ResourceKey(1, 2, 3): tmp_path / "chosen.package"}

        # A room saved in the game while TrayTrim is open shows up when coming back to it.
        tray_dir = user / "Tray"
        (tray_dir / f"0x00000003!0x{tb.ROOM_ID:016x}.trayitem").write_bytes(tb.room_trayitem())
        (tray_dir / f"0x00000000!0x{tb.ROOM_ID:016x}.room").write_bytes(
            tb.room(15, [tb.WALL], tb.objects_message([tb.CHAIR])))
        os.utime(tray_dir, ns=(time.time_ns(), time.time_ns() + 10**9))
        pump(lambda: not win.index.running)  # the previous step's check is done
        win._last_check = 0
        win._check_for_changes()
        pump(lambda: any(i.kind == "room" for i in win.trays.source.items))

        # The household on screen re-saved in the game: it's read again when coming back.
        before = win._selected
        hh_file = tray_dir / f"0x00000000!0x{tt.HH_ID:016x}.householdbinary"
        later = time.time_ns() + 5 * 10**9
        os.utime(hh_file, ns=(later, later))
        os.utime(tray_dir, ns=(later, later))
        pump(lambda: not win.index.running)  # the previous step's check is done
        win._last_check = 0
        win._check_for_changes()
        pump(lambda: win._selected is not before and win._selected.instance == before.instance)
        win._set_section("Mods")

        # Deleting the package on screen, then coming back to the window.
        win.mods_list.select_path(user / "Mods" / "Meshes" / "Soft_Bob_Mesh.package")
        pump(lambda: win.packages.report is not None
             and win.packages.report.rel == "Meshes/Soft_Bob_Mesh.package")
        pump(lambda: not win.index.running)  # the previous step's check is done (Windows
        (user / "Mods" / "Meshes" / "Soft_Bob_Mesh.package").unlink()  # can't delete open files)
        win._last_check = 0
        win._check_for_changes()
        pump(lambda: "is gone" in win.packages.message_title.text())
    finally:
        win.stop_work()
        QThreadPool.globalInstance().waitForDone(10_000)
        win._drop_received(now=True)
        win.close()
        win.deleteLater()
