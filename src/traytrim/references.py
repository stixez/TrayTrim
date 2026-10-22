"""What a resource refers to.

Structured, verified on a real Mods folder (thousands of samples, CASP versions 26 to 52):
  CASP  uint32 version, uint32 offset; the TGI list starts at 8 + offset: uint8 count, then
        count x (uint64 instance, uint32 group, uint32 type), ending exactly at the end of the
        resource. Every key the part uses (meshes, textures, region map, other parts) is in it.
  GEOM  RCOL wrapper: 5 uint32 (version, public chunks, unused, external count, internal count),
        16 bytes per internal and external key, then the chunk table (uint32 offset, size).
        The GEOM chunk starts "GEOM", uint32 version, uint32 offset, uint32 size; its TGI block
        starts at 12 + offset: uint32 count, then count x (uint32 type, uint32 group, uint64
        instance). External keys in the wrapper are (uint64 instance, uint32 type, uint32 group).

Scanned (sliders, sculpts, skin tones, presets, and other small resources whose formats aren't
parsed here): the resource bytes are searched for the 64-bit instances of other resources in the
same package. Instances are random 64-bit hashes, so an accidental match is extremely unlikely;
small instances (below 2^32) are ignored to keep it that way. Found this way = marked "scan".
Textures, meshes and thumbnails are leaves and aren't scanned.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from traytrim import restypes
from traytrim.dbpf import ResourceKey

LEAF_TYPES = restypes.TEXTURE_TYPES | {restypes.THUM, restypes.OBJECT_THUMB,
                                       restypes.STRING_TABLE}
# Resources whose references may point into other packages (a recolor's object definition
# using a mesh from a separate mesh package). Their bytes are also checked against the whole
# index, limited to ASSET_TYPES so an accidental match can't pull in unrelated content.
CROSS_SCAN_TYPES = {restypes.OBJECT_DEFINITION, restypes.OBJECT_CATALOG, restypes.MODEL}
ASSET_TYPES = (restypes.TEXTURE_TYPES
               | {restypes.MODEL, restypes.MODEL_LOD, restypes.LIGHT, restypes.SLOT,
                  restypes.RIG})
# Large resources that are never scanned (they're binary payloads, not reference holders).
_NO_SCAN_TYPES = LEAF_TYPES | {restypes.GEOM, restypes.BLEND_GEOMETRY, restypes.DEFORMER_MAP,
                                restypes.BONE_DELTA}
_MIN_SCAN_INSTANCE = 1 << 32
_MAX_SCAN_BYTES = 4 << 20


class ReferenceError_(ValueError):
    pass


@dataclass(frozen=True)
class Reference:
    key: ResourceKey
    how: str  # "tgi" (from a parsed key list) or "scan"


def is_null(key: ResourceKey) -> bool:
    return key.instance == 0 or key.type == 0


def casp_keys(data: bytes) -> list[ResourceKey]:
    if len(data) < 8:
        raise ReferenceError_("CAS part too short")
    _version, offset = struct.unpack_from("<II", data, 0)
    p = 8 + offset
    if p >= len(data):
        raise ReferenceError_("CAS part key list outside the resource")
    count = data[p]
    p += 1
    if p + 16 * count > len(data):
        raise ReferenceError_("CAS part key list truncated")
    out = []
    for i in range(count):
        instance, group, type_id = struct.unpack_from("<QII", data, p + 16 * i)
        out.append(ResourceKey(type_id, group, instance))
    return out


def geom_keys(data: bytes) -> list[ResourceKey]:
    if len(data) < 20:
        raise ReferenceError_("mesh too short")
    _ver, _public, _unused, external, internal = struct.unpack_from("<5I", data, 0)
    p = 20
    if p + 16 * (internal + external) + 8 * internal > len(data):
        raise ReferenceError_("mesh header truncated")
    out = []
    p += 16 * internal
    for i in range(external):
        instance, type_id, group = struct.unpack_from("<QII", data, p + 16 * i)
        out.append(ResourceKey(type_id, group, instance))
    p += 16 * external
    for i in range(internal):
        chunk_offset, chunk_size = struct.unpack_from("<II", data, p + 8 * i)
        chunk = data[chunk_offset:chunk_offset + chunk_size]
        if chunk[:4] != b"GEOM" or len(chunk) < 16:
            continue
        _gver, tgi_offset, _tgi_size = struct.unpack_from("<III", chunk, 4)
        q = 12 + tgi_offset
        if q + 4 > len(chunk):
            raise ReferenceError_("mesh key list outside the chunk")
        count = struct.unpack_from("<I", chunk, q)[0]
        q += 4
        if q + 16 * count > len(chunk):
            raise ReferenceError_("mesh key list truncated")
        for j in range(count):
            type_id, group, instance = struct.unpack_from("<IIQ", chunk, q + 16 * j)
            out.append(ResourceKey(type_id, group, instance))
    return out


_MAX_CROSS_SCAN_BYTES = 256 << 10  # reference holders are small; geometry isn't scanned


def large_values(data: bytes) -> set[int]:
    """Every 64-bit value of at least 2^32 in data, at any byte alignment."""
    data = data[:_MAX_CROSS_SCAN_BYTES]
    found: set[int] = set()
    for shift in range(8):
        usable = (len(data) - shift) // 8 * 8
        if usable > 0:
            found.update(v for v in struct.unpack(f"<{usable // 8}Q",
                                                   data[shift:shift + usable])
                         if v >= _MIN_SCAN_INSTANCE)
    return found


def scan_instances(data: bytes, candidates: set[int]) -> set[int]:
    """Which of these 64-bit instances occur anywhere in data (at any byte alignment)."""
    if not candidates or len(data) < 8:
        return set()
    data = data[:_MAX_SCAN_BYTES]
    found: set[int] = set()
    for shift in range(8):
        usable = (len(data) - shift) // 8 * 8
        if usable <= 0:
            continue
        values = struct.unpack(f"<{usable // 8}Q", data[shift:shift + usable])
        found |= candidates.intersection(values)
    return found


def references(key: ResourceKey, data: bytes,
               package_keys: dict[int, list[ResourceKey]]) -> list[Reference]:
    """Keys `key` (with decompressed `data`) refers to. package_keys maps instance -> keys of the
    package the resource comes from (used by the scan)."""
    try:
        if key.type == restypes.CASP:
            return [Reference(k, "tgi") for k in casp_keys(data) if not is_null(k)]
        if key.type == restypes.GEOM:
            return [Reference(k, "tgi") for k in geom_keys(data) if not is_null(k)]
    except struct.error as e:  # a damaged resource; reported, never fatal for the plan
        raise ReferenceError_(f"damaged {restypes.name(key.type).lower()} ({e})") from None
    if key.type in _NO_SCAN_TYPES:
        return []
    candidates = {i for i in package_keys if i >= _MIN_SCAN_INSTANCE}
    out = []
    for instance in scan_instances(data, candidates):
        for k in package_keys[instance]:
            if k != key:
                out.append(Reference(k, "scan"))
    return out
