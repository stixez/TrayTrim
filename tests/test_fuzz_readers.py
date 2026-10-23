"""The readers of untrusted files (CC packages, tray files, zips, thumbnails) on randomly
damaged input: each may fail only with the errors its callers catch, never anything else (an
unexpected error from one bad CC file would stop a whole index or Mods tab build)."""

import os
import random
import struct
import zipfile

import pytest

import test_build as tb
import test_tray as tt
from test_plan import casp_bytes, geom_bytes
from traytrim import dbpf, names, protobuf, restypes, snappy, tray
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.index import ModsHit, ResourceIndex
from traytrim.references import ReferenceParseError, references
from traytrim.resolve import LocatedRef
from traytrim.tray import RefKind, TrayRef

CASES = 300  # per reader; the seed is fixed, so a failure always comes back the same
INTERESTING = [0, 1, 0x7F, 0x80, 0xFF, 0xFFFF, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF]


def mutate(data: bytes, rng: random.Random) -> bytes:
    b = bytearray(data)
    for _ in range(rng.randint(1, 4)):
        if not b:
            b = bytearray(rng.randbytes(rng.randint(1, 64)))
        op, i = rng.randrange(7), rng.randrange(len(b))
        if op == 0:
            b[i] ^= 1 << rng.randrange(8)
        elif op == 1 and len(b) >= 4:
            i = rng.randrange(len(b) - 3)
            b[i:i + 4] = struct.pack("<I", rng.choice(INTERESTING + [rng.getrandbits(32)]))
        elif op == 2:
            del b[rng.randrange(len(b)):]
        elif op == 3:
            j = rng.randrange(len(b))
            b[i:i] = b[j:j + rng.randint(1, 32)]
        elif op == 4:
            del b[i:i + rng.randint(1, 16)]
        elif op == 5:
            b[i:i] = rng.randbytes(rng.randint(1, 16))
        else:
            b[i] = rng.choice([0, 0xFF, 0x80])
    return bytes(b)


def stbl() -> bytes:
    entries = [(0x11, b"Desk Chair"), (0x22, b"Mur")]
    body = b"".join(struct.pack("<IBH", h, 0, len(t)) + t for h, t in entries)
    return (b"STBL" + struct.pack("<HBQ", 5, 0, len(entries)) + b"\x00\x00"
            + struct.pack("<I", sum(len(t) + 1 for _, t in entries)) + body)


def package(tmp_path) -> bytes:
    mesh = ResourceKey(restypes.GEOM, 0x80000000, 0xAAAA0000000000B1)
    tex = ResourceKey(restypes.RLE2, 0x80000000, 0xAAAA0000000000B2)
    raw = "Me_yfHair_LongWaves".encode("utf-16-be")
    named = struct.pack("<I", 0) + bytes([len(raw)]) + raw + b"\x00" * 40
    path = tmp_path / "seed.package"
    with PackageWriter(path) as w:
        w.add(ResourceKey(restypes.CASP, 0x80000000, 0xAA01),
              struct.pack("<II", 46, len(named)) + named + bytes([1])
              + struct.pack("<QII", mesh.instance, mesh.group, mesh.type))
        w.add(mesh, geom_bytes([tex]))
        w.add(tex, b"pixels" * 50)
        w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, 0xAA02), b"objd" + struct.pack("<Q", 7))
        w.add(ResourceKey(restypes.OBJECT_CATALOG, 0, 0xAA02),
              struct.pack("<II", 0x0B, 0x11) + b"\x00" * 40)
        w.add(ResourceKey(restypes.STRING_TABLE, 0, 0x0011223344556677), stbl())
    return path.read_bytes()


def read_package(path):
    with dbpf.Package(path) as pkg:
        for e in pkg:
            try:
                pkg.read(e)
            except dbpf.DbpfError:
                continue


def item_names_of(path):
    """Names of every resource in a broken package: never raises (it reports nothing)."""
    try:
        with dbpf.Package(path) as pkg:
            entries = list(pkg)
    except (dbpf.DbpfError, OSError):
        return
    refs = []
    for e in entries:
        kind = {restypes.CASP: RefKind.CAS_PART,
                restypes.OBJECT_DEFINITION: RefKind.OBJECT}.get(e.key.type, RefKind.BUILD)
        ref = (TrayRef(kind, e.key.instance, type_id=e.key.type, res_group=e.key.group)
               if kind is RefKind.BUILD else TrayRef(kind, e.key.instance))
        refs.append(LocatedRef(ref, keys=[e.key], mods=[ModsHit(path, path.name, 0, e)]))
    names.item_names(refs)


def build(path, item_type):
    tray.read_build(tray.TrayItem(0xABC, item_type, "x", files=[path]), path)


SCAN_KEYS = {0xAAAA0000000000B1: [ResourceKey(restypes.GEOM, 0, 0xAAAA0000000000B1)]}

# (name, seed, file name or None for bytes, reader, errors its callers catch)
READERS = [
    ("package index", package, "x.package", dbpf.scan_index, (dbpf.DbpfError, OSError)),
    ("package resources", package, "x.package", read_package, (dbpf.DbpfError, OSError)),
    ("package names", package, "x.package", item_names_of, ()),
    ("CAS part references", lambda _t: casp_bytes([ResourceKey(restypes.GEOM, 0, 5)]), None,
     lambda d: references(ResourceKey(restypes.CASP, 0, 1), d, SCAN_KEYS),
     (ReferenceParseError,)),
    ("mesh references", lambda _t: geom_bytes([ResourceKey(restypes.RLE2, 0, 7)]), None,
     lambda d: references(ResourceKey(restypes.GEOM, 0, 1), d, SCAN_KEYS),
     (ReferenceParseError,)),
    ("string table", lambda _t: stbl(), None, names.string_table, (dbpf.DbpfError, struct.error)),
    ("trayitem", lambda _t: tt.trayitem(), "0x00000001!0x0000000000000abc.trayitem",
     tray.read_trayitem, (tray.TrayError,)),
    ("lot trayitem", lambda _t: tb.lot_trayitem(), "0x00000002!0x0000000000000abc.trayitem",
     tray.read_trayitem, (tray.TrayError,)),
    ("household", lambda _t: tt.householdbinary(2), "x.householdbinary", tray.read_household,
     (tray.TrayError, ValueError, OSError)),
    ("lot", lambda _t: tb.blueprint(tb.objects_message([tb.CHAIR]),
                                    tb.documented_architecture([tb.WALL])),
     "0x00000000!0x0000000000000abc.blueprint", lambda p: build(p, tray.ITEM_LOT),
     (tray.TrayError, ValueError, OSError)),
    ("room", lambda _t: tb.room(15, [tb.WALL], tb.objects_message([tb.CHAIR])),
     "0x00000000!0x0000000000000abc.room", lambda p: build(p, tray.ITEM_ROOM),
     (tray.TrayError, ValueError, OSError)),
    ("snappy", lambda _t: tb.snappy_literals(b"hello world " * 40), None,
     lambda d: snappy.decompress(d, 1 << 20), (snappy.SnappyError,)),
    ("protobuf", lambda _t: tt.householdbinary(2), None, protobuf.fields_by_number,
     (protobuf.ProtobufError,)),
    ("refpack", lambda _t: bytes([0x10, 0xFB, 0, 0, 12, 0xE2]) + b"abcdefghijkl" + b"\xFC", None,
     dbpf.refpack_decompress, (dbpf.DbpfError,)),
]


@pytest.mark.parametrize("name,seed,file_name,reader,allowed", READERS,
                         ids=[r[0] for r in READERS])
def test_damaged_input_fails_only_as_expected(tmp_path, name, seed, file_name, reader, allowed):
    rng = random.Random(name)
    valid = seed(tmp_path)
    for _ in range(CASES):
        data = mutate(valid, rng)
        target = data
        if file_name is not None:
            target = tmp_path / file_name
            target.write_bytes(data)
        try:
            reader(target)
        except allowed:
            pass


def test_damaged_zips_are_reported_not_raised(tmp_path):
    from traytrim.received import check_received

    seed = tmp_path / "seed.zip"
    with zipfile.ZipFile(seed, "w") as z:
        z.writestr(f"Tray/0x00000001!0x{tt.HH_ID:016x}.trayitem", tt.trayitem())
        z.writestr(f"Tray/0x00000000!0x{tt.HH_ID:016x}.householdbinary", tt.householdbinary(2))
        z.writestr("Mods/x.package", package(tmp_path), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("report.json", b'{"name": "x", "files": []}',
                   compress_type=zipfile.ZIP_DEFLATED)
    rng, valid = random.Random("zip"), seed.read_bytes()
    with ResourceIndex(tmp_path / "index.sqlite") as index:
        for _ in range(60):
            damaged = tmp_path / "damaged.zip"
            damaged.write_bytes(mutate(valid, rng))
            check_received(damaged, index).close()


@pytest.fixture
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_thumbnails_decode_and_damaged_or_huge_ones_dont(qt_app):
    import zlib

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QColor, QImage

    from traytrim.gui.images import _decode

    encoded = {}
    for fmt in ("JPG", "PNG"):
        image = QImage(300, 200, QImage.Format.Format_RGB32)
        image.fill(QColor(200, 100, 50))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, fmt)
        encoded[fmt] = bytes(data)
        decoded = _decode(encoded[fmt], 64)
        assert decoded is not None and min(decoded.width(), decoded.height()) == 64
    # A header claiming 30,000 x 30,000 pixels is refused before anything is decoded.
    png = bytearray(encoded["PNG"])
    at = png.find(b"IHDR")
    png[at + 4:at + 12] = struct.pack(">II", 30000, 30000)
    png[at + 17:at + 21] = struct.pack(">I", zlib.crc32(bytes(png[at:at + 17])) & 0xFFFFFFFF)
    assert _decode(bytes(png), 64) is None
    rng = random.Random("images")
    for fmt in ("JPG", "PNG"):
        for _ in range(100):
            _decode(mutate(encoded[fmt], rng), 64)  # no image, or a valid one; never raises
    assert _decode(b"", 64) is None and _decode(b"not an image", 64) is None
