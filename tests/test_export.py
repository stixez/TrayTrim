"""Export and verification on the synthetic Mods/game from test_plan."""

import json
import sys
import zipfile

import pytest

from test_plan import located, world  # noqa: F401  (fixture)
from traytrim import dbpf
from traytrim.export import (ExportOptions, default_base_name, export_household, load_history,
                             safe_name)
from traytrim.plan import build_plan
from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayItem, TraySim
from traytrim.verify import verify_export


@pytest.fixture
def household(tmp_path, world):  # noqa: F811
    index, k = world
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"]),
            located(index, RefKind.SIM_MODIFIER, k["slider"])]
    for r in refs:
        r.usage = {1: {"Everyday outfit 1"}}
        r.names = {1: "Camille Moreau"}
    tray_dir = tmp_path / "Tray"
    tray_dir.mkdir()
    files = []
    for name in ("0x00000001!0x0000000000000abc.trayitem",
                 "0x00000000!0x0000000000000abc.householdbinary"):
        (tray_dir / name).write_bytes(b"tray data " + name.encode())
        files.append(tray_dir / name)
    item = TrayItem(0xABC, 1, "Moreau", files=files)
    contents = HouseholdContents("Moreau", [SimContents(TraySim(1, "Camille", "Moreau"))], 2)
    return index, item, contents, refs


@pytest.fixture(autouse=True)
def private_config(tmp_path, monkeypatch):
    monkeypatch.setattr("traytrim.export.config_dir", lambda: tmp_path / "config")


def test_safe_names():
    assert safe_name("Camille Moreau") == "Camille_Moreau"
    assert safe_name("A/B: C?*") == "A_B_C"
    assert safe_name("!!!") == "Household"
    assert safe_name("Ана Иванова") == "Ана_Иванова"


def test_export_writes_verified_zip_and_test_kit(tmp_path, household):
    index, item, contents, refs = household
    plan = build_plan(refs, index)
    out = tmp_path / "Desktop"
    base = default_base_name(item, contents, {1})
    assert base == "Camille_Moreau"
    result = export_household(item, contents, refs, plan, [], {1},
                              ExportOptions(out, base, True, tmp_path / "Sims 4"), index)
    assert result.verification.ok, [c.problems for c in result.verification.checks]
    assert result.zip_path == out / "Camille_Moreau_TrayTrim.zip"
    with zipfile.ZipFile(result.zip_path) as z:
        names = set(z.namelist())
        assert names == {"Mods/Camille_Moreau_TrayTrim.package", "README.txt", "report.json",
                         "Tray/0x00000001!0x0000000000000abc.trayitem",
                         "Tray/0x00000000!0x0000000000000abc.householdbinary"}
        report = json.loads(z.read("report.json"))
        z.extract("Mods/Camille_Moreau_TrayTrim.package", tmp_path / "unzipped")
    assert len(report["files"]) == len(plan.resources)
    assert all(c["passed"] for c in report["verification"])
    with dbpf.Package(tmp_path / "unzipped" / "Mods" / "Camille_Moreau_TrayTrim.package") as pkg:
        assert {e.key for e in pkg} == set(plan.resources)
    # Nothing but the zip and the test kit is left in the output folder.
    assert sorted(p.name for p in out.iterdir()) == ["Camille_Moreau_TrayTrim.zip",
                                                     "Camille_Moreau_TrayTrim_test"]
    kit = result.test_kit
    assert (kit / "Mods" / "Camille_Moreau_TrayTrim.package").is_file()
    assert "Mods_TrayTrim_backup" in (kit / "HOW_TO_TEST.txt").read_text(encoding="utf-8")
    if sys.platform == "win32":
        bat = (kit / "1_start_test.bat").read_text(encoding="utf-8")
        assert "TS4_x64.exe" in bat and str(tmp_path / "Sims 4") in bat
    assert load_history()[0]["verified"] is True

    again = export_household(item, contents, refs, plan, [], {1},
                             ExportOptions(out, base, False), index)
    assert again.zip_path.name == "Camille_Moreau_TrayTrim (2).zip"


def test_verification_catches_a_changed_or_missing_file(tmp_path, household):
    index, item, contents, refs = household
    plan = build_plan(refs, index)
    # A package with one resource changed and another left out.
    victim, dropped = sorted(plan.resources)[:2]
    path = tmp_path / "bad.package"
    with dbpf.PackageWriter(path) as w:
        for key, res in plan.resources.items():
            if key == dropped:
                continue
            data = b"tampered" if key == victim else None
            if data is None:
                with dbpf.Package(res.copy.path) as src:
                    w.add_raw(key, src.read_raw(res.copy.entry), res.copy.entry.compression,
                              res.copy.entry.mem_size)
            else:
                w.add(key, data)
    result = verify_export(path, plan, refs, index)
    failed = {c.name for c in result.checks if not c.passed}
    assert "Every file matches its source" in failed
    assert "The package opens and holds the planned files" in failed
    assert not result.ok


def test_left_out_items_are_not_required(tmp_path, household):
    index, item, contents, refs = household
    plan = build_plan(refs, index, excluded={refs[0].ref})
    result = export_household(item, contents, refs, plan, [], {1},
                              ExportOptions(tmp_path / "out", "x", False), index)
    assert result.verification.ok


def test_exports_into_sims_or_game_folders_are_refused(tmp_path, household):
    from traytrim.export import ExportError, forbidden_reason

    index, item, contents, refs = household
    sims, game = tmp_path / "Sims 4", tmp_path / "Game"
    assert forbidden_reason(sims / "Mods" / "exports", sims, game)
    assert forbidden_reason(sims, sims, game)
    assert forbidden_reason(game / "x", sims, game)
    assert forbidden_reason(tmp_path / "Desktop", sims, game) is None
    plan = build_plan(refs, index)
    with pytest.raises(ExportError):
        export_household(item, contents, refs, plan, [], {1},
                         ExportOptions(sims / "Mods", "x", False, sims, game), index)
    assert not (sims / "Mods").exists()


def test_a_changed_source_package_stops_the_export(tmp_path, household):
    from traytrim.export import ExportError

    index, item, contents, refs = household
    plan = build_plan(refs, index)
    # Re-save one source package with its resources in another order, the way an editor
    # would: same content, different positions than the index remembers.
    source = next(iter(plan.resources.values())).copy.path
    with dbpf.Package(source) as pkg:
        entries = [(e, pkg.read_raw(e)) for e in pkg]
    with dbpf.PackageWriter(source) as w:
        w.add(dbpf.ResourceKey(0x12345678, 0, 1), b"new resource first")
        for e, raw in reversed(entries):
            w.add_raw(e.key, raw, e.compression, e.mem_size)
    with pytest.raises(ExportError, match="changed since"):
        export_household(item, contents, refs, plan, [], {1},
                         ExportOptions(tmp_path / "out", "x", False), index)
    assert [p.name for p in (tmp_path / "out").iterdir()] == []
