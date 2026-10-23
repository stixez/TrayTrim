import random
import struct
import zlib

import pytest

from traytrim import dbpf
from traytrim.dbpf import (COMP_DELETED, COMP_NONE, COMP_REFPACK, COMP_REFPACK_STREAMABLE,
                           COMP_ZLIB, DbpfError, Package, PackageWriter, ResourceKey)

CASP = 0x034AEECB
GEOM = 0x015A1849


def build_package(path, resources, *, const_type=None, const_group=None, legacy_index_pos=False):
    """Hand-build a package so the reader is tested against bytes it didn't write itself.

    resources: list of (key, raw_bytes, compression, mem_size).
    """
    body = bytearray(dbpf.HEADER_SIZE)
    placed = []
    for key, raw, comp, msize in resources:
        placed.append((key, len(body), len(raw), msize, comp))
        body += raw
    flags = (1 if const_type is not None else 0) | (2 if const_group is not None else 0)
    index = bytearray(struct.pack("<I", flags))
    if const_type is not None:
        index += struct.pack("<I", const_type)
    if const_group is not None:
        index += struct.pack("<I", const_group)
    for key, off, fsize, msize, comp in placed:
        if const_type is None:
            index += struct.pack("<I", key.type)
        if const_group is None:
            index += struct.pack("<I", key.group)
        index += struct.pack("<5I", key.instance >> 32, key.instance & 0xFFFFFFFF, off,
                             fsize | 0x80000000, msize)
        index += struct.pack("<HH", comp, 1)
    index_pos = len(body)
    body += index
    body[:4] = b"DBPF"
    struct.pack_into("<II", body, 4, 2, 1)
    struct.pack_into("<I", body, 36, len(placed))
    struct.pack_into("<I", body, 44, len(index))
    struct.pack_into("<I", body, 40 if legacy_index_pos else 64, index_pos)
    path.write_bytes(bytes(body))
    return path


def sample_bytes(n, seed=1):
    rng = random.Random(seed)
    words = [b"diffuse", b"normal", b"specular", b"\x00\x00\x00\x00", b"GEOM", b"\xff\xfe"]
    out = bytearray()
    while len(out) < n:
        out += rng.choice(words) if rng.random() < 0.7 else bytes([rng.randrange(256)])
    return bytes(out[:n])


@pytest.mark.parametrize("text", ["1FFFFFFFF:0:0", "0:0", "+1:0:0", "1_0:0:0", "x:0:0"])
def test_resource_key_parse_rejects_bad_text(text):
    with pytest.raises(ValueError):
        ResourceKey.parse(text)


def test_resource_key_rejects_out_of_range_values():
    with pytest.raises(ValueError, match="out of range"):
        ResourceKey(1 << 32, 0, 0)


def test_resource_key_text_round_trip():
    key = ResourceKey(CASP, 0x80000000, 0x00B2D882_12345678)
    assert str(key) == "034AEECB:80000000:00B2D88212345678"
    assert ResourceKey.parse(str(key)) == key
    assert ResourceKey.parse("0x034AEECB-0x80000000-0x00B2D88212345678") == key


def test_reads_hand_built_package_with_all_compressions(tmp_path):
    a, b, c = sample_bytes(500, 1), sample_bytes(3000, 2), sample_bytes(70, 3)
    res = [
        (ResourceKey(CASP, 0, 1), a, COMP_NONE, len(a)),
        (ResourceKey(GEOM, 0, 2), zlib.compress(b), COMP_ZLIB, len(b)),
        (ResourceKey(GEOM, 0, 3), dbpf.refpack_compress(c), COMP_REFPACK, len(c)),
        (ResourceKey(GEOM, 0, 4), dbpf.refpack_compress(b), COMP_REFPACK_STREAMABLE, len(b)),
    ]
    path = build_package(tmp_path / "x.package", res)
    with Package(path) as pkg:
        assert [e.key for e in pkg] == [r[0] for r in res]
        assert [pkg.read(e) for e in pkg] == [a, b, c, b]
        assert pkg.read_raw(pkg.entries[1]) == res[1][1]


def test_constant_type_and_group_fields(tmp_path):
    data = b"hello"
    res = [(ResourceKey(GEOM, 7, i), data, COMP_NONE, 5) for i in range(3)]
    path = build_package(tmp_path / "c.package", res, const_type=GEOM, const_group=7)
    with Package(path) as pkg:
        assert [e.key for e in pkg] == [r[0] for r in res]
        assert all(pkg.read(e) == data for e in pkg)


def test_legacy_index_position(tmp_path):
    res = [(ResourceKey(CASP, 0, 9), b"abc", COMP_NONE, 3)]
    path = build_package(tmp_path / "l.package", res, legacy_index_pos=True)
    with Package(path) as pkg:
        assert pkg.read(pkg.entries[0]) == b"abc"


def test_deleted_records_are_skipped(tmp_path):
    res = [(ResourceKey(CASP, 0, 1), b"", COMP_DELETED, 0),
           (ResourceKey(CASP, 0, 2), b"live", COMP_NONE, 4)]
    path = build_package(tmp_path / "d.package", res)
    with Package(path) as pkg:
        assert [e.key.instance for e in pkg] == [2]


def test_deleted_record_hides_earlier_live_entry(tmp_path):
    key = ResourceKey(CASP, 0, 1)
    path = build_package(tmp_path / "del.package",
                         [(key, b"old", COMP_NONE, 3), (key, b"", COMP_DELETED, 0)])
    with Package(path) as pkg:
        assert pkg.find(key) is None


def test_find_returns_last_duplicate(tmp_path):
    key = ResourceKey(CASP, 0, 1)
    path = build_package(tmp_path / "dup.package",
                         [(key, b"old", COMP_NONE, 3), (key, b"new", COMP_NONE, 3)])
    with Package(path) as pkg:
        assert pkg.read(pkg.find(key)) == b"new"
        assert pkg.find(ResourceKey(CASP, 0, 99)) is None


def test_entry_without_extended_flag_has_no_compression_field(tmp_path):
    body = bytearray(dbpf.HEADER_SIZE) + b"raw!"
    index = struct.pack("<I7I", 0, CASP, 0, 0, 5, dbpf.HEADER_SIZE, 4, 4)
    pos = len(body)
    body += index
    body[:4] = b"DBPF"
    struct.pack_into("<II", body, 4, 2, 1)
    struct.pack_into("<I", body, 36, 1)
    struct.pack_into("<I", body, 44, len(index))
    struct.pack_into("<I", body, 64, pos)
    (tmp_path / "n.package").write_bytes(bytes(body))
    with Package(tmp_path / "n.package") as pkg:
        assert pkg.entries[0].compression == COMP_NONE
        assert pkg.read(pkg.entries[0]) == b"raw!"


@pytest.mark.parametrize("content, message", [
    (b"NOPE" + bytes(92), "not a Sims 4 package"),
    (b"DBPF", "not a Sims 4 package"),
])
def test_rejects_non_packages(tmp_path, content, message):
    path = tmp_path / "bad.package"
    path.write_bytes(content)
    with pytest.raises(DbpfError, match=message):
        Package(path)


def test_rejects_index_outside_file(tmp_path):
    path = build_package(tmp_path / "t.package", [(ResourceKey(CASP, 0, 1), b"abc", COMP_NONE, 3)])
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, 44, 10_000)
    path.write_bytes(bytes(data))
    with pytest.raises(DbpfError, match="outside the file"):
        Package(path)


def test_size_mismatch_is_reported(tmp_path):
    path = build_package(tmp_path / "m.package", [(ResourceKey(CASP, 0, 1), b"abc", COMP_NONE, 4)])
    with Package(path) as pkg, pytest.raises(DbpfError, match="index says 4"):
        pkg.read(pkg.entries[0])


def test_unknown_compression_is_reported(tmp_path):
    path = build_package(tmp_path / "u.package", [(ResourceKey(CASP, 0, 1), b"abc", 0x1234, 3)])
    with Package(path) as pkg, pytest.raises(DbpfError, match="0x1234"):
        pkg.read(pkg.entries[0])


@pytest.mark.parametrize("data", [
    b"",
    b"a",
    b"abc",
    b"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",  # overlapping copy
    bytes(5000),  # long runs, 4-byte opcodes
    sample_bytes(20_000, 7),
    random.Random(1).randbytes(3000),  # incompressible, long literal runs
    sample_bytes(1000, 3) + random.Random(2).randbytes(20_000) + sample_bytes(1000, 3),  # far
], ids=["empty", "one", "three", "overlap", "zeros", "mixed", "random", "far"])
def test_refpack_round_trip(data):
    assert dbpf.refpack_decompress(dbpf.refpack_compress(data)) == data


def test_refpack_known_vector():
    # Hand-assembled: 4 literals "abcd" via 0xE0, then a 2-byte copy of 6 bytes at distance 4
    # ("abcdab"), then stop code 0xFC with no trailing literals.
    stream = bytes([0x10, 0xFB, 0, 0, 10, 0xE0]) + b"abcd" + bytes([(6 - 3) << 2, 3, 0xFC])
    assert dbpf.refpack_decompress(stream) == b"abcdabcdab"


def test_refpack_with_compressed_size_field():
    stream = bytes([0x11, 0xFB, 0, 0, 9, 0, 0, 3, 0xFF]) + b"xyz"
    assert dbpf.refpack_decompress(stream) == b"xyz"


@pytest.mark.parametrize("stream, message", [
    (b"\x10\xFA\x00\x00\x01", "not valid RefPack"),
    (bytes([0x10, 0xFB, 0, 0, 8, 0xE0]) + b"ab", "truncated"),
    (bytes([0x10, 0xFB, 0, 0, 4, 0x00, 0x05]), "before the start"),
    (bytes([0x10, 0xFB, 0, 0, 9, 0xFC]), "header says 9"),
])
def test_refpack_rejects_bad_streams(stream, message):
    with pytest.raises(DbpfError, match=message):
        dbpf.refpack_decompress(stream)


def test_writer_round_trip_keeps_payloads_identical(tmp_path):
    src_res = [
        (ResourceKey(CASP, 0, 1), sample_bytes(800, 4)),
        (ResourceKey(GEOM, 0x80000000, 0xFFFFFFFF_00000001), sample_bytes(5000, 5)),
    ]
    refpacked = sample_bytes(2000, 6)
    src = build_package(tmp_path / "src.package", [
        (ResourceKey(GEOM, 0, 3), dbpf.refpack_compress(refpacked), COMP_REFPACK, len(refpacked)),
    ])
    out = tmp_path / "out.package"
    with PackageWriter(out) as w, Package(src) as s:
        for key, data in src_res:
            w.add(key, data)
        e = s.entries[0]
        w.add_raw(e.key, s.read_raw(e), e.compression, e.mem_size)
        w.add(ResourceKey(CASP, 0, 4), b"stored", compress=False)

    with Package(out) as pkg:
        got = {e.key: pkg.read(e) for e in pkg}
        assert got == {**dict(src_res), ResourceKey(GEOM, 0, 3): refpacked,
                       ResourceKey(CASP, 0, 4): b"stored"}
        assert [e.compression for e in pkg] == [COMP_ZLIB, COMP_ZLIB, COMP_REFPACK, COMP_NONE]
        assert pkg.header.major == 2 and pkg.header.minor == 1


def test_writer_rejects_duplicate_keys(tmp_path):
    with PackageWriter(tmp_path / "w.package") as w:
        w.add(ResourceKey(CASP, 0, 1), b"a")
        with pytest.raises(ValueError, match="duplicate"):
            w.add(ResourceKey(CASP, 0, 1), b"b")


def test_empty_package_round_trip(tmp_path):
    with PackageWriter(tmp_path / "e.package"):
        pass
    with Package(tmp_path / "e.package") as pkg:
        assert len(pkg) == 0


def test_zlib_with_trailing_garbage_is_rejected(tmp_path):
    raw = zlib.compress(b"hello") + b"GARBAGE"
    path = build_package(tmp_path / "z.package", [(ResourceKey(CASP, 0, 1), raw, COMP_ZLIB, 5)])
    with Package(path) as pkg, pytest.raises(DbpfError, match="doesn't end"):
        pkg.read(pkg.entries[0])


def test_zlib_larger_than_index_says_is_stopped_early(tmp_path):
    raw = zlib.compress(bytes(10_000_000))
    path = build_package(tmp_path / "b.package", [(ResourceKey(CASP, 0, 1), raw, COMP_ZLIB, 100)])
    with Package(path) as pkg, pytest.raises(DbpfError):
        pkg.read(pkg.entries[0])


def test_refpack_longer_than_header_says_is_stopped():
    stream = bytes([0x10, 0xFB, 0, 0, 2, 0xE0]) + b"abcd" + bytes([0xFC])
    with pytest.raises(DbpfError, match="past the 2 bytes"):
        dbpf.refpack_decompress(stream)


def test_failed_write_leaves_no_file(tmp_path):
    out = tmp_path / "x.package"
    with pytest.raises(RuntimeError), PackageWriter(out) as w:
        w.add(ResourceKey(CASP, 0, 1), b"a")
        raise RuntimeError("export failed")
    assert list(tmp_path.iterdir()) == []


def test_close_is_idempotent_and_blocks_further_adds(tmp_path):
    with PackageWriter(tmp_path / "x.package") as w:
        w.add(ResourceKey(CASP, 0, 1), b"a")
        w.close()
        with pytest.raises(ValueError, match="already closed"):
            w.add(ResourceKey(CASP, 0, 2), b"b")
    with Package(tmp_path / "x.package") as pkg:
        assert len(pkg) == 1
    assert sorted(p.name for p in tmp_path.iterdir()) == ["x.package"]


def test_oversized_resource_is_refused_before_writing(tmp_path, monkeypatch):
    monkeypatch.setattr(dbpf, "_MAX_PACKAGE_SIZE", 200)
    with PackageWriter(tmp_path / "x.package") as w, pytest.raises(DbpfError, match="larger than 4 GB"):
        w.add_raw(ResourceKey(CASP, 0, 1), bytes(150), COMP_NONE, 150)


def test_entries_without_an_index_are_rejected(tmp_path):
    path = build_package(tmp_path / "t.package", [(ResourceKey(CASP, 0, 1), b"abc", COMP_NONE, 3)])
    data = bytearray(path.read_bytes())
    struct.pack_into("<I", data, 44, 0)
    path.write_bytes(bytes(data))
    with pytest.raises(DbpfError, match="damaged index"):
        Package(path)
    with pytest.raises(DbpfError, match="damaged index"):
        dbpf.scan_index(path)


def test_a_resource_claiming_to_unpack_huge_is_refused(tmp_path):
    path = tmp_path / "bomb.package"
    key = ResourceKey(0x034AEECB, 0, 1)
    with PackageWriter(path) as w:
        w.add_raw(key, zlib.compress(b"\0" * 1000), dbpf.COMP_ZLIB, dbpf.MAX_RESOURCE_SIZE + 1)
    with dbpf.Package(path) as pkg, pytest.raises(dbpf.DbpfError, match="more than any Sims 4"):
        pkg.read(pkg.find(key))
