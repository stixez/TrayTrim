"""Checking a zip someone sent against your own Mods and game."""

import json
import zipfile

import pytest

from test_batch import index, private_config  # noqa: F401  (fixtures)
from test_build import (CHAIR, FLOOR, LAMP, OVERRIDE_MODEL, WALL, items_by_kind,  # noqa: F401
                        tray_dir, write)
from traytrim import restypes
from traytrim.batch import export_many
from traytrim.dbpf import ResourceKey
from traytrim.export import ExportOptions, export_item
from traytrim.index import ResourceIndex
from traytrim.pkgreport import item_ref
from traytrim.plan import build_plan
from traytrim.received import check_received
from traytrim.tray import RefKind


@pytest.fixture
def seaside_zip(tmp_path, index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    [outcome] = export_many([lot], ExportOptions(tmp_path / "out", "", False), index)
    return outcome.result.zip_path


@pytest.fixture
def recipient(tmp_path):
    """Someone else's Sims 4: the base game, and a different copy of the chair in Mods."""
    mods, game = tmp_path / "Theirs" / "Mods", tmp_path / "Theirs" / "Game"
    write(mods / "aaa chairs.package", [
        (ResourceKey(restypes.OBJECT_DEFINITION, 0, CHAIR), b"objd, another version")])
    write(game / "Data" / "Client" / "ClientFullBuild0.package",
          [(ResourceKey(restypes.OBJECT_DEFINITION, 0, 0xEA), b"ea")])
    with ResourceIndex(tmp_path / "theirs.sqlite") as ix:
        ix.update(mods, game)
        yield ix


def test_own_export_is_intact_and_already_installed(seaside_zip, index):  # noqa: F811
    r = check_received(seaside_zip, index)
    try:
        assert r.integrity.ok and r.kind == "lot" and r.name == "Seaside"
        assert [i.item.name for i in r.items] == ["Seaside"]
        assert r.packages == ["Seaside_TrayTrim.package"] and r.package_bytes > 0
        assert {x.ref.instance for x in r.cc} == {CHAIR, WALL.instance}
        assert r.overlap and all(o.same for o in r.overlap)
        # Nobody has these: the lamp, the floor and the model override.
        assert ({x.ref.instance for x in r.not_included}
                == {LAMP, FLOOR.instance, OVERRIDE_MODEL.instance})
        assert r.packs_missing == []
    finally:
        r.close()
    assert not r.workdir.exists()


def test_a_differing_copy_and_a_missing_pack(tmp_path, seaside_zip, recipient):
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(seaside_zip) as src, zipfile.ZipFile(edited, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "report.json":
                report = json.loads(data)
                report["game_packs"] = ["EP09", "base"]
                report["gameplay_mods"] = ["Traits/MoreTraits.package"]
                data = json.dumps(report).encode()
            dst.writestr(info, data)
    r = check_received(edited, recipient)
    try:
        [chair] = r.overlap
        assert chair.ref.ref.instance == CHAIR and chair.yours.rel == "aaa chairs.package"
        assert not chair.same and chair.theirs_loads  # "Seaside_..." sorts after "aaa ..."
        assert r.packs_needed == ["EP09", "base"] and r.packs_missing == ["EP09"]
        assert r.gameplay_mods == ["Traits/MoreTraits.package"]
    finally:
        r.close()


def test_one_item_on_its_own(tmp_path, index):  # noqa: F811
    chair = item_ref(ResourceKey(restypes.OBJECT_DEFINITION, 0, CHAIR), index)
    result = export_item(chair, "Rattan Chair", build_plan([chair], index),
                         ExportOptions(tmp_path / "out", "Rattan_Chair"), index)
    r = check_received(result.zip_path, index)
    try:
        assert r.integrity.ok and r.kind == "item" and r.name == "Rattan Chair"
        assert r.items == [] and [x.ref.kind for x in r.cc] == [RefKind.OBJECT]
    finally:
        r.close()


def test_any_zip_with_tray_files_and_packages(tmp_path, index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    other = tmp_path / "from another tool.zip"
    with zipfile.ZipFile(other, "w") as z:
        for f in lot.files:
            z.write(f, f"Seaside/Tray/{f.name}")
        for name in ("chair.package", "walls.package"):
            z.write(tmp_path / "Mods" / name, f"Seaside/Mods/CC/{name}")
        z.writestr("../../escape.package", b"not a package")  # never written outside
    r = check_received(other, index)
    try:
        assert r.integrity.ok and r.report is None and r.kind == "lot"
        assert {x.ref.instance for x in r.cc} == {CHAIR, WALL.instance}
        assert sorted(r.packages) == ["chair.package", "escape.package", "walls.package"]
        assert all(p.parent == r.workdir / "Mods" for p in (r.workdir / "Mods").iterdir())
    finally:
        r.close()


def test_damaged_and_unreadable_zips(tmp_path, seaside_zip, index):  # noqa: F811
    missing = check_received(tmp_path / "gone.zip", index)
    missing.close()
    assert not missing.integrity.ok and missing.items == []
    data = bytearray(seaside_zip.read_bytes())
    at = data.find(b"DBPF") + 200
    data[at:at + 4] = b"\xff\xff\xff\xff"
    damaged = tmp_path / "damaged.zip"
    damaged.write_bytes(bytes(data))
    r = check_received(damaged, index)
    r.close()
    assert not r.integrity.ok



def test_without_a_game_nothing_from_the_game_is_called_missing(tmp_path, seaside_zip):
    with ResourceIndex(tmp_path / "no game.sqlite") as no_game:
        no_game.update(tmp_path / "Mods", None)
        r = check_received(seaside_zip, no_game)
    try:
        assert not r.game_known and r.not_included == [] and r.packs_missing == []
    finally:
        r.close()


def test_the_folder_waits_for_jobs_still_reading_it(tmp_path, seaside_zip, index):  # noqa: F811
    r = check_received(seaside_zip, index)
    r.jobs = 1
    r.close()
    assert r.workdir.exists()  # a thumbnail job is still reading it
    r.job_done()
    assert not r.workdir.exists()


def test_an_encrypted_file_is_reported_not_raised(tmp_path, index):  # noqa: F811
    plain = tmp_path / "secret.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("notes.txt", b"hello")
    data = bytearray(plain.read_bytes())
    for sig in (b"PK\x03\x04", b"PK\x01\x02"):  # set the "encrypted" flag in both headers
        at = data.find(sig)
        flags = at + (6 if sig == b"PK\x03\x04" else 8)
        data[flags] |= 1
    plain.write_bytes(bytes(data))
    r = check_received(plain, index)
    r.close()
    assert not r.integrity.ok and "notes.txt" in r.integrity.checks[0].problems[0]


def test_zip_names_never_point_outside_the_temporary_folder():
    from traytrim.received import _safe_name

    assert _safe_name("Seaside/Mods/CC/chair.package") == "chair.package"
    assert _safe_name("Mods\\walls.package") == "walls.package"
    assert _safe_name("../../escape.package") == "escape.package"
    for bad in ("Mods/D:evil.package", "x/..", "trailing. ", "NUL.package", "con", "a\x01b"):
        assert _safe_name(bad) is None, bad


def test_a_full_disk_is_not_called_a_damaged_zip(seaside_zip):
    from traytrim.verify import MemberDamaged, copy_member

    class FullDisk:
        def write(self, _data):
            raise OSError(28, "No space left on device")

    with zipfile.ZipFile(seaside_zip) as z:
        name = next(n for n in z.namelist() if n.endswith(".package"))
        with pytest.raises(OSError) as raised:
            copy_member(z, name, FullDisk())
    assert not isinstance(raised.value, MemberDamaged)


def test_a_damaged_offset_or_a_garbled_name_is_a_damaged_zip(tmp_path, index):  # noqa: F811
    from traytrim.verify import MemberDamaged, copy_member

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as z:
        z.writestr("Tray/abc.trayitem", b"x" * 100)
    data = plain.read_bytes()
    central = data.find(b"PK\x01\x02")
    # The central directory's offset of the local header (at +42) far past the end.
    bad_offset = bytearray(data)
    bad_offset[central + 42:central + 46] = (0x7FFFFFF0).to_bytes(4, "little")
    (tmp_path / "offset.zip").write_bytes(bytes(bad_offset))
    with zipfile.ZipFile(tmp_path / "offset.zip") as z, pytest.raises(MemberDamaged):
        copy_member(z, "Tray/abc.trayitem")
    # A name flagged as UTF-8 (bit 11) that isn't.
    garbled = bytearray(data)
    garbled[central + 9] |= 0x08  # bit 11 of the flags word is bit 3 of its second byte
    name_at = central + 46
    garbled[name_at:name_at + 3] = b"\xed\xa0\x80"
    (tmp_path / "garbled.zip").write_bytes(bytes(garbled))
    r = check_received(tmp_path / "garbled.zip", index)
    r.close()
    assert not r.integrity.ok  # reported, not raised


def test_limits_on_what_a_zip_may_claim(tmp_path, seaside_zip, index, monkeypatch):  # noqa: F811
    import shutil as shutil_module
    from collections import namedtuple

    import traytrim.received as received

    monkeypatch.setattr(received, "MAX_ENTRIES", 2)
    r = check_received(seaside_zip, index)
    r.close()
    assert not r.integrity.ok and "far more than a tray export" in r.integrity.checks[0].detail
    monkeypatch.setattr(received, "MAX_ENTRIES", 100_000)

    monkeypatch.setattr(received, "MAX_TRAY_FILE", 10)  # every tray file is "too large"
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(seaside_zip) as src, zipfile.ZipFile(other, "w") as dst:
        for info in src.infolist():
            if info.filename != "report.json":
                dst.writestr(info.filename, src.read(info))
    r = check_received(other, index)
    r.close()
    assert not r.integrity.ok and "too large" in r.integrity.checks[0].problems[0]
    monkeypatch.setattr(received, "MAX_TRAY_FILE", 256 << 20)

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil_module, "disk_usage", lambda _p: usage(1, 1, 1000))
    with pytest.raises(OSError, match="needs"):
        check_received(seaside_zip, index)


def test_temporary_folders_left_behind_are_removed_after_a_day(tmp_path):
    import os
    import time as time_module

    from traytrim.received import remove_stale_temp

    old, new = tmp_path / "traytrim-received-old", tmp_path / "traytrim-check-new"
    other = tmp_path / "someone-elses"
    for d in (old, new, other):
        d.mkdir()
        (d / "file").write_bytes(b"x")
    two_days_ago = time_module.time() - 2 * 24 * 3600
    for d in (old, other):
        os.utime(d, (two_days_ago, two_days_ago))
    assert remove_stale_temp(root=tmp_path) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_package_names_differing_only_in_case_are_both_kept(tmp_path, index, tray_dir):  # noqa: F811
    lot = items_by_kind(tray_dir)["lot"]
    other = tmp_path / "case.zip"
    with zipfile.ZipFile(other, "w") as z:
        for f in lot.files:
            z.write(f, f"Tray/{f.name}")
        z.write(tmp_path / "Mods" / "chair.package", "CC/Chair.package")
        z.write(tmp_path / "Mods" / "walls.package", "Other/chair.package")
    r = check_received(other, index)
    try:
        kept = sorted(p.name for p in (r.workdir / "Mods").iterdir())
        assert len(kept) == 2 and len({k.casefold() for k in kept}) == 2
        assert {x.ref.instance for x in r.cc} == {CHAIR, WALL.instance}  # neither overwritten
    finally:
        r.close()


def test_a_report_with_fields_of_the_wrong_type(tmp_path, seaside_zip, index):  # noqa: F811
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(seaside_zip) as src, zipfile.ZipFile(edited, "w") as dst:
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "report.json":
                report = json.loads(data)
                report["game_packs"], report["gameplay_mods"] = "EP01", "Traits.package"
                data = json.dumps(report).encode()
            dst.writestr(info, data)
    r = check_received(edited, index)
    r.close()
    assert r.packs_needed == [] and r.gameplay_mods == []  # not split into letters
