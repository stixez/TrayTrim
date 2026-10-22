"""Tray items: discovery in the Tray folder and what a household's Sims reference.

Format sources
--------------
The messages below are the game's own protobuf schemas, read from the compiled protocol buffer
modules that ship with The Sims 4 (Game/Bin/Python/generated.zip: Exchange_pb2, FileSerialization_
pb2, Outfits_pb2, PersistenceBlobs_pb2). Field numbers used here:

  TrayMetadata (.trayitem)          1 id, 2 type, 4 name, 5 description, 7 creator_name,
                                    10 metadata -> SpecificData 2 hh_metadata -> 2 sim_data
  TraySimMetadata                   3 first_name, 4 last_name, 5 id, 6 gender, 9 age, 12 species
  SimData                           1 sim_id (fixed64), 5 first_name, 6 last_name,
                                    10 skin_tone, 18 facial_attr, 21 outfits, 28 genetic_data,
                                    63 pelt_layers, 75 parts_custom_tattoos
  OutfitList / OutfitData           1 outfits / 2 category, 5 parts (IdList: 1 fixed64 ids),
                                    7 body_types_list (1 body_types, same order as parts)
  BlobSimFacialCustomizationData    1 sculpts, 2 face_modifiers, 3 body_modifiers,
                                    4 aged_face_modifiers, 5 aged_body_modifiers (Modifier: 1 key)
  GeneticData                       1 sculpts_and_mods_attr (same blob), 5 parts_list,
                                    6 growth_parts_list (PartData: 1 id, 2 body_type)
  PeltLayerData                     1 layer_id;  SimPartCustomTattooData: 1 body_type, 2 texture_id

Not in those schemas, observed in real files (version 1 and 2 headers, the household wrapper):
  .trayitem         uint32 0, uint32 length, TrayMetadata
  .householdbinary  version 1: uint32 1, uint32 length, message
                    version 2: uint32 2, uint32 remaining size, uint32 0, uint32 length, message
                    The message's field 1 is a household record whose field 3 is the household
                    name and whose repeated field 6 is SimData (checked against sim ids, names and
                    household ids in .trayitem/.sgi files). Field 21 lists reward parts the
                    household has unlocked; those aren't worn, so they're not CC references.
  .sgi              one per Sim, named after the Sim id.

Enums (OutfitCategory, BodyType) come from the game's sims/outfits/outfit_enums module.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from traytrim import protobuf as pb
from traytrim.protobuf import ProtobufError

ITEM_HOUSEHOLD, ITEM_LOT, ITEM_ROOM, ITEM_PART = 1, 2, 3, 5
ITEM_KINDS = {ITEM_HOUSEHOLD: "household", ITEM_LOT: "lot", ITEM_ROOM: "room", ITEM_PART: "part"}

OUTFIT_CATEGORIES = {
    0: "Everyday", 1: "Formal", 2: "Athletic", 3: "Sleep", 4: "Party", 5: "Bathing",
    6: "Career", 7: "Situation", 8: "Special", 9: "Swimwear", 10: "Hot weather",
    11: "Cold weather", 12: "Batuu", 13: "Small business",
}

# BodyType values from the game, with labels for the UI.
BODY_TYPES = {
    0: "None", 1: "Hat", 2: "Hair", 3: "Head", 4: "Teeth", 5: "Full body", 6: "Top",
    7: "Bottom", 8: "Shoes", 9: "Cummerbund", 10: "Earrings", 11: "Glasses", 12: "Necklace",
    13: "Gloves", 14: "Left bracelet", 15: "Right bracelet", 16: "Left lip ring",
    17: "Right lip ring", 18: "Left nose ring", 19: "Right nose ring", 20: "Left brow ring",
    21: "Right brow ring", 22: "Left index finger ring", 23: "Right index finger ring",
    24: "Left ring finger ring", 25: "Right ring finger ring", 26: "Left middle finger ring",
    27: "Right middle finger ring", 28: "Facial hair", 29: "Lipstick", 30: "Eyeshadow",
    31: "Eyeliner", 32: "Blush", 33: "Face paint", 34: "Eyebrows", 35: "Eye color",
    36: "Socks", 37: "Eyelashes", 38: "Forehead crease", 39: "Freckles", 40: "Left dimple",
    41: "Right dimple", 42: "Tights", 43: "Left lip mole", 44: "Right lip mole",
    45: "Lower left arm tattoo", 46: "Upper left arm tattoo", 47: "Lower right arm tattoo",
    48: "Upper right arm tattoo", 49: "Left leg tattoo", 50: "Right leg tattoo",
    51: "Lower back tattoo", 52: "Upper back tattoo", 53: "Lower front torso tattoo",
    54: "Upper front torso tattoo", 55: "Left cheek mole", 56: "Right cheek mole",
    57: "Mouth crease", 58: "Skin overlay", 59: "Fur", 60: "Ears", 61: "Tail",
    62: "Nose color", 63: "Secondary eye color", 64: "Occult brow", 65: "Occult eye socket",
    66: "Occult eyelid", 67: "Occult mouth", 68: "Occult left cheek", 69: "Occult right cheek",
    70: "Occult neck scar", 71: "Forearm scar", 72: "Acne", 73: "Fingernails", 74: "Toenails",
    75: "Hair color override", 76: "Bite", 77: "Body freckles", 78: "Arm hair", 79: "Leg hair",
    80: "Chest hair", 81: "Back hair", 82: "Left arm scar", 83: "Right arm scar",
    84: "Front torso scar", 85: "Back scar", 86: "Left leg scar", 87: "Right leg scar",
    88: "Back attachment", 89: "Puberty acne", 90: "Face scar", 91: "Face birthmark",
    92: "Back birthmark", 93: "Front torso birthmark", 94: "Arm birthmark", 95: "Face mole",
    96: "Upper chest mole", 97: "Upper back mole", 98: "Leg birthmark",
    99: "Front stretch marks", 100: "Back stretch marks", 101: "Saddle", 102: "Bridle",
    103: "Reins", 104: "Blanket", 105: "Hoof color", 106: "Mane", 107: "Tail hair",
    108: "Forelock", 109: "Feathers", 110: "Horn", 111: "Tail base", 112: "Occult birthmark",
    113: "Head tattoo", 114: "Wings", 115: "Head decoration", 116: "Skin specularity",
    117: "Base layer",
}

# Body types grouped the way CAS groups them, for the UI.
_GROUPS = {
    "Hair": {2, 28, 34, 75, 106, 107, 108, 109},
    "Clothing": {5, 6, 7, 8, 9, 13, 36, 42, 101, 102, 103, 104, 117},
    "Accessories": {1, 10, 11, 12, *range(14, 28), 73, 74, 88, 110, 114, 115},
    "Makeup": {29, 30, 31, 32, 33, 37},
    "Eyes": {35, 63},
    "Skin details": {*range(38, 42), *range(43, 59), *range(62, 73), 76, *range(77, 88),
                     *range(89, 101), 105, 111, 112, 113, 116},
    "Body": {3, 4, 59, 60, 61},
}
BODY_TYPE_GROUP = {bt: group for group, members in _GROUPS.items() for bt in members}


class RefKind(str, Enum):
    CAS_PART = "cas_part"            # CASP
    SCULPT = "sculpt"                # Sculpt
    SIM_MODIFIER = "sim_modifier"    # SMOD: sliders
    SKIN_TONE = "skin_tone"          # TONE
    PELT_LAYER = "pelt_layer"        # pet coat layer; resource type not verified
    CUSTOM_TATTOO = "custom_tattoo"  # texture of a custom tattoo; resource type not verified
    OBJECT = "object"                # placed object: the instance is its object definition
    BUILD = "build"                  # exact resource key: walls, floors, roofs, model overrides


@dataclass(frozen=True)
class TrayRef:
    kind: RefKind
    instance: int
    body_type: int | None = None
    type_id: int | None = None   # BUILD only: the exact resource type and group
    res_group: int | None = None

    @property
    def group(self) -> str:
        if self.kind is RefKind.CAS_PART:
            return BODY_TYPE_GROUP.get(self.body_type, "Other CAS parts")
        return {RefKind.SCULPT: "Sculpts", RefKind.SIM_MODIFIER: "Sliders",
                RefKind.SKIN_TONE: "Skin tone", RefKind.PELT_LAYER: "Pet coat",
                RefKind.CUSTOM_TATTOO: "Custom tattoos", RefKind.OBJECT: "Objects",
                RefKind.BUILD: "Build"}[self.kind]


@dataclass
class TraySim:
    sim_id: int
    first_name: str
    last_name: str
    age: int | None = None
    gender: int | None = None
    species: int | None = None

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class SimContents:
    sim: TraySim
    # every reference, with where it's used ("Everyday outfit 1", "Genetics", "Face sliders"...)
    refs: dict[TrayRef, set[str]] = field(default_factory=dict)
    # 64-bit ids found in the Sim's gameplay data (traits, aspiration, moods, careers, skills...).
    # Gathered by walking the data, not by parsing every tracker; used to tell which gameplay
    # mods the Sim depends on.
    gameplay_ids: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)  # things that couldn't be read

    def add(self, ref: TrayRef, where: str) -> None:
        self.refs.setdefault(ref, set()).add(where)


@dataclass
class HouseholdContents:
    """What a tray item references. For a lot or room there is one entry in `sims` standing for
    the build itself, so the rest of TrayTrim treats every kind the same way."""
    name: str
    sims: list[SimContents]
    version: int
    kind: str = "household"


@dataclass
class TrayItem:
    instance: int
    type: int
    name: str
    description: str = ""
    creator: str = ""
    sims: list[TraySim] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)
    modified: float = 0.0
    # Lots and rooms (TrayBlueprintMetadata / TrayRoomBlueprintMetadata in Exchange.proto)
    size: tuple[int, int] | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    price: int | None = None     # simoleons
    lot_traits: list[int] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return ITEM_KINDS.get(self.type, "unknown")

    @property
    def id_text(self) -> str:
        return f"0x{self.instance:016x}"

    def file_with_suffix(self, suffix: str) -> Path | None:
        return next((f for f in self.files if f.suffix.lower() == suffix), None)


class TrayError(ValueError):
    """A tray file that can't be read. The message is meant to be shown to the user."""


_NAME = re.compile(r"^0x([0-9a-fA-F]{1,8})!0x([0-9a-fA-F]{1,16})\.(\w+)$")


def parse_tray_filename(name: str) -> tuple[int, int, str] | None:
    """(type, instance, extension) from "0x00000001!0x0058175cd0eb001b.trayitem"."""
    m = _NAME.match(name)
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16), m.group(3).lower()


# .trayitem

def read_trayitem(path: Path) -> TrayItem:
    data = _read(path)
    if len(data) < 8:
        raise TrayError(f"{path.name} is too short to be a tray item.")
    _zero, size = struct.unpack_from("<II", data, 0)
    if 8 + size > len(data):
        raise TrayError(f"{path.name} is truncated.")
    try:
        meta = pb.fields_by_number(data[8:8 + size])
        sims: list[TraySim] = []
        specific = pb.first_bytes(meta, 10)
        if specific:
            hh = pb.first_bytes(pb.fields_by_number(specific), 2)
            if hh:
                for raw in pb.messages(pb.fields_by_number(hh), 2):
                    s = pb.fields_by_number(raw)
                    sims.append(TraySim(
                        sim_id=pb.first_int(s, 5, 0), first_name=pb.first_str(s, 3),
                        last_name=pb.first_str(s, 4), age=pb.first_int(s, 9),
                        gender=pb.first_int(s, 6), species=pb.first_int(s, 12)))
        item = TrayItem(
            instance=pb.first_int(meta, 1, 0), type=pb.first_int(meta, 2, 0),
            name=pb.first_str(meta, 4), description=pb.first_str(meta, 5),
            creator=pb.first_str(meta, 7), sims=sims)
        if specific:
            sf = pb.fields_by_number(specific)
            lot, room = pb.first_bytes(sf, 1), pb.first_bytes(sf, 7)
            if lot is not None:
                lf = pb.fields_by_number(lot)
                item.size = (pb.first_int(lf, 2, 0), pb.first_int(lf, 3, 0))
                item.bedrooms, item.bathrooms = pb.first_int(lf, 6), pb.first_int(lf, 7)
                item.price = pb.first_int(lf, 5)
                item.lot_traits = pb.ints(lf.get(14, []), fixed64=True)
            elif room is not None:
                rf = pb.fields_by_number(room)
                item.size = (pb.first_int(rf, 2, 0), pb.first_int(rf, 3, 0))
                item.price = pb.first_int(rf, 4)
        return item
    except ProtobufError as e:
        raise TrayError(f"{path.name} is damaged ({e}).") from None


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise TrayError(f"Couldn't read {path.name}: {e.strerror or e}.") from None


# Tray folder

def scan_tray(tray_dir: Path) -> tuple[list[TrayItem], list[tuple[Path, str]]]:
    """All tray items with their files, newest first, plus files that couldn't be read."""
    by_instance: dict[int, list[Path]] = {}
    sgi_by_sim: dict[int, list[Path]] = {}
    with os.scandir(tray_dir) as it:
        for entry in it:
            if not entry.is_file():
                continue
            parsed = parse_tray_filename(entry.name)
            if parsed is None:
                continue
            _type, instance, ext = parsed
            target = sgi_by_sim if ext == "sgi" else by_instance
            target.setdefault(instance, []).append(Path(entry.path))

    items: list[TrayItem] = []
    failed: list[tuple[Path, str]] = []
    for instance, files in by_instance.items():
        trayitem = next((f for f in files if f.suffix.lower() == ".trayitem"), None)
        if trayitem is None:
            continue
        try:
            item = read_trayitem(trayitem)
        except TrayError as e:
            failed.append((trayitem, str(e)))
            continue
        item.instance = instance  # the file name is what the game uses to find the files
        item.files = sorted(files)
        for sim in item.sims:
            item.files += sorted(sgi_by_sim.get(sim.sim_id, []))
        try:
            item.modified = max(f.stat().st_mtime for f in item.files)
        except OSError:
            pass
        items.append(item)
    items.sort(key=lambda i: i.modified, reverse=True)
    return items, failed


# .householdbinary

def _household_message(data: bytes, name: str) -> tuple[int, bytes]:
    if len(data) < 8:
        raise TrayError(f"{name} is too short to be a household file.")
    version = struct.unpack_from("<I", data, 0)[0]
    if version == 1:
        start, size = 8, struct.unpack_from("<I", data, 4)[0]
    elif version == 2:
        if len(data) < 16:
            raise TrayError(f"{name} is too short to be a household file.")
        start, size = 16, struct.unpack_from("<I", data, 12)[0]
    else:
        raise TrayError(f"{name} uses household file version {version}, which TrayTrim "
                        "doesn't know yet.")
    if start + size > len(data):
        raise TrayError(f"{name} is truncated.")
    return version, data[start:start + size]


def read_household(path: Path) -> HouseholdContents:
    data = _read(path)
    version, message = _household_message(data, path.name)
    try:
        top = pb.fields_by_number(message)
        household = pb.first_bytes(top, 1)
        if household is None:
            raise TrayError(f"{path.name} has no household record.")
        hh = pb.fields_by_number(household)
        sims = [_read_sim(raw) for raw in pb.messages(hh, 6)]
        return HouseholdContents(pb.first_str(hh, 3), sims, version)
    except ProtobufError as e:
        raise TrayError(f"{path.name} is damaged ({e}).") from None


def _read_sim(raw: bytes) -> SimContents:
    s = pb.fields_by_number(raw)
    sim = SimContents(TraySim(
        sim_id=pb.first_int(s, 1, 0), first_name=pb.first_str(s, 5),
        last_name=pb.first_str(s, 6), age=pb.first_int(s, 8), gender=pb.first_int(s, 7)))

    # SimData 30 attributes (traits, commodities, careers...), 47 gameplay_data,
    # 34 primary_aspiration.
    for number in (30, 47):
        blob = pb.first_bytes(s, number)
        if blob:
            _collect_ids(blob, sim.gameplay_ids)
    aspiration = pb.first_int(s, 34)
    if aspiration and aspiration >= _MIN_ID:
        sim.gameplay_ids.add(aspiration)

    skin = pb.first_int(s, 10)
    if skin:
        sim.add(TrayRef(RefKind.SKIN_TONE, skin), "Skin tone")

    outfits = pb.first_bytes(s, 21)
    if outfits:
        seen: dict[int, int] = {}
        for raw_outfit in pb.messages(pb.fields_by_number(outfits), 1):
            o = pb.fields_by_number(raw_outfit)
            category = pb.first_int(o, 2, 0)
            seen[category] = seen.get(category, 0) + 1
            where = f"{OUTFIT_CATEGORIES.get(category, f'Category {category}')} outfit {seen[category]}"
            parts: list[int] = []
            for id_list in pb.messages(o, 5):
                parts += pb.ints(pb.fields_by_number(id_list).get(1, []), fixed64=True)
            body_types: list[int] = []
            for bt_list in pb.messages(o, 7):
                body_types += pb.ints(pb.fields_by_number(bt_list).get(1, []))
            for i, part in enumerate(parts):
                if part:
                    bt = body_types[i] if i < len(body_types) else None
                    sim.add(TrayRef(RefKind.CAS_PART, part, bt), where)

    facial = pb.first_bytes(s, 18)
    if facial:
        _read_face_blob(sim, facial, "")

    genetic = pb.first_bytes(s, 28)
    if genetic:
        g = pb.fields_by_number(genetic)
        blob = pb.first_bytes(g, 1)
        if blob:
            _read_face_blob(sim, blob, "Genetics: ")
        for number in (5, 6):
            for part_list in pb.messages(g, number):
                for raw_part in pb.messages(pb.fields_by_number(part_list), 1):
                    p = pb.fields_by_number(raw_part)
                    part = pb.first_int(p, 1)
                    if part:
                        sim.add(TrayRef(RefKind.CAS_PART, part, pb.first_int(p, 2)), "Genetics")

    pelts = pb.first_bytes(s, 63)
    if pelts:
        for raw_layer in pb.messages(pb.fields_by_number(pelts), 1):
            layer = pb.first_int(pb.fields_by_number(raw_layer), 1)
            if layer:
                sim.add(TrayRef(RefKind.PELT_LAYER, layer), "Coat")

    for raw_tattoo in pb.messages(s, 75):
        t = pb.fields_by_number(raw_tattoo)
        texture = pb.first_int(t, 2)
        if texture:
            sim.add(TrayRef(RefKind.CUSTOM_TATTOO, texture, pb.first_int(t, 1)), "Custom tattoo")
    return sim


_MODIFIER_FIELDS = {2: "Face sliders", 3: "Body sliders", 4: "Aged face sliders",
                    5: "Aged body sliders"}


def _read_face_blob(sim: SimContents, blob: bytes, prefix: str) -> None:
    f = pb.fields_by_number(blob)
    for sculpt in pb.ints(f.get(1, [])):
        if sculpt:
            sim.add(TrayRef(RefKind.SCULPT, sculpt), f"{prefix}Sculpts")
    for number, where in _MODIFIER_FIELDS.items():
        for raw_mod in pb.messages(f, number):
            key = pb.first_int(pb.fields_by_number(raw_mod), 1)
            if key:
                sim.add(TrayRef(RefKind.SIM_MODIFIER, key), prefix + where)


_MIN_ID = 1 << 32  # tuning ids are 64-bit hashes; small numbers are counts, flags, values


def _collect_ids(blob: bytes, out: set[int], depth: int = 0) -> bool:
    """Every large varint/fixed64 value in a protobuf blob, nested messages included. Returns
    False if the blob isn't a protobuf message (then it's treated as packed 64-bit values)."""
    try:
        fields = list(pb.iter_fields(blob))
    except ProtobufError:
        return False
    for _number, wire, value in fields:
        if wire in (pb.VARINT, pb.FIXED64):
            if value >= _MIN_ID:
                out.add(value)
        elif wire == pb.LEN and depth < 40:
            assert isinstance(value, bytes)
            # A LEN field can be a nested message, packed varints (lists of trait ids...) or
            # packed fixed64s; the bytes don't say which, so every reading is collected. Wrong
            # readings give random 64-bit numbers that match nothing in the index.
            _collect_ids(value, out, depth + 1)
            try:
                out.update(v for v in pb.ints([(pb.LEN, value)]) if v >= _MIN_ID)
            except ProtobufError:
                pass
            if len(value) % 8 == 0:
                out.update(v for v in struct.unpack(f"<{len(value) // 8}Q", value)
                           if v >= _MIN_ID)
    return True


# Lots (.blueprint) and rooms (.room)
#
# Sources: the snappy container and the lot and room layouts below were described by Harbor
# (github.com/Harborofficial/Harbor, MIT; src-tauri/src/games/sims_cc_build.rs and
# sims_cc_architecture.rs) and checked on every lot and room in a real Tray folder. Room
# version 15 adds a 4-byte value after the "1" marker; Harbor documents version 13 only.
#
#   file   uint32 1, uint32 length, snappy-compressed body
#   lot    uint32 0xFEA1BAAD, uint32 2, uint32 length, objects message, architecture
#   room   uint32 13|15, uint32 length, keys message (1: repeated key), uint32 1,
#          [uint32, version 15 only], uint32 length, objects message
#   objects message   2: repeated object (15 or 4: object definition id, 17: model key)
#   key message       1: type, 2: group, 3: instance
#   architecture      uint32 1, uint32 n, n x 16 bytes, uint32 0, then a BNDL v3 bundle whose
#                     key table (at offset 40: uint32 count, count x type/group/instance)
#                     lists the walls, floors, roofs, fences... the lot uses. Some lots carry
#                     more data before the bundle; then the bundle is found by its header and
#                     accepted only if every table offset in it is consistent.

LOT_MAGIC = 0xFEA1BAAD
OBJECT_CATALOG = 0x319E4F1D
_BNDL = 0x424E444C


def _build_body(path: Path) -> bytes:
    from traytrim import snappy

    data = _read(path)
    if len(data) < 8:
        raise TrayError(f"{path.name} is too short.")
    version, size = struct.unpack_from("<II", data, 0)
    if version != 1 or 8 + size > len(data):
        raise TrayError(f"{path.name} has an unknown layout (version {version}).")
    try:
        return snappy.decompress(data[8:8 + size], 64 << 20)
    except snappy.SnappyError as e:
        raise TrayError(f"{path.name} is damaged ({e}).") from None


def _key_message(raw: bytes):
    from traytrim.dbpf import ResourceKey

    f = pb.fields_by_number(raw)
    t, g, i = pb.first_int(f, 1, 0), pb.first_int(f, 2, 0), pb.first_int(f, 3, 0)
    if not (t and i) or t > 0xFFFFFFFF or g > 0xFFFFFFFF:
        return None
    return ResourceKey(t, g, i)


def _objects(raw: bytes, build: SimContents) -> None:
    placed: dict[int, int] = {}
    for obj in pb.messages(pb.fields_by_number(raw), 2):
        o = pb.fields_by_number(obj)
        definition = pb.first_int(o, 15) or pb.first_int(o, 4)
        if definition:
            placed[definition] = placed.get(definition, 0) + 1
        model = pb.first_bytes(o, 17)
        key = _key_message(model) if model else None
        if key is not None:
            build.add(TrayRef(RefKind.BUILD, key.instance, type_id=key.type,
                              res_group=key.group), "Model override")
    for definition, n in placed.items():
        build.add(TrayRef(RefKind.OBJECT, definition),
                  "Placed once" if n == 1 else f"Placed {n} times")


def _bundle_at(data: bytes, start: int):
    """Keys from a BNDL v3 bundle at `start`, or None if the header doesn't check out."""
    from traytrim.dbpf import ResourceKey

    if start < 0 or start + 44 > len(data):
        return None
    (magic, version, z1, z2, records, table1, table3, keys_at, repeated,
     table2, count) = struct.unpack_from("<11I", data, start)
    bundle = len(data) - start
    if (magic != _BNDL or version != 3 or z1 or z2 or records != repeated or keys_at != 40
            or table1 < 44 or table1 + records * 8 != table2
            or table2 + (records + 1) * 12 != table3 or table3 > bundle
            or 44 + count * 16 > table1):
        return None
    keys = []
    for i in range(count):
        t, g, inst = struct.unpack_from("<IIQ", data, start + 44 + 16 * i)
        if t and inst:
            keys.append(ResourceKey(t, g, inst))
    return keys


def _architecture(rest: bytes):
    """(keys, found): the documented position first, then a checked search for the header."""
    if len(rest) >= 8:
        lead, pairs = struct.unpack_from("<II", rest, 0)
        pos = 8 + 16 * pairs
        if lead == 1 and pos + 4 <= len(rest) and struct.unpack_from("<I", rest, pos)[0] == 0:
            keys = _bundle_at(rest, pos + 4)
            if keys is not None:
                return keys, True
    start = rest.find(b"LDNB")
    while start != -1:
        keys = _bundle_at(rest, start)
        if keys is not None:
            return keys, True
        start = rest.find(b"LDNB", start + 1)
    return [], False


def read_build(item: TrayItem, path: Path) -> HouseholdContents:
    """The objects and build items a lot (.blueprint) or room (.room) uses."""
    body = _build_body(path)
    build = SimContents(TraySim(item.instance, item.name or "Unnamed", ""))
    kind = "lot" if path.suffix.lower() == ".blueprint" else "room"
    version = 2
    try:
        if kind == "lot":
            if len(body) < 12:
                raise TrayError(f"{path.name} is truncated.")
            magic, version, size = struct.unpack_from("<III", body, 0)
            if magic != LOT_MAGIC or version != 2:
                raise TrayError(f"{path.name} uses a lot layout TrayTrim doesn't know "
                                f"({magic:08X} version {version}).")
            if 12 + size > len(body):
                raise TrayError(f"{path.name} is truncated.")
            _objects(body[12:12 + size], build)
            keys, found = _architecture(body[12 + size:])
            if not found:
                build.notes.append("Walls, floors and roofs couldn't be read from this lot, "
                                   "so only its objects are included.")
        else:
            version, size = struct.unpack_from("<II", body, 0)
            if version not in (13, 15) or 8 + size > len(body):
                raise TrayError(f"{path.name} uses a room layout TrayTrim doesn't know "
                                f"(version {version}).")
            keys = [k for raw in pb.messages(pb.fields_by_number(body[8:8 + size]), 1)
                    if (k := _key_message(raw)) is not None]
            p = 8 + size
            marker = struct.unpack_from("<I", body, p)[0]
            p += 4 + (4 if version >= 15 else 0)
            objects_size = struct.unpack_from("<I", body, p)[0]
            if marker != 1 or p + 4 + objects_size > len(body):
                raise TrayError(f"{path.name} has an unexpected room layout.")
            _objects(body[p + 4:p + 4 + objects_size], build)
    except (struct.error, ProtobufError) as e:
        raise TrayError(f"{path.name} is damaged ({e}).") from None
    for key in keys:
        if key.type == OBJECT_CATALOG:
            continue  # rooms list their objects' catalog keys; the objects cover those
        build.add(TrayRef(RefKind.BUILD, key.instance, type_id=key.type, res_group=key.group),
                  "Build")
    build.gameplay_ids.update(item.lot_traits)
    return HouseholdContents(item.name, [build], version, kind)


def read_contents(item: TrayItem) -> HouseholdContents:
    """What any tray item references: a household's Sims, or a lot's or room's build."""
    if item.type == ITEM_HOUSEHOLD:
        path = item.file_with_suffix(".householdbinary")
        if path is None:
            raise TrayError(f"{item.name or 'This household'} has no .householdbinary file in "
                            "the Tray folder, so its Sims can't be read.")
        return read_household(path)
    if item.type in (ITEM_LOT, ITEM_ROOM):
        path = build_file(item)
        if path is None:
            raise TrayError(f"{item.name or 'This item'} has no .blueprint or .room file in "
                            "the Tray folder.")
        return read_build(item, path)
    raise TrayError(f"{item.name or 'This item'} is a kind of tray item TrayTrim can't read.")


def build_file(item: TrayItem) -> Path | None:
    """The .blueprint or .room file of a lot or room."""
    return item.file_with_suffix(".blueprint") or item.file_with_suffix(".room")
