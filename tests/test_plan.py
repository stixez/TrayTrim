"""Dependency following on small synthetic packages."""

import struct

import pytest

from traytrim import restypes
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ResourceIndex
from traytrim.plan import build_plan
from traytrim.references import casp_keys, geom_keys, references, scan_instances
from traytrim.resolve import LocatedRef
from traytrim.tray import RefKind, TrayRef

CC = 0x80000000


def casp_bytes(keys, version=46, filler=b"\x00" * 40) -> bytes:
    body = filler
    tgi = bytes([len(keys)]) + b"".join(struct.pack("<QII", k.instance, k.group, k.type)
                                        for k in keys)
    return struct.pack("<II", version, len(body)) + body + tgi


def geom_bytes(texture_keys, external=()) -> bytes:
    tgi = struct.pack("<I", len(texture_keys)) + b"".join(
        struct.pack("<IIQ", k.type, k.group, k.instance) for k in texture_keys)
    payload = b"\x11" * 30  # stands in for shader, vertices, faces...
    # The offset counts from just after itself, so it includes the 4-byte size field.
    chunk = b"GEOM" + struct.pack("<III", 15, 4 + len(payload), len(tgi)) + payload + tgi
    header = struct.pack("<5I", 3, 1, 0, len(external), 1)
    header += struct.pack("<QII", 0x1234, restypes.GEOM, 0)  # internal key (the chunk itself)
    header += b"".join(struct.pack("<QII", k.instance, k.type, k.group) for k in external)
    chunk_offset = len(header) + 8
    return header + struct.pack("<II", chunk_offset, len(chunk)) + chunk


def test_casp_keys_follow_the_offset():
    keys = [ResourceKey(restypes.GEOM, CC, 1), ResourceKey(restypes.RLE2, CC, 2)]
    assert casp_keys(casp_bytes(keys)) == keys


def test_geom_keys_from_chunk_and_wrapper():
    tex = [ResourceKey(restypes.DST, 0, 7)]
    ext = [ResourceKey(restypes.RLE2, CC, 8)]
    assert geom_keys(geom_bytes(tex, ext)) == ext + tex


def test_scan_finds_instances_at_any_alignment():
    big = 0x1122334455667788
    data = b"xyz" + struct.pack("<Q", big) + b"tail" + struct.pack("<Q", 5)
    assert scan_instances(data, {big, 0x99AA99AA99AA99AA}) == {big}


def test_scan_ignores_small_instances_and_itself():
    own = ResourceKey(restypes.SIM_MODIFIER, 0, 0xAAAABBBBCCCCDDDD)
    bgeo = ResourceKey(restypes.BLEND_GEOMETRY, 0, 0xAAAABBBBCCCCDDDD)
    small = ResourceKey(restypes.BONE_DELTA, 0, 9)
    data = struct.pack("<QQ", own.instance, 9)
    found = references(own, data, {own.instance: [own, bgeo], 9: [small]})
    assert [(r.key, r.how) for r in found] == [(bgeo, "scan")]


def write(path, resources):
    path.parent.mkdir(parents=True, exist_ok=True)
    with PackageWriter(path) as w:
        for key, data in resources:
            w.add(key, data)


@pytest.fixture
def world(tmp_path):
    """Mods with a hair (mesh + textures + thumbnail), a recolor that needs a mesh from another
    package, a slider with its shape data, a duplicate with different content, and the game."""
    mods, game = tmp_path / "Mods", tmp_path / "Game"
    hair = ResourceKey(restypes.CASP, CC, 0x100)
    mesh = ResourceKey(restypes.GEOM, CC, 0x101)
    diffuse = ResourceKey(restypes.RLE2, CC, 0x102)
    ea_tex = ResourceKey(restypes.DST, 0, 0x900)
    stored_as_rles = ResourceKey(restypes.DST, 0, 0x103)  # referenced as DST, stored as RLES
    write(mods / "hair.package", [
        (hair, casp_bytes([ResourceKey(0, 0, 0), mesh, diffuse])),
        (mesh, geom_bytes([ea_tex, stored_as_rles])),
        (diffuse, b"pixels"),
        (ResourceKey(restypes.RLES, 0, 0x103), b"spec"),
        (ResourceKey(restypes.THUM, 1, 0x100), b"thumb"),
        (ResourceKey(restypes.THUM, 1, 0x555), b"someone else's thumb"),
    ])
    recolor = ResourceKey(restypes.CASP, CC, 0x200)
    shared_mesh = ResourceKey(restypes.GEOM, CC, 0x201)
    write(mods / "mesh pack.package", [(shared_mesh, geom_bytes([]))])
    write(mods / "recolor.package", [(recolor, casp_bytes([shared_mesh]))])
    slider = ResourceKey(restypes.SIM_MODIFIER, 0, 0xAAAABBBBCCCC0001)
    shape = ResourceKey(restypes.DEFORMER_MAP, 0, 0xAAAABBBBCCCC0002)
    write(mods / "slider.package", [(slider, b"hdr" + struct.pack("<Q", shape.instance)),
                                    (shape, b"shape v1")])
    write(mods / "zz slider copy.package", [(slider, b"hdr" + struct.pack("<Q", shape.instance)),
                                            (shape, b"shape v2!")])
    write(game / "Data" / "Client" / "ClientFullBuild0.package", [(ea_tex, b"ea")])
    index = ResourceIndex(tmp_path / "index.sqlite")
    index.update(mods, game)
    yield index, dict(hair=hair, mesh=mesh, diffuse=diffuse, ea_tex=ea_tex, recolor=recolor,
                      shared_mesh=shared_mesh, slider=slider, shape=shape)
    index.close()


def located(index, kind, key):
    return LocatedRef(TrayRef(kind, key.instance), keys=[key], mods=index.mods_hits(key),
                      game=index.game_hits(key))


def test_plan_follows_meshes_textures_thumbnails_and_scans(world):
    index, k = world
    refs = [located(index, RefKind.CAS_PART, k["hair"]),
            located(index, RefKind.CAS_PART, k["recolor"]),
            located(index, RefKind.SIM_MODIFIER, k["slider"])]
    plan = build_plan(refs, index)
    got = {(key.type, key.instance): r.via for key, r in plan.resources.items()}
    assert got == {
        (restypes.CASP, 0x100): "tray", (restypes.GEOM, 0x101): "tgi",
        (restypes.RLE2, 0x102): "tgi", (restypes.RLES, 0x103): "texture",
        (restypes.THUM, 0x100): "thumbnail",
        (restypes.CASP, 0x200): "tray", (restypes.GEOM, 0x201): "tgi",
        (restypes.SIM_MODIFIER, 0xAAAABBBBCCCC0001): "tray",
        (restypes.DEFORMER_MAP, 0xAAAABBBBCCCC0002): "scan",
    }
    assert plan.from_game == {k["ea_tex"]: {"base"}}
    assert plan.missing == {}
    assert plan.resources[k["shared_mesh"]].copy.rel == "mesh pack.package"
    shape = plan.resources[k["shape"]]
    assert shape.conflict and shape.copy.rel == "zz slider copy.package"  # loads last
    assert not plan.resources[k["slider"]].conflict  # identical copies aren't a conflict
    assert plan.resources[k["mesh"]].owner == TrayRef(RefKind.CAS_PART, 0x100)
    assert len(plan.packages_with_copies) == 5 and plan.whole_packages_size() > 0


def test_excluded_items_and_missing_dependencies(world, tmp_path):
    index, k = world
    hair = located(index, RefKind.CAS_PART, k["hair"])
    plan = build_plan([hair], index, excluded={hair.ref})
    assert plan.resources == {}

    broken = ResourceKey(restypes.CASP, CC, 0x300)
    gone = ResourceKey(restypes.GEOM, CC, 0x301)
    write(tmp_path / "Mods" / "broken.package", [(broken, casp_bytes([gone]))])
    index.update(tmp_path / "Mods", tmp_path / "Game")
    plan = build_plan([located(index, RefKind.CAS_PART, broken)], index)
    assert plan.missing == {gone: {broken}}


def test_damaged_mesh_is_reported_not_fatal(tmp_path):
    from traytrim.references import ReferenceParseError

    good = geom_bytes([ResourceKey(restypes.DST, 0, 7)])
    # Claim two chunks but end right after the first chunk-table entry.
    damaged = struct.pack("<5I", 3, 1, 0, 0, 2) + good[20:20 + 32 + 8]
    with pytest.raises(ReferenceParseError):
        geom_keys(damaged)
    key = ResourceKey(restypes.GEOM, CC, 1)
    cut = good[:-6]  # key list cut short
    with pytest.raises(ReferenceParseError):
        references(key, cut, {})
