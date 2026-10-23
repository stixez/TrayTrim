"""A minimal protobuf wire-format reader.

Tray files are protobuf messages whose schemas ship with the game (compiled into its Python
protocol buffer modules). We only need a handful of fields from them, so instead of depending on
the protobuf package and generated classes, fields are read by number with this decoder.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator

VARINT, FIXED64, LEN, FIXED32 = 0, 1, 2, 5


class ProtobufError(ValueError):
    pass


def _varint(buf: bytes, p: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if p >= len(buf):
            raise ProtobufError("truncated varint")
        b = buf[p]
        p += 1
        result |= (b & 0x7F) << shift
        if b < 0x80:
            return result, p
        shift += 7
        if shift > 63:
            raise ProtobufError("varint too long")


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, int | bytes]]:
    """(field number, wire type, value): ints for varint/fixed fields, bytes for LEN fields."""
    p, n = 0, len(buf)
    while p < n:
        tag, p = _varint(buf, p)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ProtobufError("field number 0")
        if wire == VARINT:
            value, p = _varint(buf, p)
        elif wire == FIXED64:
            if p + 8 > n:
                raise ProtobufError("truncated fixed64")
            value = struct.unpack_from("<Q", buf, p)[0]
            p += 8
        elif wire == LEN:
            size, p = _varint(buf, p)
            if p + size > n:
                raise ProtobufError("truncated length-delimited field")
            value = buf[p:p + size]
            p += size
        elif wire == FIXED32:
            if p + 4 > n:
                raise ProtobufError("truncated fixed32")
            value = struct.unpack_from("<I", buf, p)[0]
            p += 4
        else:
            raise ProtobufError(f"unsupported wire type {wire}")
        yield number, wire, value


def fields_by_number(buf: bytes) -> dict[int, list[tuple[int, int | bytes]]]:
    out: dict[int, list[tuple[int, int | bytes]]] = {}
    for number, wire, value in iter_fields(buf):
        out.setdefault(number, []).append((wire, value))
    return out


def ints(entries: list[tuple[int, int | bytes]], *, fixed64: bool = False) -> list[int]:
    """All values of a repeated integer field, packed or not."""
    out: list[int] = []
    for wire, value in entries:
        if wire == LEN:
            assert isinstance(value, bytes)
            if fixed64:
                if len(value) % 8:
                    raise ProtobufError("packed fixed64 field has a partial value")
                out += struct.unpack(f"<{len(value) // 8}Q", value)
            else:
                p = 0
                while p < len(value):
                    v, p = _varint(value, p)
                    out.append(v)
        else:
            assert isinstance(value, int)
            out.append(value)
    return out


def first_int(fields: dict, number: int, default: int | None = None) -> int | None:
    entries = fields.get(number)
    if not entries or isinstance(entries[0][1], bytes):
        return default
    return entries[0][1]


def first_bytes(fields: dict, number: int) -> bytes | None:
    entries = fields.get(number)
    if not entries or not isinstance(entries[0][1], bytes):
        return None
    return entries[0][1]


def first_str(fields: dict, number: int, default: str = "") -> str:
    b = first_bytes(fields, number)
    return b.decode("utf-8", errors="replace") if b is not None else default


def messages(fields: dict, number: int) -> list[bytes]:
    return [v for w, v in fields.get(number, []) if w == LEN and isinstance(v, bytes)]


def float32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


# Writing. TrayTrim never writes tray files; these exist for tests and the demo setup.

def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return encode_varint(number << 3) + encode_varint(value)


def field_fixed64(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | FIXED64) + struct.pack("<Q", value)


def field_fixed32(number: int, value: int) -> bytes:
    return encode_varint((number << 3) | FIXED32) + struct.pack("<I", value)


def field_float(number: int, value: float) -> bytes:
    return encode_varint((number << 3) | FIXED32) + struct.pack("<f", value)


def field_bytes(number: int, value: bytes) -> bytes:
    return encode_varint((number << 3) | LEN) + encode_varint(len(value)) + value


def field_str(number: int, value: str) -> bytes:
    return field_bytes(number, value.encode())


def field_packed_varints(number: int, values) -> bytes:
    return field_bytes(number, b"".join(encode_varint(v) for v in values))


def field_packed_fixed64(number: int, values) -> bytes:
    return field_bytes(number, b"".join(struct.pack("<Q", v) for v in values))
