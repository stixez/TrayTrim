"""Export and verification on the synthetic Mods/game from test_plan."""

import sys
import zipfile

import pytest

from test_plan import located, world  # noqa: F401  (fixture)
from traytrim import dbpf
from traytrim.export import (ExportOptions, default_base_name, export_household, export_item,
                             load_history, package_stem, safe_name)
from traytrim.plan import build_plan
from traytrim.tray import HouseholdContents, RefKind, SimContents, TrayItem, TraySim
from traytrim.verify import check_zip, verify_export


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


def test_packages_are_named_like_the_creator_named_the_item():
    from traytrim.names import ItemName, readable_cas_name

    raw = "Nivelle_yfTop_EP02HalterBackZip_SolidBlack_202610231248082585"
    assert (package_stem(readable_cas_name(raw), "x")
            == "Nivelle_yfTop_EP02HalterBackZip_SolidBlack_TT")
    assert package_stem(readable_cas_name("A_A_yuHair_Bob_A_2026010100000000"), "x") == (
        "A_yuHair_Bob_A_TT")  # a repeated prefix once
    assert package_stem(ItemName("Driftwood Rattan Chair", "", "Driftwood Rattan Chair"),
                        "x") == "Driftwood_Rattan_Chair_TT"
    assert package_stem(None, "hair_0100") == "hair_0100_TT"
    assert package_stem(ItemName("???", "", "???"), "") == "Item_TT"
    assert package_stem(ItemName("Aux", "", "Aux"), "") == "Aux_TT"  # not the AUX device
    numbers = readable_cas_name("-1889534009:976845349")  # shown by its package's name
    assert package_stem(numbers, "Lacy_Shoes_0000000000000ABC") == "Lacy_Shoes_0000000000000ABC_TT"
    assert len(package_stem(ItemName("x" * 300, "", "x" * 300), "")) <= 64 + len("_TT")


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
    # One package per CC item, named after it (these test parts have no name: after the
    # package each comes from and its id), and no report.json.
    packages = {"Mods/hair_0000000000000100_TT.package",
                "Mods/mesh_pack_0000000000000200_TT.package",
                "Mods/zz_slider_copy_AAAABBBBCCCC0001_TT.package"}
    with zipfile.ZipFile(result.zip_path) as z:
        names = set(z.namelist())
        assert names == packages | {"README.txt", "Tray/0x00000001!0x0000000000000abc.trayitem",
                                    "Tray/0x00000000!0x0000000000000abc.householdbinary"}
        readme = z.read("README.txt").decode()
        z.extractall(tmp_path / "unzipped")
    assert sorted(result.packages) == sorted(n.split("/")[1] for n in packages)
    assert readme.startswith("How to install Moreau")
    assert "Copy every .package file in Mods/" in readme and "Checked" not in readme
    keys: list = []
    for name in packages:
        with dbpf.Package(tmp_path / "unzipped" / name) as pkg:
            keys += [e.key for e in pkg]
    assert sorted(keys) == sorted(plan.resources)  # every file once, in one package
    assert all(plan.resources[k].owner for k in keys)
    # Nothing but the zip and the test kit is left in the output folder.
    assert sorted(p.name for p in out.iterdir()) == ["Camille_Moreau_TrayTrim.zip",
                                                     "Camille_Moreau_TrayTrim_test"]
    kit = result.test_kit
    assert {p.name for p in (kit / "Mods").iterdir()} == set(result.packages)
    assert "Mods_TrayTrim_backup" in (kit / "HOW_TO_TEST.txt").read_text(encoding="utf-8")
    if sys.platform == "win32":
        bat = (kit / "1_start_test.bat").read_text(encoding="utf-8")
        assert "TS4_x64.exe" in bat and str(tmp_path / "Sims 4") in bat
        assert "load the household in Create a Sim" in bat
    assert load_history()[0]["verified"] is True

    again = export_household(item, contents, refs, plan, [], {1},
                             ExportOptions(out, base, False), index)
    assert again.zip_path.name == "Camille_Moreau_TrayTrim (2).zip"


def test_verification_catches_a_changed_or_missing_file(tmp_path, household):
    index, _item, _contents, refs = household
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
    result = verify_export([path], plan, refs, index)
    failed = {c.name for c in result.checks if not c.passed}
    assert "Every file matches its source" in failed
    assert "The packages open and hold the planned files" in failed
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


def test_one_item_exports_on_its_own(tmp_path, world):  # noqa: F811
    index, k = world
    recolor = located(index, RefKind.CAS_PART, k["recolor"])
    plan = build_plan([recolor], index)
    result = export_item(recolor, "Soft Bob", plan,
                         ExportOptions(tmp_path / "Desktop", "Soft_Bob", True), index)
    assert result.verification.ok, [c.problems for c in result.verification.checks]
    assert result.test_kit is None
    assert result.verification.checks[-1].name == "The item is included"
    with zipfile.ZipFile(result.zip_path) as z:
        assert sorted(z.namelist()) == ["Mods/Soft_Bob_TT.package", "README.txt"]
        readme = z.read("README.txt").decode()
        z.extract("Mods/Soft_Bob_TT.package", tmp_path / "unzipped")
    # The recolor brings the mesh it needs from the other package.
    with dbpf.Package(tmp_path / "unzipped" / "Mods" / "Soft_Bob_TT.package") as pkg:
        assert {e.key for e in pkg} == {k["recolor"], k["shared_mesh"]}
    assert readme.startswith("How to install Soft Bob") and "Tray/" not in readme
    assert check_zip(result.zip_path).ok
    assert load_history()[0]["kind"] == "item" and load_history()[0]["name"] == "Soft Bob"


def test_two_items_with_the_same_name_get_their_own_packages(tmp_path, household):
    from traytrim.export import package_groups, package_names
    from traytrim.names import ItemName

    index, _item, _contents, refs = household
    plan = build_plan(refs, index)
    same = ItemName("Bob", "Me", "Me_yfHair_Bob_Black_202601010000000000")
    names = package_names(package_groups(plan), {r.ref: same for r in refs})
    assert len(set(names.values())) == len(names) == len(package_groups(plan))
    assert sorted(names.values())[0] == "Me_yfHair_Bob_Black_TT.package"
    # Names that only casefold alike ("Straße", "Strasse") are told apart too.
    alike = package_names(package_groups(plan), {
        r.ref: ItemName(t, "", t) for r, t in zip(refs, ("Straße", "Strasse", "STRASSE"),
                                                   strict=True)})
    assert len({n.casefold() for n in alike.values()}) == len(alike)
    assert all(n.startswith("Me_yfHair_Bob_Black") for n in names.values())


def test_what_several_items_need_goes_into_a_shared_package(tmp_path, world):  # noqa: F811
    from test_build import write
    from test_plan import casp_bytes
    from traytrim import restypes
    from traytrim.dbpf import ResourceKey
    from traytrim.export import SHARED, package_groups
    from traytrim.index import ResourceIndex
    from traytrim.plan import build_plan as plan_for

    _index, k = world
    # A second recolor of the same mesh, in a package of its own.
    other = ResourceKey(restypes.CASP, k["recolor"].group, 0x300)
    write(tmp_path / "Mods" / "recolor 2.package", [(other, casp_bytes([k["shared_mesh"]]))])
    with ResourceIndex(tmp_path / "index2.sqlite") as ix:
        ix.update(tmp_path / "Mods", tmp_path / "Game")
        refs = [located(ix, RefKind.CAS_PART, k["recolor"]), located(ix, RefKind.CAS_PART, other)]
        plan = plan_for(refs, ix)
        groups = package_groups(plan)
        assert {r.key for r in groups[SHARED]} == {k["shared_mesh"]}
        assert {r.key for r in groups[refs[0].ref]} == {k["recolor"]}
        assert {r.key for r in groups[refs[1].ref]} == {other}
        item = TrayItem(0xABD, 1, "Pair", files=[])
        contents = HouseholdContents("Pair", [SimContents(TraySim(1, "A", "B"))], 2)
        result = export_household(item, contents, refs, plan, [], {1},
                                  ExportOptions(tmp_path / "out", "Pair", False), ix)
    assert result.verification.ok, [c.problems for c in result.verification.checks]
    assert "Pair_Shared.package" in result.packages and len(result.packages) == 3
    with zipfile.ZipFile(result.zip_path) as z:
        readme = z.read("README.txt").decode()
    assert "Pair_Shared.package holds what several items share" in readme


def test_check_zip_catches_a_package_taken_out(tmp_path, household):
    index, item, contents, refs = household
    result = export_household(item, contents, refs, build_plan(refs, index), [], {1},
                              ExportOptions(tmp_path / "out", "x", False), index)
    assert [c.name for c in check_zip(result.zip_path).checks][-1] == "It holds what was exported"
    assert check_zip(result.zip_path).ok
    cut = tmp_path / "cut.zip"
    with zipfile.ZipFile(result.zip_path) as src, zipfile.ZipFile(cut, "w") as dst:
        dst.comment = src.comment
        for info in src.infolist():
            if info.filename != f"Mods/{result.packages[0]}":
                dst.writestr(info, src.read(info))
    failed = [c for c in check_zip(cut).checks if not c.passed]
    assert [c.name for c in failed] == ["It holds what was exported"]
    assert failed[0].problems == [f"{result.packages[0]}: missing from the zip"]
    # Without TrayTrim's record (re-zipped by hand), it isn't taken for a TrayTrim export.
    bare = tmp_path / "bare.zip"
    with zipfile.ZipFile(result.zip_path) as src, zipfile.ZipFile(bare, "w") as dst:
        for info in src.infolist():
            dst.writestr(info, src.read(info))
    assert check_zip(bare).checks[0].name == "It's a TrayTrim export"


def test_a_record_too_big_for_the_zip_comment_becomes_a_digest(monkeypatch):
    from traytrim import export
    from traytrim.verify import read_zip_comment, record_check

    names = {f"Mods/Creator_yfTop_Item_Number_{i:05}_SolidBlack_TT.package": 3 for i in range(1500)}
    counts = {n.split("/")[1]: c for n, c in names.items()}
    monkeypatch.setattr(export.dbpf, "read_index", lambda path: (None, [None] * 3))
    from pathlib import Path
    comment = export._zip_comment([], "Big", [], [], [Path(n) for n in counts])
    meta = read_zip_comment(comment)
    assert len(comment) <= 0xFFFF and "packages" not in meta and meta["packages_digest"]
    assert record_check(meta, list(names), counts).passed
    fewer = list(names)[1:]
    assert not record_check(meta, fewer, counts).passed  # one taken out
    assert not record_check(meta, list(names), {**counts, next(iter(counts)): 4}).passed
