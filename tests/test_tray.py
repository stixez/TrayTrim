"""Tray parsing against small hand-built files that follow the game's schemas."""

import struct

import pytest

from traytrim import protobuf as pb
from traytrim import restypes, tray
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ResourceIndex
from traytrim.resolve import Where, locate
from traytrim.tray import RefKind, TrayRef

# Test files are built with the encoder in traytrim.protobuf.
varint = pb.encode_varint
f_varint = pb.field_varint
f_fixed64 = pb.field_fixed64
f_fixed32 = pb.field_fixed32
f_bytes = pb.field_bytes
f_str = pb.field_str
packed_varints = pb.field_packed_varints
packed_fixed64 = pb.field_packed_fixed64


HH_ID = 0x0058175CD0EB001B
SIM_ID = 0x0658175CD0EB001C
HAIR, TOP, BOTTOM, SCULPT, SLIDER, TONE = 0x1001, 0x1002, 0x1003, 0x2001, 0x3001, 0x4001
EA_PART = 0x9001


def outfit(category: int, parts, body_types, *, packed=True) -> bytes:
    ids = packed_fixed64(1, parts) if packed else b"".join(f_fixed64(1, p) for p in parts)
    return (f_varint(1, 1) + f_varint(2, category) + f_bytes(5, ids)
            + f_bytes(7, packed_varints(1, body_types)))


def face_blob(sculpts, face_mods, body_mods=()) -> bytes:
    out = packed_varints(1, sculpts)
    for key in face_mods:
        out += f_bytes(2, f_varint(1, key) + f_fixed32(2, 0x3F000000))
    for key in body_mods:
        out += f_bytes(3, f_varint(1, key))
    return out


def sim_data() -> bytes:
    outfits = (f_bytes(1, outfit(0, [HAIR, TOP, BOTTOM, EA_PART], [2, 6, 7, 8]))
               + f_bytes(1, outfit(0, [HAIR, TOP], [2, 5], packed=False))
               + f_bytes(1, outfit(1, [HAIR], [2])))
    genetic = (f_bytes(1, face_blob([SCULPT], [SLIDER]))
               + f_bytes(5, f_bytes(1, f_varint(1, HAIR) + f_varint(2, 2))))
    return (f_fixed64(1, SIM_ID) + f_str(5, "Camille") + f_str(6, "Moreau")
            + f_varint(7, 2) + f_varint(8, 32) + f_varint(10, TONE)
            + f_bytes(18, face_blob([SCULPT], [SLIDER], [SLIDER + 1]))
            + f_bytes(21, outfits) + f_bytes(28, genetic)
            + f_bytes(75, f_varint(1, 45) + f_varint(2, 0x5001)))


def household_message() -> bytes:
    household = (f_varint(1, 7) + f_varint(2, HH_ID) + f_str(3, "Moreau") + f_varint(4, 20000)
                 + f_bytes(6, sim_data())
                 + f_bytes(21, f_bytes(1, f_varint(1, 0xDEAD) + f_varint(4, 55242443))))
    return f_bytes(1, household) + f_varint(2, 0)


def householdbinary(version: int) -> bytes:
    msg = household_message()
    if version == 1:
        return struct.pack("<II", 1, len(msg)) + msg + b"\x01\x00\x00\x00\x00"
    body = struct.pack("<II", 0, len(msg)) + msg + b"\x01\x00\x00\x00\x00"
    return struct.pack("<II", 2, len(body)) + body


def trayitem() -> bytes:
    sim_meta = f_str(3, "Camille") + f_str(4, "Moreau") + f_varint(5, SIM_ID) + f_varint(9, 32)
    specific = f_bytes(2, f_varint(1, 1) + f_bytes(2, sim_meta))
    meta = (f_varint(1, 123) + f_varint(2, tray.ITEM_HOUSEHOLD) + f_str(4, "Moreau")
            + f_str(5, "Test household") + f_str(7, "creator") + f_bytes(10, specific))
    return struct.pack("<II", 0, len(meta)) + meta


@pytest.fixture
def tray_dir(tmp_path):
    d = tmp_path / "Tray"
    d.mkdir()
    (d / f"0x00000001!0x{HH_ID:016x}.trayitem").write_bytes(trayitem())
    (d / f"0x00000000!0x{HH_ID:016x}.householdbinary").write_bytes(householdbinary(2))
    (d / f"0x473e1502!0x{HH_ID:016x}.hhi").write_bytes(b"img")
    (d / f"0x00000013!0x{SIM_ID:016x}.sgi").write_bytes(b"img")
    (d / "0x00000013!0x00000000000000ff.sgi").write_bytes(b"other sim")
    (d / "notes.txt").write_text("not a tray file")
    return d


def test_protobuf_reader_handles_packed_and_unpacked():
    f = pb.fields_by_number(packed_fixed64(1, [1, 2]) + f_fixed64(1, 3) + packed_varints(2, [300]))
    assert pb.ints(f[1], fixed64=True) == [1, 2, 3]
    assert pb.ints(f[2]) == [300]


@pytest.mark.parametrize("data", [b"\x0a\x05abc", b"\x08\xff", b"\x0b", b"\x00\x01"])
def test_protobuf_reader_rejects_damage(data):
    with pytest.raises(pb.ProtobufError):
        list(pb.iter_fields(data))


def test_filename_parsing():
    assert tray.parse_tray_filename(f"0x00000001!0x{HH_ID:016x}.trayitem") == (1, HH_ID,
                                                                               "trayitem")
    assert tray.parse_tray_filename("Resource.cfg") is None


def test_scan_tray_groups_files_and_sim_images(tray_dir):
    items, failed = tray.scan_tray(tray_dir)
    assert failed == []
    [item] = items
    assert (item.instance, item.kind, item.name, item.creator) == (HH_ID, "household", "Moreau",
                                                                   "creator")
    assert [s.name for s in item.sims] == ["Camille Moreau"]
    assert sorted(f.suffix for f in item.files) == [".hhi", ".householdbinary", ".sgi",
                                                    ".trayitem"]


def test_damaged_trayitem_is_reported_not_raised(tray_dir):
    (tray_dir / "0x00000001!0x0000000000000abc.trayitem").write_bytes(b"\x00\x00\x00\x00\x05")
    items, failed = tray.scan_tray(tray_dir)
    assert len(items) == 1 and len(failed) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_household_references(tmp_path, version):
    path = tmp_path / "hh.householdbinary"
    path.write_bytes(householdbinary(version))
    hh = tray.read_household(path)
    assert (hh.name, hh.version) == ("Moreau", version)
    [sim] = hh.sims
    assert (sim.sim.sim_id, sim.sim.name) == (SIM_ID, "Camille Moreau")
    refs = {(r.kind, r.instance, r.body_type): uses for r, uses in sim.refs.items()}
    assert refs[(RefKind.CAS_PART, HAIR, 2)] == {"Everyday outfit 1", "Everyday outfit 2",
                                                 "Formal outfit 1", "Genetics"}
    assert refs[(RefKind.CAS_PART, TOP, 6)] == {"Everyday outfit 1"}
    assert refs[(RefKind.CAS_PART, TOP, 5)] == {"Everyday outfit 2"}
    assert refs[(RefKind.SKIN_TONE, TONE, None)] == {"Skin tone"}
    assert refs[(RefKind.SCULPT, SCULPT, None)] == {"Sculpts", "Genetics: Sculpts"}
    assert refs[(RefKind.SIM_MODIFIER, SLIDER, None)] == {"Face sliders", "Genetics: Face sliders"}
    assert refs[(RefKind.SIM_MODIFIER, SLIDER + 1, None)] == {"Body sliders"}
    assert refs[(RefKind.CUSTOM_TATTOO, 0x5001, 45)] == {"Custom tattoo"}
    # Reward parts the household unlocked aren't worn and aren't references.
    assert all(r.instance != 0xDEAD for r in sim.refs)


@pytest.mark.parametrize("data, message", [
    (struct.pack("<I", 9) + bytes(12), "version 9"),
    (struct.pack("<II", 1, 500) + b"\x0a", "truncated"),
    (struct.pack("<II", 1, 2) + b"\x0a\x05", "damaged"),
    (b"\x01", "too short"),
])
def test_bad_household_files(tmp_path, data, message):
    path = tmp_path / "hh.householdbinary"
    path.write_bytes(data)
    with pytest.raises(tray.TrayError, match=message):
        tray.read_household(path)


def test_locate_sorts_cc_game_and_missing(tmp_path):
    mods, game = tmp_path / "Mods", tmp_path / "Game"
    mods.mkdir()
    (game / "Data" / "Client").mkdir(parents=True)
    with PackageWriter(mods / "a.package") as w:
        w.add(ResourceKey(restypes.CASP, 0x80000000, HAIR), b"hair")
        w.add(ResourceKey(restypes.SIM_MODIFIER, 0, SLIDER), b"slider")
    with PackageWriter(mods / "b.package") as w:
        w.add(ResourceKey(restypes.CASP, 0x80000000, HAIR), b"hair v2")
    with PackageWriter(game / "Data" / "Client" / "ClientFullBuild0.package") as w:
        w.add(ResourceKey(restypes.CASP, 0, EA_PART), b"ea")
        w.add(ResourceKey(restypes.SKIN_TONE, 0, TONE), b"tone")
        # Same instance as a CC slider but a different type: must not count as a match.
        w.add(ResourceKey(restypes.BONE_DELTA, 0, SLIDER + 1), b"bond")
    path = tmp_path / "hh.householdbinary"
    path.write_bytes(householdbinary(2))
    hh = tray.read_household(path)
    with ResourceIndex(tmp_path / "index.sqlite") as ix:
        ix.update(mods, game)
        refs = {(r.ref.kind, r.ref.instance): r for r in locate(hh, ix)}

    hair = refs[(RefKind.CAS_PART, HAIR)]
    assert hair.where is Where.CC and hair.used.rel == "b.package" and len(hair.mods) == 2
    assert hair.usage_by_name() == [("Camille Moreau", {"Everyday outfit 1", "Everyday outfit 2",
                                                        "Formal outfit 1", "Genetics"})]
    assert refs[(RefKind.CAS_PART, EA_PART)].where is Where.GAME
    assert refs[(RefKind.SKIN_TONE, TONE)].packs == {"base"}
    assert refs[(RefKind.SIM_MODIFIER, SLIDER)].where is Where.CC
    assert refs[(RefKind.SIM_MODIFIER, SLIDER + 1)].where is Where.MISSING
    assert refs[(RefKind.CUSTOM_TATTOO, 0x5001)].approximate
    # One entry per part even though it appears with two body types.
    assert sum(1 for k in refs if k == (RefKind.CAS_PART, TOP)) == 1
    assert TrayRef(RefKind.CAS_PART, TOP, 6).group == "Clothing"


def test_for_sims_and_summary_and_duplicate_names(tmp_path):
    from traytrim.resolve import LocatedRef, for_sims, sim_display_names, summarize
    from traytrim.tray import HouseholdContents, SimContents, TraySim

    a, b = SimContents(TraySim(1, "Alex", "Smith")), SimContents(TraySim(2, "Alex", "Smith"))
    hh = HouseholdContents("Smith", [a, b], 2)
    assert sim_display_names(hh) == {1: "Alex Smith", 2: "Alex Smith (2)"}
    shared = LocatedRef(TrayRef(RefKind.CAS_PART, 5, 2), usage={1: {"x"}, 2: {"y"}})
    only_b = LocatedRef(TrayRef(RefKind.CAS_PART, 6, 2), usage={2: {"z"}})
    filtered = for_sims([shared, only_b], {1})
    assert [r.ref.instance for r in filtered] == [5] and filtered[0].usage == {1: {"x"}}
    assert shared.usage == {1: {"x"}, 2: {"y"}}  # the original isn't changed
    s = summarize([shared, only_b])
    assert len(s.missing) == 2 and s.packages == set() and s.packs == []


def test_gameplay_ids_from_packed_and_nested_fields():
    from traytrim.tray import _collect_ids

    big1, big2, big3 = 0x1111222233334444, 0x5555666677778888, 0x9999AAAABBBBCCCC
    blob = (f_bytes(10, packed_varints(1, [big1, big2]))     # packed trait ids
            + f_bytes(3, f_fixed64(2, big3))                  # nested fixed64
            + f_varint(4, 7))                                 # small values are ignored
    found = set()
    _collect_ids(blob, found)
    assert {big1, big2, big3} <= found and 7 not in found
