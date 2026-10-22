import os
import threading

import pytest

from traytrim import cli, gameindex, modsindex
from traytrim.dbpf import COMP_ZLIB, PackageWriter, ResourceKey
from traytrim.index import MODS, ResourceIndex

CASP = 0x034AEECB
GEOM = 0x015A1849
BIG_INSTANCE = 0xFFEEDDCCBBAA9988  # needs the signed 64-bit round trip


def write_package(path, resources):
    path.parent.mkdir(parents=True, exist_ok=True)
    with PackageWriter(path) as w:
        for key, data in resources:
            w.add(key, data)
    return path


@pytest.fixture
def folders(tmp_path):
    mods = tmp_path / "Mods"
    game = tmp_path / "Game"
    write_package(mods / "b_top.package", [(ResourceKey(CASP, 0, 1), b"top-b")])
    write_package(mods / "A" / "a_top.package", [(ResourceKey(CASP, 0, 1), b"top-a"),
                                                 (ResourceKey(GEOM, 7, BIG_INSTANCE), b"mesh")])
    write_package(game / "Data" / "Client" / "ClientFullBuild0.package",
                  [(ResourceKey(CASP, 0, 100), b"ea")])
    write_package(game / "EP03" / "ClientFullBuild0.package", [(ResourceKey(CASP, 0, 300), b"ep")])
    write_package(game / "Delta" / "EP03" / "ClientDeltaBuild0.package",
                  [(ResourceKey(CASP, 0, 300), b"ep2")])
    write_package(game / "Mods" / "unused.package", [(ResourceKey(CASP, 0, 999), b"x")])
    return mods, game


@pytest.fixture
def index(tmp_path):
    with ResourceIndex(tmp_path / "cache" / "index.sqlite") as ix:
        yield ix


def test_finds_mods_resources_in_load_order(folders, index):
    mods, game = folders
    r = index.update(mods, game)
    assert (r.mods_packages, r.mods_resources) == (2, 3)
    hits = index.mods_hits(ResourceKey(CASP, 0, 1))
    assert [h.rel for h in hits] == ["A/a_top.package", "b_top.package"]  # last one wins in game
    big = index.mods_hits(ResourceKey(GEOM, 7, BIG_INSTANCE))
    assert big[0].entry.key.instance == BIG_INSTANCE and big[0].entry.compression == COMP_ZLIB


def test_game_packs_and_deltas_without_install_mods_folder(folders, index):
    mods, game = folders
    index.update(mods, game)
    hits = index.game_hits(ResourceKey(CASP, 0, 300))
    assert [(h.pack, h.delta) for h in hits] == [("EP03", False), ("EP03", True)]
    assert index.game_hits(ResourceKey(CASP, 0, 100))[0].pack == "base"
    assert index.game_hits(ResourceKey(CASP, 0, 999)) == []  # Game/Mods isn't game content


def test_second_update_rereads_nothing_then_only_changes(folders, index):
    mods, game = folders
    index.update(mods, game)
    assert index.update(mods, game).reread == 0

    write_package(mods / "b_top.package", [(ResourceKey(CASP, 0, 2), b"changed")])
    os.utime(mods / "b_top.package", ns=(1, 1))  # make sure mtime differs
    (mods / "A" / "a_top.package").unlink()
    write_package(mods / "new.package", [(ResourceKey(CASP, 0, 3), b"new")])
    r = index.update(mods, game)
    assert r.reread == 2
    assert index.mods_hits(ResourceKey(CASP, 0, 1)) == []
    assert [h.rel for h in index.mods_hits(ResourceKey(CASP, 0, 2))] == ["b_top.package"]
    assert r.mods_packages == 2


def test_load_order_updates_when_packages_are_added(folders, index):
    mods, game = folders
    index.update(mods, game)
    write_package(mods / "0_first.package", [(ResourceKey(CASP, 0, 1), b"zero")])
    index.update(mods, game)
    assert [h.rel for h in index.mods_hits(ResourceKey(CASP, 0, 1))] == [
        "0_first.package", "A/a_top.package", "b_top.package"]
    assert [h.load_order for h in index.mods_hits(ResourceKey(CASP, 0, 1))] == [0, 1, 2]


def test_damaged_package_is_reported_and_retried(folders, index):
    mods, game = folders
    bad = mods / "bad.package"
    bad.write_bytes(b"DBPF" + bytes(10))
    r = index.update(mods, game)
    assert [os.path.basename(p) for p, _ in r.failed] == ["bad.package"]
    assert "not a Sims 4 package" in r.failed[0][1]
    write_package(bad, [(ResourceKey(CASP, 0, 5), b"fixed")])
    r = index.update(mods, game)
    assert r.failed == []
    assert index.mods_hits(ResourceKey(CASP, 0, 5))


def test_missing_folder_keeps_cache_and_new_folder_replaces_it(folders, index, tmp_path):
    mods, game = folders
    index.update(mods, game)
    index.update(None, tmp_path / "nowhere")
    assert index.mods_hits(ResourceKey(CASP, 0, 1))
    assert index.game_hits(ResourceKey(CASP, 0, 100))

    other = tmp_path / "OtherMods"
    write_package(other / "x.package", [(ResourceKey(CASP, 0, 77), b"x")])
    index.update(other, game)
    assert index.mods_hits(ResourceKey(CASP, 0, 1)) == []
    assert index.mods_hits(ResourceKey(CASP, 0, 77))


def test_cancel_stops_early_and_next_update_resumes(folders, index):
    mods, game = folders
    cancel = threading.Event()
    calls = []

    def progress(phase, done, total):
        calls.append(phase)
        cancel.set()

    r = index.update(mods, game, progress, cancel)
    assert r.cancelled and calls == [MODS]
    r = index.update(mods, game)
    assert not r.cancelled and r.reread == 4  # the other Mods package and the 3 game ones
    assert index.mods_hits(ResourceKey(CASP, 0, 1))


def test_damaged_cache_is_rebuilt(tmp_path):
    db = tmp_path / "index.sqlite"
    db.write_bytes(b"this is not sqlite" * 100)
    with ResourceIndex(db) as ix:
        assert ix.mods_hits(ResourceKey(CASP, 0, 1)) == []


def test_resource_cfg_depth_and_too_deep_packages(tmp_path):
    mods = tmp_path / "Mods"
    (mods).mkdir()
    (mods / "Resource.cfg").write_text(
        "Priority 500\nPackedFile *.package\nPackedFile */*.package\n", encoding="utf-8")
    write_package(mods / "one" / "ok.package", [(ResourceKey(CASP, 0, 1), b"a")])
    write_package(mods / "one" / "two" / "deep.package", [(ResourceKey(CASP, 0, 2), b"b")])
    scan = modsindex.scan_mods(mods)
    assert scan.depth == 1
    assert [p.rel for p in scan.packages] == ["one/ok.package"]
    assert scan.too_deep == ["one/two/deep.package"]


def test_default_depth_without_resource_cfg(tmp_path):
    assert modsindex.max_depth(tmp_path) == modsindex.DEFAULT_DEPTH


def test_macos_bundle_content_root(tmp_path):
    app = tmp_path / "The Sims 4.app"
    write_package(app / "Contents" / "Data" / "Client" / "ClientFullBuild0.package",
                  [(ResourceKey(CASP, 0, 1), b"a")])
    found = gameindex.scan_game(app)
    assert [(p.rel, p.pack) for p in found] == [("Data/Client/ClientFullBuild0.package", "base")]


def test_cli_which_and_index(folders, tmp_path, monkeypatch, capsys):
    mods, game = folders
    monkeypatch.setattr("traytrim.index.cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr("traytrim.settings.default_path", lambda: tmp_path / "settings.json")
    user = mods.parent
    (user / "Tray").mkdir()
    args = ["--sims-folder", str(user), "--game-folder", str(game)]

    assert cli.main(["index", "--quiet", *args]) == 0
    assert "Mods: 2 packages, 3 resources" in capsys.readouterr().out

    assert cli.main(["which", f"{CASP:08X}:00000000:{1:016X}", *args]) == 0
    out = capsys.readouterr().out
    assert "A/a_top.package" in out and "b_top.package" in out and "used: loads last" in out

    assert cli.main(["which", f"{300:X}", *args]) == 0  # instance only
    assert "EP03" in capsys.readouterr().out

    assert cli.main(["which", "ABCDEF", *args]) == 1
    assert cli.main(["which", "not-a-key", *args]) == 2


def test_new_sims_folder_without_mods_clears_old_mods(folders, index, tmp_path):
    mods, game = folders
    index.update(mods, game)
    only_tray = tmp_path / "Other Sims 4"
    (only_tray / "Tray").mkdir(parents=True)
    r = index.update(only_tray / "Mods", game)
    assert r.mods_packages == 0
    assert index.mods_hits(ResourceKey(CASP, 0, 1)) == []


def test_reopening_keeps_cache_and_old_schema_is_replaced(folders, tmp_path):
    mods, game = folders
    path = tmp_path / "c" / "index.sqlite"
    with ResourceIndex(path) as ix:
        ix.update(mods, game)
    with ResourceIndex(path) as ix:
        assert ix.mods_hits(ResourceKey(CASP, 0, 1))
        ix.db.execute("UPDATE meta SET value='0' WHERE key='schema'")
        ix.db.commit()
    with ResourceIndex(path) as ix:
        assert ix.mods_hits(ResourceKey(CASP, 0, 1)) == []
        assert ix.update(mods, game).mods_packages == 2


def test_index_is_complete_only_after_a_finished_update(folders, index):
    mods, game = folders
    assert not index.is_complete(mods, game)
    cancel = threading.Event()
    index.update(mods, game, lambda *a: cancel.set(), cancel)
    assert not index.is_complete(mods, game)
    index.update(mods, game)
    assert index.is_complete(mods, game)
    assert index.is_complete(mods, None)
    assert not index.is_complete(mods.parent / "Elsewhere", game)


def test_human_sizes():
    from traytrim.units import human_size, percent_smaller
    assert human_size(512) == "512 B"
    assert human_size(31_948_000) == "30.5 MB"
    assert human_size(5_320_000_000) == "4.95 GB"
    assert percent_smaller(32, 5000) == "99.4% smaller"
    assert percent_smaller(5000, 32) is None
