"""Snappy raw-block decompression (the format lot and room tray files use).

Implemented from Google's specification (github.com/google/snappy, format_description.txt):
a varint with the uncompressed length, then elements whose tag's low two bits say what they
are: 00 literal, 01 copy with an 11-bit offset, 10 copy with a 16-bit offset, 11 copy with a
32-bit offset.
"""

from __future__ import annotations


class SnappyError(ValueError):
    pass


def decompress(src: bytes, max_size: int = 512 << 20) -> bytes:
    p, n = 0, len(src)
    size = shift = 0
    while True:
        if p >= n or shift > 28:
            raise SnappyError("bad length preamble")
        b = src[p]
        p += 1
        size |= (b & 0x7F) << shift
        if b < 0x80:
            break
        shift += 7
    if size > max_size:
        raise SnappyError(f"claims {size} bytes, more than allowed")
    out = bytearray()
    try:
        while p < n:
            tag = src[p]
            p += 1
            kind = tag & 3
            if kind == 0:
                length = (tag >> 2) + 1
                if length > 60:
                    extra = length - 60
                    length = int.from_bytes(src[p:p + extra], "little") + 1
                    p += extra
                if p + length > n:
                    raise SnappyError("literal runs past the end")
                out += src[p:p + length]
                p += length
                continue
            if kind == 1:
                length = ((tag >> 2) & 7) + 4
                offset = ((tag >> 5) << 8) | src[p]
                p += 1
            elif kind == 2:
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[p:p + 2], "little")
                p += 2
            else:
                length = (tag >> 2) + 1
                offset = int.from_bytes(src[p:p + 4], "little")
                p += 4
            if offset == 0 or offset > len(out):
                raise SnappyError("copy refers outside the output")
            start = len(out) - offset
            if offset >= length:
                out += out[start:start + length]
            else:
                pattern = out[start:]
                out += (pattern * (length // offset + 1))[:length]
            if len(out) > size:
                raise SnappyError("output longer than its preamble says")
    except IndexError:
        raise SnappyError("truncated") from None
    if len(out) != size:
        raise SnappyError(f"output is {len(out)} bytes, preamble says {size}")
    return bytes(out)
