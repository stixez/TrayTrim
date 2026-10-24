"""A made-up Sims 4 setup (Tray, Mods, game, thumbnail cache) for screenshots and demos.

Every name, Sim and CC item here is invented; thumbnails and portraits are drawn. Built with
the same formats TrayTrim reads, so the app treats it like a real install.
"""

from __future__ import annotations

import random
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from traytrim import protobuf as pb
from traytrim import restypes
from traytrim.dbpf import COMP_NONE, PackageWriter, ResourceKey
from traytrim.gui import icons
from traytrim.gui.images import SIM_PORTRAIT

CC = 0x80000000


# Tray files are written with the encoder in traytrim.protobuf.
_v = pb.field_varint
_f64 = pb.field_fixed64
_b = pb.field_bytes
_s = pb.field_str
_packed_f64 = pb.field_packed_fixed64
_packed_v = pb.field_packed_varints


# Images

def _jpeg(image: QImage, quality: int = 90) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "JPEG", quality)
    return bytes(data)


def _product_shot(icon: str, colour: str, background: str, size: int = 256) -> bytes:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(background))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    # a soft floor shadow, then the item
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 22))
    p.drawEllipse(QRectF(size * 0.25, size * 0.80, size * 0.5, size * 0.07))
    glyph = icons.image(icon, colour, int(size * 0.72), 1.0)
    p.drawImage(QRectF(size * 0.14, size * 0.10, size * 0.72, size * 0.72), glyph)
    p.end()
    return _jpeg(image)


def _object_shot(shape: str, colour: str, background: str, size: int = 256) -> bytes:
    """A simple drawing of a piece of furniture, standing on a soft shadow."""
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(background))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(0, 0, 0, 22))
    p.drawEllipse(QRectF(size * 0.2, size * 0.80, size * 0.6, size * 0.07))
    main, dark = QColor(colour), QColor(colour).darker(135)
    u = size / 100

    def box(x, y, w, h, c, r=2.0):
        p.setBrush(c)
        p.drawRoundedRect(QRectF(x * u, y * u, w * u, h * u), r * u, r * u)

    if shape == "chair":
        box(30, 30, 40, 30, main, 6); box(28, 56, 44, 10, dark, 3)
        box(30, 64, 4, 20, dark); box(66, 64, 4, 20, dark)
    elif shape == "sofa":
        box(14, 40, 72, 26, main, 8); box(10, 52, 14, 22, dark, 5); box(76, 52, 14, 22, dark, 5)
        box(20, 62, 60, 12, dark, 4); box(18, 74, 4, 8, dark); box(78, 74, 4, 8, dark)
    elif shape == "table":
        box(14, 44, 72, 7, main, 2); box(20, 51, 5, 32, dark); box(75, 51, 5, 32, dark)
    elif shape == "lamp":
        p.setBrush(main)
        path = QPainterPath()
        path.moveTo(QPointF(36 * u, 22 * u)); path.lineTo(QPointF(64 * u, 22 * u))
        path.lineTo(QPointF(72 * u, 42 * u)); path.lineTo(QPointF(28 * u, 42 * u))
        path.closeSubpath()
        p.drawPath(path)
        box(48, 42, 4, 36, dark); box(38, 78, 24, 5, dark, 2)
    elif shape == "plant":
        p.setBrush(QColor("#6E8B5E"))
        for dx, dy, w, h in ((30, 18, 18, 34), (44, 12, 16, 40), (54, 20, 18, 32)):
            p.drawEllipse(QRectF(dx * u, dy * u, w * u, h * u))
        box(36, 52, 28, 30, main, 4)
    elif shape == "shelf":
        box(22, 16, 56, 68, main, 2)
        for y in (30, 48, 66):
            box(25, y, 50, 3, dark, 1)
        box(30, 21, 8, 9, dark, 1); box(56, 39, 12, 9, dark, 1); box(36, 57, 18, 9, dark, 1)
    elif shape == "bed":
        box(12, 46, 76, 22, main, 4); box(12, 34, 10, 40, dark, 3)
        box(24, 42, 20, 8, QColor("#F4F1EC"), 3); box(14, 66, 4, 14, dark); box(82, 66, 4, 14, dark)
    p.end()
    return _jpeg(image)


def _surface_shot(pattern: str, colour: str, size: int = 256) -> bytes:
    """A swatch of a wall or floor: planks, tiles or plaster."""
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(colour))
    p = QPainter(image)
    p.setPen(QColor(colour).darker(118))
    if pattern == "planks":
        for y in range(0, size, 32):
            p.drawLine(0, y, size, y)
            offset = 64 if (y // 32) % 2 else 0
            for x in range(offset, size, 128):
                p.drawLine(x, y, x, y + 32)
    elif pattern == "tiles":
        for k in range(0, size, 48):
            p.drawLine(k, 0, k, size)
            p.drawLine(0, k, size, k)
    else:
        rng = random.Random(len(colour))
        for _ in range(900):
            p.drawPoint(rng.randrange(size), rng.randrange(size))
    p.end()
    return _jpeg(image)


def _lot_picture(width: int = 300, height: int = 200) -> bytes:
    """The lot's own picture, as the game saves it: a JPEG scrambled with a repeating key."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.fillRect(0, 0, width, height, QColor("#BFD6E3"))
    p.fillRect(0, int(height * 0.68), width, height, QColor("#E7D8BC"))
    p.fillRect(0, int(height * 0.80), width, height, QColor("#7FA9B8"))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#F3EFE8"))
    p.drawRect(QRectF(90, 82, 120, 56))
    roof = QPainterPath()
    roof.moveTo(QPointF(80, 84)); roof.lineTo(QPointF(150, 46)); roof.lineTo(QPointF(220, 84))
    roof.closeSubpath()
    p.setBrush(QColor("#5E6B73"))
    p.drawPath(roof)
    p.setBrush(QColor("#8C6E52"))
    p.drawRect(QRectF(140, 104, 20, 34))
    p.setBrush(QColor("#9DB7C4"))
    p.drawRect(QRectF(104, 96, 24, 18)); p.drawRect(QRectF(172, 96, 24, 18))
    p.setBrush(QColor("#6E8B5E"))
    p.drawEllipse(QRectF(40, 92, 40, 50)); p.drawEllipse(QRectF(226, 98, 34, 42))
    p.end()
    jpeg = _jpeg(image)
    key = bytes.fromhex("4125e6cd47bab21a")
    body = bytes(b ^ key[i % 8] for i, b in enumerate(jpeg))
    return struct.pack("<II", len(body) + 16, 0) + bytes.fromhex(
        "8efc24489b780e3c0200000000000000") + body


def _snappy_literals(data: bytes) -> bytes:
    out = bytearray(pb.encode_varint(len(data)))
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


def _blueprint(lot_id: int, placed: list[int], build_keys: list[ResourceKey]) -> bytes:
    objects = _v(1, lot_id) + b"".join(
        _b(2, _v(1, n + 1) + _v(15, definition)) for n, definition in enumerate(placed))
    table1 = 44 + 16 * len(build_keys)
    bundle = (struct.pack("<11I", 0x424E444C, 3, 0, 0, 0, table1, table1 + 12, 40, 0, table1,
                          len(build_keys))
              + b"".join(struct.pack("<IIQ", k.type, k.group, k.instance) for k in build_keys)
              + bytes(12))
    architecture = struct.pack("<II", 1, 0) + struct.pack("<I", 0) + bundle
    body = struct.pack("<III", 0xFEA1BAAD, 2, len(objects)) + objects + architecture
    packed = _snappy_literals(body)
    return struct.pack("<II", 1, len(packed)) + packed


def _string_table(strings: dict[int, str]) -> bytes:
    entries = b"".join(struct.pack("<IBH", k, 0, len(v.encode())) + v.encode()
                       for k, v in strings.items())
    total = sum(len(v.encode()) + 1 for v in strings.values())
    return (b"STBL" + struct.pack("<HBQ", 5, 0, len(strings)) + b"\x00\x00"
            + struct.pack("<I", total) + entries)


def _catalog(name_hash: int) -> bytes:
    return struct.pack("<IIII", 25, 11, name_hash, 0) + bytes(40)


def _portrait(skin: str, hair: str, top: str, background: str, size: int = 256) -> bytes:
    image = QImage(size, size, QImage.Format.Format_RGB32)
    image.fill(QColor(background))
    p = QPainter(image)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(top))
    p.drawRoundedRect(QRectF(size * 0.18, size * 0.68, size * 0.64, size * 0.5), 60, 60)
    p.setBrush(QColor(skin))
    p.drawRect(QRectF(size * 0.44, size * 0.55, size * 0.12, size * 0.16))
    p.drawEllipse(QRectF(size * 0.33, size * 0.24, size * 0.34, size * 0.40))
    hair_path = QPainterPath()
    hair_path.moveTo(QPointF(size * 0.31, size * 0.48))
    hair_path.cubicTo(QPointF(size * 0.28, size * 0.14), QPointF(size * 0.72, size * 0.14),
                      QPointF(size * 0.69, size * 0.48))
    hair_path.cubicTo(QPointF(size * 0.64, size * 0.30), QPointF(size * 0.40, size * 0.30),
                      QPointF(size * 0.31, size * 0.48))
    p.setBrush(QColor(hair))
    p.drawPath(hair_path)
    p.end()
    return _jpeg(image)


# The world

@dataclass
class Item:
    name: str            # "Atelier Nord - Linen Shirt"
    folder: str          # folder in Mods ("" for the root)
    body_type: int
    icon: str
    colour: str
    mb: float            # roughly how big its textures and meshes are
    merged_into: str | None = None   # package name if it lives in a merged package
    instance: int = 0


@dataclass
class Sim:
    first: str
    last: str
    portrait: tuple[str, str, str, str]
    items: list[str]
    sim_id: int = 0
    ea_parts: int = 3
    sliders: list[str] = field(default_factory=list)
    traits: list[int] = field(default_factory=list)


BACKGROUNDS = ["#EEEAE5", "#E9ECEF", "#EDE7E3", "#E6EAE6", "#ECE8EE"]

ITEMS = [
    Item("Atelier Nord - Linen Shirt", "Clothing", 6, "cat-top", "#C9B8A3", 6.5),
    Item("Atelier Nord - Wide Trousers", "Clothing", 7, "cat-bottom", "#4A4F57", 5.2),
    Item("Paloma Studio - Slip Dress", "Clothing", 5, "cat-dress", "#B07A84", 7.8),
    Item("Paloma Studio - Knit Tank", "Clothing", 6, "cat-top", "#8E9C88", 4.1),
    Item("Fieldnotes - Canvas Sneakers", "Shoes", 8, "cat-shoes", "#F2F2F0", 3.3),
    Item("Fieldnotes - Leather Loafers", "Shoes", 8, "cat-shoes", "#5A3E2E", 3.9),
    Item("Lumen - Soft Bob", "Hair", 2, "cat-hair", "#3B2A22", 11.4),
    Item("Lumen - Long Waves", "Hair", 2, "cat-hair", "#8A5A3C", 14.0),
    Item("Lumen - Short Crop", "Hair", 2, "cat-hair", "#1E1A18", 9.6),
    Item("Ostra - Pearl Studs", "Accessories", 10, "cat-accessory", "#E8E1D3", 1.2,
         merged_into="Ostra_Jewelry_Collection_MERGED"),
    Item("Ostra - Thin Chain", "Accessories", 12, "cat-accessory", "#C7A75B", 1.0,
         merged_into="Ostra_Jewelry_Collection_MERGED"),
    Item("Grove - Everyday Lip Tint", "Makeup", 29, "cat-makeup", "#B5525A", 0.6,
         merged_into="Grove_Makeup_MERGED"),
    Item("Grove - Soft Liner", "Makeup", 31, "cat-makeup", "#2B2B2B", 0.5,
         merged_into="Grove_Makeup_MERGED"),
    Item("Grove - Natural Brows", "Makeup", 34, "cat-hair", "#4B3A30", 0.7,
         merged_into="Grove_Makeup_MERGED"),
    Item("Iris Lab - Grey Eyes", "Eyes", 35, "cat-eyes", "#7E8C96", 0.9),
    Item("Kinfolk - Freckles", "Skin", 39, "cat-skin-detail", "#B88A6A", 0.8),
    Item("Paloma Studio - Wool Coat", "Clothing", 5, "cat-dress", "#7A6A58", 9.1),
    Item("Atelier Nord - Denim Jacket", "Clothing", 6, "cat-top", "#5D7290", 6.0),
]
SLIDERS = ["Shape - Jaw Width", "Shape - Nose Bridge", "Shape - Shoulder Width"]

HOUSEHOLDS = [
    ("Moreau", [Sim("Camille", "Moreau", ("#E8C3A6", "#3B2A22", "#C9B8A3", "#EEEAE5"),
                    ["Atelier Nord - Linen Shirt", "Atelier Nord - Wide Trousers",
                     "Fieldnotes - Leather Loafers", "Lumen - Soft Bob", "Ostra - Pearl Studs",
                     "Ostra - Thin Chain", "Grove - Everyday Lip Tint", "Grove - Soft Liner",
                     "Grove - Natural Brows", "Iris Lab - Grey Eyes", "Kinfolk - Freckles",
                     "Paloma Studio - Wool Coat"],
                    sliders=SLIDERS[:2])]),
    ("Okafor", [Sim("Ada", "Okafor", ("#8D5A3B", "#1E1A18", "#B07A84", "#E9ECEF"),
                    ["Paloma Studio - Slip Dress", "Lumen - Long Waves",
                     "Fieldnotes - Canvas Sneakers", "Grove - Everyday Lip Tint"]),
                Sim("Tobi", "Okafor", ("#7A4B30", "#1E1A18", "#5D7290", "#E9ECEF"),
                    ["Atelier Nord - Denim Jacket", "Atelier Nord - Wide Trousers",
                     "Lumen - Short Crop", "Fieldnotes - Canvas Sneakers"])]),
    ("Hart", [Sim("Mae", "Hart", ("#F0CDB4", "#8A5A3C", "#8E9C88", "#E6EAE6"),
                  ["Paloma Studio - Knit Tank", "Atelier Nord - Wide Trousers",
                   "Lumen - Long Waves", "Kinfolk - Freckles", "Grove - Everyday Lip Tint",
                   "Ostra - Thin Chain", "Not Installed - Silk Scarf"],
                  sliders=SLIDERS, traits=[0x7A11C0DE00000001]),
              Sim("Jonah", "Hart", ("#E5BE9E", "#3B2A22", "#4A4F57", "#E6EAE6"),
                  ["Atelier Nord - Linen Shirt", "Fieldnotes - Leather Loafers",
                   "Lumen - Short Crop"]),
              Sim("Wren", "Hart", ("#F0CDB4", "#C08A5A", "#B07A84", "#E6EAE6"),
                  ["Paloma Studio - Slip Dress", "Lumen - Soft Bob"])]),
    ("Lindqvist", [Sim("Elsa", "Lindqvist", ("#F3D6C2", "#D9BE8C", "#E8E1D3", "#ECE8EE"),
                       ["Paloma Studio - Knit Tank", "Lumen - Long Waves",
                        "Ostra - Pearl Studs", "Iris Lab - Grey Eyes"])]),
    ("Sato", [Sim("Hana", "Sato", ("#EBC9AE", "#1E1A18", "#4A4F57", "#EDE7E3"),
                  ["Atelier Nord - Denim Jacket", "Lumen - Soft Bob",
                   "Fieldnotes - Canvas Sneakers", "Grove - Soft Liner"])]),
    ("Brennan", [Sim("Rosa", "Brennan", ("#E2B796", "#6B3E26", "#7A6A58", "#EEEAE5"),
                     ["Paloma Studio - Wool Coat", "Lumen - Long Waves"])]),
]


def _make_sparse(path: Path) -> None:
    if sys.platform == "win32":  # NTFS allocates truncated space unless the file is sparse
        subprocess.run(["fsutil", "sparse", "setflag", str(path)], check=False,
                       capture_output=True)


def build(root: Path) -> tuple[Path, Path]:
    """Create the world under root; returns (Sims 4 user folder, game folder)."""
    rng = random.Random(4)
    user = root / "Documents" / "Electronic Arts" / "The Sims 4"
    game = root / "The Sims 4"
    mods, tray = user / "Mods", user / "Tray"
    for d in (mods, tray, game / "Data" / "Client"):
        d.mkdir(parents=True, exist_ok=True)
    (user / "Options.ini").write_text("[options]\n")

    by_name = {i.name: i for i in ITEMS}
    for n, item in enumerate(ITEMS):
        item.instance = 0x5EED_0000_0000_1000 + n * 0x10

    def payload(mb: float) -> bytes:
        return rng.randbytes(int(mb * 1024 * 1024))

    def write_item(w: PackageWriter, item: Item) -> None:
        mesh = ResourceKey(restypes.GEOM, CC, item.instance + 1)
        diffuse = ResourceKey(restypes.RLE2, CC, item.instance + 2)
        spec = ResourceKey(restypes.RLES, CC, item.instance + 3)
        creator, piece = item.name.split(" - ", 1)
        internal = f"{creator.replace(' ', '')}_yfCas_{piece.replace(' ', '')}"
        name = internal.encode("utf-16-be")
        casp_body = struct.pack("<I", 0) + pb.encode_varint(len(name)) + name + b"\x00" * 40
        tgi = bytes([3]) + b"".join(struct.pack("<QII", k.instance, k.group, k.type)
                                    for k in (mesh, diffuse, spec))
        casp = struct.pack("<II", 46, len(casp_body)) + casp_body + tgi
        geom_tgi = struct.pack("<I", 0)
        mesh_payload = payload(item.mb * 0.35)
        chunk = (b"GEOM" + struct.pack("<III", 15, 4 + len(mesh_payload), len(geom_tgi))
                 + mesh_payload + geom_tgi)
        header = struct.pack("<5I", 3, 1, 0, 0, 1) + struct.pack("<QII", 1, restypes.GEOM, 0)
        geom = header + struct.pack("<II", len(header) + 8, len(chunk)) + chunk
        w.add(ResourceKey(restypes.CASP, CC, item.instance), casp)
        w.add_raw(mesh, geom, COMP_NONE, len(geom))
        w.add_raw(diffuse, payload(item.mb * 0.45), COMP_NONE, int(item.mb * 0.45 * 1048576))
        w.add_raw(spec, payload(item.mb * 0.2), COMP_NONE, int(item.mb * 0.2 * 1048576))
        bg = BACKGROUNDS[item.instance % len(BACKGROUNDS)]
        w.add(ResourceKey(restypes.THUM, 1, item.instance),
              _product_shot(item.icon, item.colour, bg), compress=False)

    merged: dict[str, list[Item]] = {}
    for item in ITEMS:
        if item.merged_into:
            merged.setdefault(item.merged_into, []).append(item)
            continue
        folder = mods / item.folder if item.folder else mods
        folder.mkdir(parents=True, exist_ok=True)
        file_name = item.name.replace(" - ", "_").replace(" ", "_")
        with PackageWriter(folder / f"{file_name}.package") as w:
            write_item(w, item)
    for name, items in merged.items():
        path = mods / f"{name}.package"
        with PackageWriter(path) as w:
            for item in items:
                write_item(w, item)
            # The rest of the merge: lots of other CC nobody here wears.
            for k in range(60):
                w.add_raw(ResourceKey(restypes.RLE2, CC, 0x0E770000_0000_0000 + k),
                          payload(0.2), COMP_NONE, int(0.2 * 1048576))
        # Merged packages are big; make the file as large as a real one. Marked sparse first
        # where the platform needs that, so the padding takes no disk space.
        _make_sparse(path)
        with open(path, "r+b") as f:
            f.seek(0, 2)
            f.truncate(f.tell() + rng.randrange(700, 1600) * 1024 * 1024)

    # Sliders: two packages hold the jaw slider with different content (a conflict).
    slider_ids = {}
    (mods / "Sliders").mkdir(exist_ok=True)
    with PackageWriter(mods / "Sliders" / "Shape_Sliders.package") as w:
        for n, name in enumerate(SLIDERS):
            inst = 0x5EED_5100_0000_0000 + n
            shape = ResourceKey(restypes.DEFORMER_MAP, 0, inst + 0x100)
            w.add(ResourceKey(restypes.SIM_MODIFIER, 0, inst),
                  b"smod" + struct.pack("<Q", shape.instance))
            w.add(shape, payload(0.4))
            slider_ids[name] = inst
    with PackageWriter(mods / "Sliders" / "Shape_Sliders_old_copy.package") as w:
        inst = slider_ids[SLIDERS[0]]
        w.add(ResourceKey(restypes.SIM_MODIFIER, 0, inst),
              b"smod-old" + struct.pack("<Q", inst + 0x100))

    # A gameplay mod (a trait) the Hart family uses.
    (mods / "Gameplay").mkdir(exist_ok=True)
    with PackageWriter(mods / "Gameplay" / "Kinfolk_Trait_Bookworm.package") as w:
        w.add(ResourceKey(0xCB5FDDC7, 0, 0x7A11C0DE00000001), b"<I n='trait_Bookworm'/>")

    # The game: a few EA parts every Sim wears (face, teeth, a base skin tone).
    ea_parts = [0x0000000000002000 + n for n in range(4)]
    skin_tone = 0x00000000000000A1
    with PackageWriter(game / "Data" / "Client" / "ClientFullBuild0.package") as w:
        for inst in ea_parts:
            w.add(ResourceKey(restypes.CASP, 0, inst), b"ea part")
        w.add(ResourceKey(restypes.SKIN_TONE, 0, skin_tone), b"ea tone")
    for pack in ("EP01", "GP02"):
        (game / pack).mkdir(exist_ok=True)
        with PackageWriter(game / pack / "ClientFullBuild0.package") as w:
            w.add(ResourceKey(restypes.CASP, 0, 0x0000000000003000 + len(pack)), b"pack part")

    # Tray files and the game's portrait cache.
    not_installed = 0x5EED_DEAD_0000_0001
    portraits: list[tuple[int, bytes]] = []
    for h, (household, sims) in enumerate(HOUSEHOLDS):
        hh_id = 0x0011_2233_0000_0100 + h * 0x10
        sim_meta, sim_data = [], []
        for s, sim in enumerate(sims):
            sim.sim_id = hh_id + 0x0100_0000_0000_0000 + s + 1
            parts, body_types = [], []
            for name in sim.items:
                item = by_name.get(name)
                parts.append(item.instance if item else not_installed)
                body_types.append(item.body_type if item else 12)
            parts += ea_parts[:sim.ea_parts]
            body_types += [3, 4, 37][:sim.ea_parts]
            outfit = (_v(1, 1) + _v(2, 0) + _b(5, _packed_f64(1, parts))
                      + _b(7, _packed_v(1, body_types)))
            facial = b"".join(_b(2, _v(1, slider_ids[n]) + pb.field_float(2, 0.4))
                              for n in sim.sliders)
            attributes = _b(10, _packed_v(1, sim.traits)) if sim.traits else b""
            sim_data.append(_b(6, _f64(1, sim.sim_id) + _s(5, sim.first) + _s(6, sim.last)
                                + _v(10, skin_tone) + _b(18, facial)
                                + _b(21, _b(1, outfit)) + _b(30, attributes)))
            sim_meta.append(_b(2, _s(3, sim.first) + _s(4, sim.last) + _v(5, sim.sim_id)))
            portraits.append((sim.sim_id, _portrait(*sim.portrait)))
        household_msg = _v(2, hh_id) + _s(3, household) + b"".join(sim_data)
        message = _b(1, household_msg)
        body = struct.pack("<II", 0, len(message)) + message + b"\x01\x00\x00\x00\x00"
        (tray / f"0x00000000!0x{hh_id:016x}.householdbinary").write_bytes(
            struct.pack("<II", 2, len(body)) + body)
        meta = (_v(1, hh_id) + _v(2, 1) + _s(4, household)
                + _b(10, _b(2, _v(1, len(sims)) + b"".join(sim_meta))))
        (tray / f"0x00000001!0x{hh_id:016x}.trayitem").write_bytes(
            struct.pack("<II", 0, len(meta)) + meta)
        for sim in sims:
            (tray / f"0x00000013!0x{sim.sim_id:016x}.sgi").write_bytes(b"portrait")

    _demo_lot(mods, tray, game, rng)

    with PackageWriter(user / "localthumbcache.package") as w:
        for sim_id, jpeg in portraits:
            w.add(ResourceKey(SIM_PORTRAIT, 0x13, sim_id), jpeg, compress=False)
    return user, game


# A lot: objects from a few creators, walls and floors, a mod that unlocks an EA sofa, one
# object whose two copies differ, and one that isn't installed.
LOT_OBJECTS = [
    # name, package, shape, colour, MB, times placed
    ("Driftwood Rattan Chair", "Driftwood_Living", "chair", "#B89A72", 1.4, 4),
    ("Driftwood Linen Sofa", "Driftwood_Living", "sofa", "#C9C2B5", 2.6, 1),
    ("Driftwood Oak Dining Table", "Driftwood_Living", "table", "#8C6E52", 1.9, 1),
    ("Saltwater Paper Lamp", "Saltwater_Decor", "lamp", "#E8DCC6", 0.6, 3),
    ("Saltwater Olive Tree", "Saltwater_Decor", "plant", "#B5A48C", 1.1, 2),
    ("Saltwater Open Shelf", "Saltwater_Decor", "shelf", "#A8896B", 1.3, 1),
    ("Harbour Lane Linen Bed", "Harbour_Lane_Bedroom", "bed", "#9DB0BE", 3.2, 2),
]
LOT_BUILD = [
    ("Whitewashed Oak Planks", restypes.FLOOR, "planks", "#D9CDBB", 1.6),
    ("Terrazzo Tiles", restypes.FLOOR, "tiles", "#E4E0D8", 1.2),
    ("Lime Plaster Wall", restypes.WALL, "plaster", "#EDE6DA", 0.9),
]


def _demo_lot(mods: Path, tray: Path, game: Path, rng: random.Random) -> None:
    lot_id = 0x0011_2233_0000_0F00
    folder = mods / "Build Buy"
    folder.mkdir(parents=True, exist_ok=True)
    placed: list[int] = []
    packages: dict[str, list] = {}
    for n, (name, package, shape, colour, mb, times) in enumerate(LOT_OBJECTS):
        packages.setdefault(package, []).append((n, name, shape, colour, mb))
        placed += [0x5EED_0B00_0000_0000 + n * 0x10] * times
    for package, objects in packages.items():
        with PackageWriter(folder / f"{package}.package") as w:
            strings = {}
            for n, name, shape, colour, mb in objects:
                inst = 0x5EED_0B00_0000_0000 + n * 0x10
                model = ResourceKey(restypes.MODEL, 0, inst + 1)
                texture = ResourceKey(restypes.RLE2, CC, inst + 2)
                name_hash = 0x10000 + n
                strings[name_hash] = name
                w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, inst),
                      b"objd" + struct.pack("<QQ", model.instance, texture.instance))
                w.add(ResourceKey(restypes.OBJECT_CATALOG, 0, inst), _catalog(name_hash))
                w.add(ResourceKey(restypes.OBJECT_THUMB, 1, inst),
                      _object_shot(shape, colour, BACKGROUNDS[n % len(BACKGROUNDS)]),
                      compress=False)
                w.add_raw(model, rng.randbytes(int(mb * 0.4 * 1048576)), COMP_NONE,
                          int(mb * 0.4 * 1048576))
                w.add_raw(texture, rng.randbytes(int(mb * 0.6 * 1048576)), COMP_NONE,
                          int(mb * 0.6 * 1048576))
            w.add(ResourceKey(restypes.STRING_TABLE, 0, 0x0000_5EED_0000_0001 + len(strings)),
                  _string_table(strings))
        # Sets come as big merged packages; make the files as large as real ones (sparse).
        path = folder / f"{package}.package"
        _make_sparse(path)
        with open(path, "r+b") as f:
            f.seek(0, 2)
            f.truncate(f.tell() + rng.randrange(400, 900) * 1024 * 1024)
    # A newer copy of the lamp from an update, with different content: a conflict to choose.
    lamp = 0x5EED_0B00_0000_0000 + 3 * 0x10
    with PackageWriter(folder / "Saltwater_Lamp_Update.package") as w:
        w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, lamp), b"objd updated version")
        w.add(ResourceKey(restypes.OBJECT_CATALOG, 0, lamp), _catalog(0x10003))
        w.add(ResourceKey(restypes.STRING_TABLE, 0, 0x0000_5EED_0000_0200),
              _string_table({0x10003: "Saltwater Paper Lamp"}))

    build_keys = []
    with PackageWriter(folder / "Coastline_Walls_and_Floors.package") as w:
        strings = {}
        for n, (name, type_id, pattern, colour, mb) in enumerate(LOT_BUILD):
            key = ResourceKey(type_id, 0, 0x5EED_0C00_0000_0000 + n * 0x10)
            build_keys.append(key)
            strings[0x20000 + n] = name
            w.add(key, _catalog(0x20000 + n) + rng.randbytes(int(mb * 1048576)))
            w.add(ResourceKey(restypes.OBJECT_THUMB, 1, key.instance),
                  _surface_shot(pattern, colour), compress=False)
        w.add(ResourceKey(restypes.STRING_TABLE, 0, 0x0000_5EED_0000_0100),
              _string_table(strings))

    # An EA sofa that an unlock mod replaces: left out of lot exports unless included.
    ea_sofa = 0x0000_0000_0001_5000
    with PackageWriter(mods / "Gameplay" / "Unlock_All_Sofas.package") as w:
        w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, ea_sofa), b"objd unlocked")
    with PackageWriter(game / "Data" / "Client" / "ClientFullBuild1.package") as w:
        w.add(ResourceKey(restypes.OBJECT_DEFINITION, 0, ea_sofa), b"objd ea")
    placed += [ea_sofa, 0x5EED_DEAD_0B00_0001]  # the second one isn't installed

    (tray / f"0x00000000!0x{lot_id:016x}.blueprint").write_bytes(
        _blueprint(lot_id, placed, build_keys))
    (tray / f"0x00000002!0x{lot_id:016x}.bpi").write_bytes(_lot_picture())
    lot_meta = _v(2, 30) + _v(3, 20) + _v(5, 86_400) + _v(6, 2) + _v(7, 1)
    meta = (_v(1, lot_id) + _v(2, 2) + _s(4, "Seaside Cottage")
            + _b(10, _b(1, lot_meta)))
    (tray / f"0x00000002!0x{lot_id:016x}.trayitem").write_bytes(
        struct.pack("<II", 0, len(meta)) + meta)
