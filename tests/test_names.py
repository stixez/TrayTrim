"""CC names from string tables and CAS part internal names, and choosing which copy is used."""

import struct

import pytest

from test_plan import located, world  # noqa: F401  (fixture)
from traytrim import restypes
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ResourceIndex
from traytrim.names import (ItemName, cas_internal_name, catalog_name_hash, clean, item_names,
                            readable_cas_name, string_table)
from traytrim.plan import build_plan
from traytrim.tray import RefKind


def stbl(strings: dict[int, bytes]) -> bytes:
    body = b"".join(struct.pack("<IBH", k, 0, len(v)) + v for k, v in strings.items())
    total = sum(len(v) + 1 for v in strings.values())
    return (b"STBL" + struct.pack("<HBQ", 5, 0, len(strings)) + b"\x00\x00"
            + struct.pack("<I", total) + body)


def catalog(name_hash: int) -> bytes:
    return struct.pack("<IIII", 25, 11, name_hash, 0) + b"\x00" * 20


def casp(name: str, presets: int = 0) -> bytes:
    raw = name.encode("utf-16-be")
    length = b""
    n = len(raw)
    while True:
        b = n & 0x7F
        n >>= 7
        length += bytes([b | (0x80 if n else 0)])
        if not n:
            break
    return struct.pack("<III", 46, 0, presets) + length + raw + b"\x00" * 16


def test_string_tables():
    data = stbl({0x11: b"Leo Fluffy Desk Chair", 0x22: "Spot Léon".encode("cp1252")})
    assert string_table(data) == {0x11: "Leo Fluffy Desk Chair", 0x22: "Spot Léon"}
    assert string_table(b"nope") == {}
    assert catalog_name_hash(catalog(0xABCD)) == 0xABCD and catalog_name_hash(b"x") == 0


def test_cas_names():
    assert cas_internal_name(casp("LVNDR_yfAcc_Mani_beige")) == "LVNDR_yfAcc_Mani_beige"
    assert cas_internal_name(casp("x" * 200)) == "x" * 200  # two-byte length
    assert cas_internal_name(casp("Hair", presets=1)) == ""
    assert cas_internal_name(b"short") == ""


@pytest.mark.parametrize("raw, title, creator", [
    ("LVNDR_yfAcc_ManiRoundSolidColor_beige_202511021041562324",
     "Mani Round Solid Color beige", "LVNDR"),
    ("Prl_Prl_Prl_yuAcc_GlassesFramelessSquare_SolidBeigeDk_201609052328173312_201703072242",
     "Glasses Frameless Square Solid Beige Dk", "Prl"),
    ("BYDIV._yfAcc_PediColor_white", "Pedi Color white", "BYDIV"),
    ("yfHair_Bob", "Bob", ""),
    ("MyHair", "MyHair", ""),
    ("Creator_ymTop", "Creator ymTop", ""),  # nothing after the category: shown as it is
])
def test_readable_cas_names(raw, title, creator):
    assert readable_cas_name(raw) == ItemName(title, creator, raw)


def test_markup_is_dropped():
    assert clean("<font color='#3b3b3b'>SYB Imperium</font> - Concrete  Platform") == (
        "SYB Imperium - Concrete Platform")


def test_item_names_from_packages(tmp_path):
    mods = tmp_path / "Mods"
    mods.mkdir()
    chair = 0xAAAA000000000001
    wall = ResourceKey(restypes.WALL, 0, 0xBBBB000000000001)
    hair = ResourceKey(restypes.CASP, 0x80000000, 0xCCCC000000000001)
    english = ResourceKey(restypes.STRING_TABLE, 0, 0x0012345678901234)
    french = ResourceKey(restypes.STRING_TABLE, 0, 0x0712345678901234)
    with PackageWriter(mods / "cc.package") as w:
        w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, chair), b"objd")
        w.add(ResourceKey(restypes.OBJECT_CATALOG, 0, chair), catalog(0x11))
        w.add(wall, catalog(0x22))
        w.add(hair, casp("Me_yfHair_LongWaves_black_202401010000000000"))
        w.add(french, stbl({0x11: b"Chaise", 0x22: b"Mur"}))
        w.add(english, stbl({0x11: b"Desk Chair"}))
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, tmp_path / "Game")
        refs = [located(ix, RefKind.OBJECT, ResourceKey(restypes.OBJECT_DEFINITION, 0, chair)),
                located(ix, RefKind.CAS_PART, hair)]
        build = located(ix, RefKind.BUILD, wall)
        refs.append(build)
    names = item_names(refs)
    assert names[refs[0].ref] == ItemName("Desk Chair", "", "Desk Chair")  # English first
    assert names[refs[1].ref].title == "Long Waves black"
    assert names[build.ref].title == "Mur"  # only in French: better than nothing


def test_a_chosen_copy_is_used(world):  # noqa: F811
    index, k = world
    slider = located(index, RefKind.SIM_MODIFIER, k["slider"])
    default = build_plan([slider], index)
    shape = default.resources[k["shape"]]
    assert shape.conflict and shape.copy.rel == "zz slider copy.package" and not shape.chosen
    other = next(h.path for h in shape.copies if h.rel == "slider.package")
    plan = build_plan([slider], index, copies_chosen={k["shape"]: other})
    chosen = plan.resources[k["shape"]]
    assert chosen.copy.rel == "slider.package" and chosen.chosen
    assert plan.export_size == default.export_size - 1  # "shape v1" vs "shape v2!"


def test_a_name_of_numbers_only_gives_way_to_the_package_name():
    from traytrim.names import readable_cas_name

    assert readable_cas_name("-1889534009:976845349").title == ""
    assert readable_cas_name("yfHead").title == "yfHead"


@pytest.mark.parametrize("stem, readable", [
    ("Bergdorf_MiuMiu_WanderTeddyBag_LH", "Bergdorf Miu Miu Wander Teddy Bag LH"),
    ("Nivelle_yfTop_HalterBackZip_SolidBlack_202601011200", "Nivelle Top Halter Back Zip Solid Black"),
    ("[SUNBERRY]V.2 Logo Belt Mini Skirt 24.13", "[SUNBERRY] V.2 Logo Belt Mini Skirt 24.13"),
    ("Sentate-2025-MagnoliaSlingbacks(1)", "Sentate 2025 Magnolia Slingbacks"),
    ("SHOESTOPIA+-+Salopato+High+Heels", "SHOESTOPIA - Salopato High Heels"),
    ("NA - Skyla Heels", "NA - Skyla Heels"),
    ("[B0T0XBRAT]-Long-Dress", "[B0T0XBRAT] Long Dress"),
    ("Creator_Off-Shoulder_T-Shirt", "Creator Off-Shoulder T-Shirt"),
    ("___", "___"),
])
def test_readable_package_names(stem, readable):
    from traytrim.names import readable_package_name

    assert readable_package_name(stem) == readable


def test_a_package_of_one_item_names_it(tmp_path):
    # A real case: a Miu Miu bag whose parts are all "Bergdorf_yfAcc_IndexLeftEyeballRing_...",
    # the EA ring it was cloned from. In a merge, each part keeps its own name.
    from traytrim.gui.describe import display_name

    mods = tmp_path / "Mods"
    mods.mkdir()
    bag = [ResourceKey(restypes.CASP, 0x80000000, 0xB000 + i) for i in range(2)]
    merged = [ResourceKey(restypes.CASP, 0x80000000, 0xC000 + i) for i in range(2)]
    with PackageWriter(mods / "Bergdorf_MiuMiu_WanderTeddyBag_LH.package") as w:
        for key, swatch in zip(bag, ("SolidMagenta", "SolidBlack"), strict=True):
            w.add(key, casp(f"Bergdorf_yfAcc_IndexLeftEyeballRing_{swatch}_202303282111074027"))
    with PackageWriter(mods / "Accessories 1.package") as w:
        w.add(merged[0], casp("Sbe_yfAcc_EarStudsPearl_Cream_202106200325154040"))
        w.add(merged[1], casp("LYNX_yfAcc_GlassesMod_PinkHot_202005271753244432"))
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, tmp_path / "Game")
        refs = [located(ix, RefKind.CAS_PART, k) for k in (bag[0], merged[0])]
    names = item_names(refs)
    bag_name = names[refs[0].ref]
    assert bag_name.title == "Index Left Eyeball Ring Solid Magenta"  # kept for search
    assert display_name(refs[0], bag_name) == "Bergdorf Miu Miu Wander Teddy Bag LH"
    assert bag_name.swatch == "Solid Magenta"
    assert bag_name.full == "Bergdorf Miu Miu Wander Teddy Bag LH \u00b7 Solid Magenta"
    assert display_name(refs[1], names[refs[1].ref]) == "Ear Studs Pearl Cream"
    from traytrim.export import package_stem  # exported under the name the app shows
    assert package_stem(bag_name, "x") == "Bergdorf_Miu_Miu_Wander_Teddy_Bag_LH_Solid_Magenta_TT"


def test_merges_and_sets_are_not_one_item(tmp_path):
    mods = tmp_path / "Mods"
    mods.mkdir()
    # Two creators' parts cloned from one EA dress are two items; so is a file named a merge,
    # and a CAS part next to objects (a set).
    with PackageWriter(mods / "dresses.package") as w:
        w.add(ResourceKey(restypes.CASP, 0, 1), casp("Ann_yfBody_DressMaxi_Lemon"))
        w.add(ResourceKey(restypes.CASP, 0, 2), casp("Bea_yfBody_DressMaxi_Lemon"))
    with PackageWriter(mods / "Hair MERGED.package") as w:
        w.add(ResourceKey(restypes.CASP, 0, 3), casp("Me_yfHair_Bob_black"))
    with PackageWriter(mods / "bedroom set.package") as w:
        w.add(ResourceKey(restypes.CASP, 0, 4), casp("Me_yfAcc_Blanket_white"))
        w.add(ResourceKey(restypes.OBJECT_CATALOG, 0, 5), catalog(0x11))
    with PackageWriter(mods / "bathroom set.package") as w:  # build items make a set too
        w.add(ResourceKey(restypes.CASP, 0, 6), casp("Me_yfAcc_Towel_white"))
        w.add(ResourceKey(restypes.WALL, 0, 7), catalog(0x12))
    with PackageWriter(mods / "tops.package") as w:  # one word in common is not one item
        w.add(ResourceKey(restypes.CASP, 0, 8), casp("Me_yfTop_Halter_Neck_red"))
        w.add(ResourceKey(restypes.CASP, 0, 9), casp("Me_yfTop_Halter_Backless_red"))
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, tmp_path / "Game")
        refs = [located(ix, RefKind.CAS_PART, ResourceKey(restypes.CASP, 0, i))
                for i in (1, 3, 4, 6, 8)]
    names = item_names(refs)
    assert all(not names[r.ref].package for r in refs)


def test_one_item_for_every_age_is_one_item(tmp_path):
    # Real cases: "[Kijiko]Remove-EA-Lashes" has yfHead and ymHead parts; a file name with
    # "merge" inside a word is not a merge.
    mods = tmp_path / "Mods"
    mods.mkdir()
    with PackageWriter(mods / "Submerged_Bob.package") as w:
        for i, frame in enumerate(("yf", "ym", "cu")):
            w.add(ResourceKey(restypes.CASP, 0, i + 1), casp(f"Me_{frame}Hair_Bob_black"))
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, tmp_path / "Game")
        ref = located(ix, RefKind.CAS_PART, ResourceKey(restypes.CASP, 0, 2))
    name = item_names([ref])[ref.ref]
    assert (name.package, name.swatch) == ("Submerged Bob", "black")


@pytest.mark.parametrize("file, merge", [
    ("Hair_Merge", True), ("MERGED hair", True), ("MyMerge", True), ("merged", True),
    ("Submerged_Tail", False), ("Emergency_Uniform", False), ("SUBMERGED", False),
])
def test_merge_names(file, merge):
    from traytrim.names import _MERGE

    assert bool(_MERGE.search(file)) is merge


@pytest.mark.parametrize("raw, swatch", [
    ("Bergdorf_yfAcc_IndexLeftEyeballRing_SolidMagenta_202303282111074027", "Solid Magenta"),
    ("Me_Long_Waves_black", "black"),
    ("Set_Top", ""),  # two parts: a name, no swatch
])
def test_swatch_names(raw, swatch):
    from traytrim.names import swatch_name

    assert swatch_name(raw) == swatch
