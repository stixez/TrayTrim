"""Exporting several tray items in a row, and checking an export's zip again later."""

import json
import struct
import threading
import zipfile

import pytest

from test_build import (CHAIR, EA_SOFA, LAMP, WALL, items_by_kind, tray_dir,  # noqa: F401
                        write)
from traytrim import restypes
from traytrim.batch import Choice, export_many, export_together, prepare
from traytrim.dbpf import ResourceKey
from traytrim.export import ExportOptions
from traytrim.index import ResourceIndex
from traytrim.tray import RefKind, TrayRef
from traytrim.verify import check_zip


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    monkeypatch.setattr("traytrim.export.config_dir", lambda: tmp_path / "config")


@pytest.fixture
def index(tmp_path):
    mods, game = tmp_path / "Mods", tmp_path / "Game"
    chair = ResourceKey(restypes.OBJECT_DEFINITION, 0, CHAIR)
    write(mods / "chair.package", [
        (chair, b"objd" + struct.pack("<Q", 0)),
        (ResourceKey(restypes.OBJECT_CATALOG, 0, CHAIR), b"cobj"),
        (ResourceKey(restypes.OBJECT_THUMB, 1, CHAIR), b"jpeg"),
    ])
    write(mods / "walls.package", [(WALL, b"wall")])
    sofa = ResourceKey(restypes.OBJECT_DEFINITION, 0, EA_SOFA)
    write(mods / "unlock sofa.package", [(sofa, b"unlocked")])
    write(game / "Data" / "Client" / "ClientFullBuild0.package", [(sofa, b"ea sofa")])
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, game)
        yield ix


def test_prepare_uses_the_defaults_or_the_users_choice(index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    p = prepare(lot, index)
    assert p.plan.excluded == {TrayRef(RefKind.OBJECT, EA_SOFA)} and p.base_name == "Seaside"
    chair = TrayRef(RefKind.OBJECT, CHAIR)
    p = prepare(lot, index, Choice(excluded={chair}))
    assert p.plan.excluded == {chair}
    assert ResourceKey(restypes.OBJECT_DEFINITION, 0, EA_SOFA) in p.plan.resources


def test_export_many_reports_each_item(tmp_path, index, tray_dir):  # noqa: F811
    items = items_by_kind(tray_dir)
    lot, room = items["lot"], items["room"]
    broken = items_by_kind(tray_dir)["lot"]
    broken.files = [f for f in broken.files if f.suffix != ".blueprint"]
    everything = {TrayRef(RefKind.OBJECT, CHAIR), TrayRef(RefKind.OBJECT, LAMP),
                  TrayRef(RefKind.BUILD, WALL.instance, type_id=WALL.type, res_group=0)}
    steps = []
    out = tmp_path / "out"
    outcomes = export_many([lot, broken, room, lot], ExportOptions(out, "", False), index,
                           {room.instance: Choice(excluded=everything)},
                           lambda *args: steps.append(args))
    first, failed, skipped, again = outcomes
    assert first.result.verification.ok and first.result.zip_path.name == "Seaside_TrayTrim.zip"
    assert "no .blueprint or .room file" in failed.error
    assert skipped.skipped == "No CC to export" and skipped.result is None
    assert again.result.zip_path.name == "Seaside_TrayTrim (2).zip"
    assert [s for s in steps if s[1] == "done"] == [(i, "done", 1, 1) for i in range(4)]
    assert {"plan", "package", "verify", "zip"} <= {s[1] for s in steps}
    assert sorted(p.name for p in out.iterdir()) == ["Seaside_TrayTrim (2).zip",
                                                     "Seaside_TrayTrim.zip"]


def test_export_many_stops_when_cancelled(tmp_path, index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    cancel = threading.Event()

    def progress(position, step, _done, _total):
        if step == "done" and position == 0:
            cancel.set()

    out = tmp_path / "out"
    outcomes = export_many([lot, lot], ExportOptions(out, "", False), index, None, progress,
                           cancel)
    assert len(outcomes) == 1 and outcomes[0].result is not None
    assert [p.name for p in out.iterdir()] == ["Seaside_TrayTrim.zip"]


def test_check_zip_finds_damage_and_changes(tmp_path, index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    [outcome] = export_many([lot], ExportOptions(tmp_path / "out", "", False), index)
    good = outcome.result.zip_path
    assert check_zip(good).ok

    # A report that no longer matches the package
    changed = tmp_path / "changed.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(changed, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "report.json":
                report = json.loads(data)
                report["files"] = report["files"][1:]
                data = json.dumps(report).encode()
            dst.writestr(info, data)
    result = check_zip(changed)
    assert [c.name for c in result.checks if not c.passed] == ["It matches its report"]

    # Flipped bytes inside the stored package
    damaged = tmp_path / "damaged.zip"
    data = bytearray(good.read_bytes())
    at = data.find(b"DBPF") + 200
    data[at:at + 4] = b"\xff\xff\xff\xff"
    damaged.write_bytes(bytes(data))
    assert not check_zip(damaged).checks[0].passed

    missing = check_zip(tmp_path / "gone.zip")
    assert not missing.ok and missing.checks[0].name == "The zip opens"


def test_check_zip_reports_damaged_compressed_files_and_foreign_zips(tmp_path, index,
                                                                    tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    [outcome] = export_many([lot], ExportOptions(tmp_path / "out", "", False), index)
    good = outcome.result.zip_path
    with zipfile.ZipFile(good) as z:
        info = z.getinfo("report.json")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    data = bytearray(good.read_bytes())
    for i in range(start + 10, start + 30):
        data[i] ^= 0x5A
    broken = tmp_path / "broken.zip"
    broken.write_bytes(bytes(data))
    result = check_zip(broken)  # must not raise zlib.error
    assert result.checks[0].name == "The zip is intact" and not result.checks[0].passed
    assert any("report.json" in p for p in result.checks[0].problems)

    foreign = tmp_path / "photos.zip"
    with zipfile.ZipFile(foreign, "w") as z:
        z.writestr("holiday.jpg", b"jpeg")
    [only] = check_zip(foreign).checks
    assert only.name == "It's a TrayTrim export" and not only.passed


def test_export_together_shares_one_package(tmp_path, index, tray_dir):  # noqa: F811
    items = items_by_kind(tray_dir)
    lot, room = items["lot"], items["room"]
    chair = TrayRef(RefKind.OBJECT, CHAIR)
    # Left out of the lot but not of the room: the room still needs it.
    result = export_together([lot, room], ExportOptions(tmp_path / "out", "", False), index,
                             {lot.instance: Choice(excluded={chair})})
    assert result.verification.ok and result.zip_path.name == "Seaside_and_Nursery_TrayTrim.zip"
    with zipfile.ZipFile(result.zip_path) as z:
        names = z.namelist()
        report = json.loads(z.read("report.json"))
        readme = z.read("README.txt").decode()
    assert sum(n.endswith(".package") for n in names) == 1
    assert {n for n in names if n.startswith("Tray/")} == {
        f"Tray/{f.name}" for f in lot.files + room.files}
    assert report["kind"] == "bundle" and [i["kind"] for i in report["items"]] == ["lot", "room"]
    keys = [f["key"] for f in report["files"]]
    assert len(keys) == len(set(keys))  # the chair both use is in it once
    assert any(f"{restypes.OBJECT_DEFINITION:08X}" in k and f"{CHAIR:016X}" in k for k in keys)
    assert "Lot: Seaside" in readme and "Room: Nursery" in readme
    assert check_zip(result.zip_path).ok


def test_export_together_with_nothing_to_export(tmp_path, index, tray_dir):  # noqa: F811
    from traytrim.export import ExportError

    room = items_by_kind(tray_dir)["room"]
    everything = {TrayRef(RefKind.OBJECT, CHAIR), TrayRef(RefKind.OBJECT, LAMP),
                  TrayRef(RefKind.BUILD, WALL.instance, type_id=WALL.type, res_group=0)}
    with pytest.raises(ExportError):
        export_together([room], ExportOptions(tmp_path / "out", "", False), index,
                        {room.instance: Choice(excluded=everything)})
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
