"""Lots and rooms: the snappy container, the .blueprint and .room layouts, and what an export of
one includes. Files are hand-built to the layouts documented in traytrim.tray."""

import struct

import pytest

from traytrim import protobuf as pb
from traytrim import restypes, snappy, tray
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ResourceIndex
from traytrim.plan import build_plan
from traytrim.resolve import default_excluded, locate
from traytrim.tray import RefKind, TrayRef

f_varint, f_bytes, f_str = pb.field_varint, pb.field_bytes, pb.field_str

LOT_ID = 0x0010175A327A0833
ROOM_ID = 0x025E16A64FEB00CE
CC = 0x80000000
CHAIR, LAMP, EA_SOFA = 0xAAAA000000000001, 0xAAAA000000000002, 0xEEEE000000000001
CHAIR_MODEL = 0xAAAA0000000000F1
WALL = ResourceKey(restypes.WALL, 0, 0xBBBB000000000001)
FLOOR = ResourceKey(restypes.FLOOR, 0, 0xBBBB000000000002)
OVERRIDE_MODEL = ResourceKey(restypes.MODEL, 0, 0xCCCC000000000001)
TRAIT = 0x1234


# Snappy

def varint(n: int) -> bytes:
    return pb.encode_varint(n)


def snappy_literals(data: bytes) -> bytes:
    """A valid snappy stream made only of literals (the simplest encoder there is)."""
    out = bytearray(varint(len(data)))
    for i in range(0, len(data), 65536):
        chunk = data[i:i + 65536]
        n = len(chunk) - 1
        if n < 60:
            out.append(n << 2)
        else:
            extra = (n.bit_length() + 7) // 8
            out.append((59 + extra) << 2)
            out += n.to_bytes(extra, "little")
        out += chunk
    return bytes(out)


def test_snappy_literals_and_copies():
    # "abcd", then an overlapping 1-byte-offset copy of 8, a 2-byte-offset copy, a literal
    stream = (varint(19) + bytes([3 << 2]) + b"abcd"
              + bytes([((8 - 4) << 2) | 1, 4])
              + bytes([((6 - 1) << 2) | 2]) + (12).to_bytes(2, "little")
              + bytes([0 << 2]) + b"X")
    assert snappy.decompress(stream) == b"abcdabcdabcdabcdabX"
    long = bytes(range(256)) * 300
    assert snappy.decompress(snappy_literals(long)) == long


@pytest.mark.parametrize("stream, message", [
    (varint(4) + bytes([1, 0]), "outside"),                     # copy with offset 0
    (varint(8) + bytes([3 << 2]) + b"ab", "past the end"),     # literal cut short
    (varint(9) + bytes([3 << 2]) + b"abcd", "preamble"),       # shorter than it says
    (varint(2) + bytes([3 << 2]) + b"abcd", "preamble says 2"),  # longer than it says
    (b"\xff\xff\xff\xff\xff\x01", "length"),                    # absurd length
])
def test_snappy_rejects_damage(stream, message):
    with pytest.raises(snappy.SnappyError, match=message):
        snappy.decompress(stream)


def test_snappy_size_limit():
    with pytest.raises(snappy.SnappyError, match="more than allowed"):
        snappy.decompress(snappy_literals(b"x" * 100), max_size=50)


# Tray files

def key_message(key: ResourceKey) -> bytes:
    return f_varint(1, key.type) + f_varint(2, key.group) + f_varint(3, key.instance)


def objects_message(definitions, *, model: ResourceKey | None = None) -> bytes:
    objects = b""
    for i, definition in enumerate(definitions):
        field = 15 if i % 2 == 0 else 4  # newer and older saves keep the id in different fields
        obj = f_varint(1, i + 1) + f_varint(field, definition)
        if model is not None and i == 0:
            obj += f_bytes(17, key_message(model))
        objects += f_bytes(2, obj)
    return f_varint(1, LOT_ID) + objects


def bundle(keys) -> bytes:
    """A BNDL v3 bundle with an empty record table and this key table."""
    table1 = 44 + 16 * len(keys)
    table2, table3 = table1, table1 + 12
    head = struct.pack("<11I", 0x424E444C, 3, 0, 0, 0, table1, table3, 40, 0, table2,
                       len(keys))
    body = b"".join(struct.pack("<IIQ", k.type, k.group, k.instance) for k in keys)
    return head + body + b"\x00" * 12


def blueprint(objects: bytes, architecture: bytes) -> bytes:
    body = struct.pack("<III", tray.LOT_MAGIC, 2, len(objects)) + objects + architecture
    packed = snappy_literals(body)
    return struct.pack("<II", 1, len(packed)) + packed


def documented_architecture(keys) -> bytes:
    return struct.pack("<II", 1, 1) + b"\x07" * 16 + struct.pack("<I", 0) + bundle(keys)


def room(version: int, keys, objects: bytes) -> bytes:
    keys_msg = b"".join(f_bytes(1, key_message(k)) for k in keys)
    body = struct.pack("<II", version, len(keys_msg)) + keys_msg + struct.pack("<I", 1)
    if version >= 15:
        body += struct.pack("<I", 0x55)
    body += struct.pack("<I", len(objects)) + objects
    packed = snappy_literals(body)
    return struct.pack("<II", 1, len(packed)) + packed


def lot_trayitem() -> bytes:
    lot = (f_varint(2, 40) + f_varint(3, 30) + f_varint(5, 45200) + f_varint(6, 3)
           + f_varint(7, 2) + pb.field_packed_fixed64(14, [TRAIT]))
    meta = (f_varint(1, LOT_ID) + f_varint(2, tray.ITEM_LOT) + f_str(4, "Seaside")
            + f_bytes(10, f_bytes(1, lot)))
    return struct.pack("<II", 0, len(meta)) + meta


def room_trayitem() -> bytes:
    rm = f_varint(2, 6) + f_varint(3, 4) + f_varint(4, 3100)
    meta = (f_varint(1, ROOM_ID) + f_varint(2, tray.ITEM_ROOM) + f_str(4, "Nursery")
            + f_bytes(10, f_bytes(7, rm)))
    return struct.pack("<II", 0, len(meta)) + meta


@pytest.fixture
def tray_dir(tmp_path):
    d = tmp_path / "Tray"
    d.mkdir()
    (d / f"0x00000002!0x{LOT_ID:016x}.trayitem").write_bytes(lot_trayitem())
    (d / f"0x00000000!0x{LOT_ID:016x}.blueprint").write_bytes(blueprint(
        objects_message([CHAIR, CHAIR, LAMP, EA_SOFA], model=OVERRIDE_MODEL),
        documented_architecture([WALL, FLOOR])))
    (d / f"0x00000003!0x{ROOM_ID:016x}.trayitem").write_bytes(room_trayitem())
    catalog = ResourceKey(restypes.OBJECT_CATALOG, 0, CHAIR)
    (d / f"0x00000000!0x{ROOM_ID:016x}.room").write_bytes(
        room(15, [WALL, catalog], objects_message([CHAIR, LAMP])))
    return d


def items_by_kind(tray_dir):
    items, failed = tray.scan_tray(tray_dir)
    assert failed == []
    return {i.kind: i for i in items}


def test_lot_and_room_metadata(tray_dir):
    items = items_by_kind(tray_dir)
    lot, rm = items["lot"], items["room"]
    assert (lot.name, lot.size, lot.price, lot.bedrooms, lot.bathrooms, lot.lot_traits) == (
        "Seaside", (40, 30), 45200, 3, 2, [TRAIT])
    assert (rm.name, rm.size, rm.price, rm.bedrooms) == ("Nursery", (6, 4), 3100, None)


def test_read_lot(tray_dir):
    contents = tray.read_contents(items_by_kind(tray_dir)["lot"])
    assert contents.kind == "lot" and len(contents.sims) == 1
    build = contents.sims[0]
    assert build.refs == {
        TrayRef(RefKind.OBJECT, CHAIR): {"Placed 2 times"},
        TrayRef(RefKind.OBJECT, LAMP): {"Placed once"},
        TrayRef(RefKind.OBJECT, EA_SOFA): {"Placed once"},
        TrayRef(RefKind.BUILD, OVERRIDE_MODEL.instance, type_id=restypes.MODEL,
                res_group=0): {"Model override"},
        TrayRef(RefKind.BUILD, WALL.instance, type_id=restypes.WALL, res_group=0): {"Build"},
        TrayRef(RefKind.BUILD, FLOOR.instance, type_id=restypes.FLOOR, res_group=0): {"Build"},
    }
    assert build.gameplay_ids == {TRAIT} and build.notes == []


def test_lot_bundle_is_found_by_its_header_when_more_data_comes_first(tray_dir):
    item = items_by_kind(tray_dir)["lot"]
    path = tray.build_file(item)
    # A decoy "LDNB" that fails the offset checks, then the real bundle.
    decoy = b"LDNB" + b"\x00" * 60
    path.write_bytes(blueprint(objects_message([CHAIR]),
                               struct.pack("<II", 7, 0) + decoy + bundle([WALL])))
    refs = tray.read_contents(item).sims[0].refs
    assert TrayRef(RefKind.BUILD, WALL.instance, type_id=restypes.WALL, res_group=0) in refs


def test_lot_without_a_readable_bundle_keeps_its_objects(tray_dir):
    item = items_by_kind(tray_dir)["lot"]
    tray.build_file(item).write_bytes(blueprint(objects_message([CHAIR]), b"\x01\x02\x03"))
    build = tray.read_contents(item).sims[0]
    assert set(build.refs) == {TrayRef(RefKind.OBJECT, CHAIR)}
    assert len(build.notes) == 1 and "only its objects" in build.notes[0]


@pytest.mark.parametrize("version", [13, 15])
def test_read_room(tray_dir, version):
    item = items_by_kind(tray_dir)["room"]
    catalog = ResourceKey(restypes.OBJECT_CATALOG, 0, CHAIR)
    tray.build_file(item).write_bytes(room(version, [WALL, catalog], objects_message([CHAIR])))
    contents = tray.read_contents(item)
    assert contents.kind == "room"
    # The objects' catalog keys are covered by the objects themselves.
    assert set(contents.sims[0].refs) == {
        TrayRef(RefKind.OBJECT, CHAIR),
        TrayRef(RefKind.BUILD, WALL.instance, type_id=restypes.WALL, res_group=0)}


@pytest.mark.parametrize("data, message", [
    (b"\x01\x00", "too short"),
    (struct.pack("<II", 9, 0), "unknown layout"),
    (struct.pack("<II", 1, 3) + b"\x05ab", "damaged"),
])
def test_bad_build_files(tray_dir, data, message):
    item = items_by_kind(tray_dir)["lot"]
    tray.build_file(item).write_bytes(data)
    with pytest.raises(tray.TrayError, match=message):
        tray.read_contents(item)


def test_unknown_lot_layout_is_refused(tray_dir):
    item = items_by_kind(tray_dir)["lot"]
    body = struct.pack("<III", 0x12345678, 2, 0)
    packed = snappy_literals(body)
    tray.build_file(item).write_bytes(struct.pack("<II", 1, len(packed)) + packed)
    with pytest.raises(tray.TrayError, match="doesn't know"):
        tray.read_contents(item)


# What an export of a lot includes

def write(path, resources):
    path.parent.mkdir(parents=True, exist_ok=True)
    with PackageWriter(path) as w:
        for key, data in resources:
            w.add(key, data)


def test_lot_plan_takes_whole_objects_and_leaves_ea_overrides_out(tmp_path, tray_dir):
    mods, game = tmp_path / "Mods", tmp_path / "Game"
    chair = ResourceKey(restypes.OBJECT_DEFINITION, 0, CHAIR)
    model = ResourceKey(restypes.MODEL, 0, CHAIR_MODEL)
    write(mods / "chair.package", [
        (chair, b"objd" + struct.pack("<Q", CHAIR_MODEL)),  # the mesh lives in another package
        (ResourceKey(restypes.OBJECT_CATALOG, 0, CHAIR), b"cobj"),
        (ResourceKey(restypes.OBJECT_THUMB, 1, CHAIR), b"jpeg"),
        (ResourceKey(restypes.OBJECT_THUMB, 1, 0xAAAA0000000000FF), b"another object's"),
    ])
    write(mods / "chair mesh.package", [(model, b"model"),
                                        (ResourceKey(restypes.RLE2, 0, 0x77), b"unrelated")])
    sofa = ResourceKey(restypes.OBJECT_DEFINITION, 0, EA_SOFA)
    write(mods / "unlock sofa.package", [(sofa, b"unlocked")])
    write(mods / "walls.package", [(WALL, b"wall"),
                                   (ResourceKey(restypes.OBJECT_THUMB, 2, WALL.instance), b"jpeg")])
    write(game / "Data" / "Client" / "ClientFullBuild0.package", [(sofa, b"ea sofa"),
                                                                 (FLOOR, b"ea floor")])
    with ResourceIndex(tmp_path / "index.sqlite") as index:
        index.update(mods, game)
        contents = tray.read_contents(items_by_kind(tray_dir)["lot"])
        refs = locate(contents, index)
        excluded = default_excluded(contents, refs)
        assert excluded == {TrayRef(RefKind.OBJECT, EA_SOFA)}
        plan = build_plan(refs, index, excluded)
        assert not index.db.in_transaction  # the cross-package lookup only reads
    got = {(k.type, k.instance): r.via for k, r in plan.resources.items()}
    assert got == {
        (restypes.OBJECT_DEFINITION, CHAIR): "tray",
        (restypes.OBJECT_CATALOG, CHAIR): "object",
        (restypes.OBJECT_THUMB, CHAIR): "object",
        (restypes.MODEL, CHAIR_MODEL): "scan",
        (restypes.WALL, WALL.instance): "tray",
        (restypes.OBJECT_THUMB, WALL.instance): "thumbnail",
    }
    by_kind = {r.ref: r for r in refs}
    assert by_kind[TrayRef(RefKind.OBJECT, LAMP)].where.name == "MISSING"
    floor = TrayRef(RefKind.BUILD, FLOOR.instance, type_id=restypes.FLOOR, res_group=0)
    assert by_kind[floor].where.name == "GAME"


def test_households_keep_their_default_replacements():
    contents = tray.HouseholdContents("Moreau", [], 2)
    assert default_excluded(contents, []) == set()


def test_test_kit_helper_names_the_right_screen(tmp_path):
    from pathlib import Path

    from traytrim.export import _write_bats

    _write_bats(tmp_path, Path("Sims 4"), "Seaside_TrayTrim.package", "lot")
    bat = (tmp_path / "1_start_test.bat").read_text(encoding="utf-8")
    assert "place the lot from the Library" in bat and "Create a Sim" not in bat
