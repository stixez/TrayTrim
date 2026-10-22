"""Resource type ids, each with where it was verified.

Sources: "user" = confirmed with earlier tools on real files; "s4tk" = Sims 4 Toolkit's
BinaryResourceType enum; "observed" = every tray reference of that kind resolved to this type in
the index of a real Mods folder and game install (checked on real households).
"""

CASP = 0x034AEECB          # CAS part. user, s4tk
GEOM = 0x015A1849          # mesh. user
RLE2 = 0x3453CF95          # texture. user, s4tk
RLES = 0xBA856C78          # texture with specular. user, s4tk
DST = 0x00B2D882           # DXT texture. user, s4tk
THUM = 0x3C1AF1F2          # CAS thumbnail. user; observed next to every CASP
REGION_MAP = 0xAC16FBEC    # user, s4tk
CAS_PRESET = 0xEAA32ADD    # s4tk
STRING_TABLE = 0x220557DA  # s4tk
SIM_MODIFIER = 0xC5F6763E  # slider (SMOD). observed: all slider keys in tray data
SCULPT = 0x9D1AB874        # observed: all sculpt keys in tray data
SKIN_TONE = 0x0354796A     # observed: the skin tone key in tray data
LRLE = 0x2BC04EDF          # texture (LRLE format). community name; observed: textures referenced
                           # as another texture type resolve to it by instance
DEFORMER_MAP = 0xDB43E069  # slider shape data. community name; found by scanning sliders
BONE_DELTA = 0x0355E0A6    # observed sharing instances with sim modifiers
BLEND_GEOMETRY = 0x067CAA11  # observed sharing instances with modifiers and sculpts; verify too

# Build/buy. ObjectDefinition and ObjectCatalog: s4tk, and observed: every object placed on a
# real lot has exactly one of each with its definition id as instance. Object thumbnails:
# observed next to them (JPEG). Model, ModelLod, Light, Slot, Rig: s4tk.
OBJECT_DEFINITION = 0xC0DB5AE7
OBJECT_CATALOG = 0x319E4F1D
OBJECT_THUMB = 0x3C2A8647
MODEL = 0x01661233
MODEL_LOD = 0x01D10F34
LIGHT = 0x03B4C61D
SLOT = 0xD3044521
RIG = 0x8EAF13DE

# Build catalog entries a lot or room lists. Names only where s4pi-reforged and TS4Tools agree
# (and walls and floors are by far the most common keys on real lots); the other build types
# those tools name differently stay unnamed. Each has an OBJECT_THUMB with the same instance
# (observed).
WALL = 0xD5F0F921
FLOOR = 0xB4F762C9
FENCE = 0x0418FE2A
FOUNDATION = 0x2FAE983E
RAILING = 0x1C1CF1F7
BUILD_NAMES = {WALL: "Wall", FLOOR: "Floor", FENCE: "Fence", FOUNDATION: "Foundation",
               RAILING: "Railing"}

# Texture formats. A reference to a texture is resolved by instance across these: real meshes
# and CAS parts refer to e.g. a DST texture that's stored as RLE2 (seen on hundreds of keys).
TEXTURE_TYPES = {RLE2, RLES, DST, LRLE}

NAMES = {
    CASP: "CAS part", GEOM: "Mesh", RLE2: "Texture", RLES: "Texture", DST: "Texture",
    LRLE: "Texture", DEFORMER_MAP: "Deformer map",
    OBJECT_DEFINITION: "Object definition", OBJECT_CATALOG: "Object catalog",
    OBJECT_THUMB: "Thumbnail", MODEL: "Model", MODEL_LOD: "Model LOD", LIGHT: "Light",
    SLOT: "Slot", RIG: "Rig",
    THUM: "Thumbnail", REGION_MAP: "Region map", CAS_PRESET: "CAS preset",
    STRING_TABLE: "String table", SIM_MODIFIER: "Slider", SCULPT: "Sculpt",
    SKIN_TONE: "Skin tone", BONE_DELTA: "Bone delta", BLEND_GEOMETRY: "Blend geometry",
    **BUILD_NAMES,
}


def name(type_id: int) -> str:
    return NAMES.get(type_id, f"Type {type_id:08X}")
