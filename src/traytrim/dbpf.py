"""Read and write Sims 4 DBPF 2.1 packages.

Layout (96-byte header, little-endian):
    0   magic b"DBPF"
    4   major version (2)            8   minor version (1)
    36  index entry count            40  index position (legacy, used when 64 is 0)
    44  index size in bytes          60  index minor version (3)
    64  index position

The index starts with a uint32 flags word. Bits 0/1/2 mark type / group / instance-high as
constant; each constant value is stored once right after the flags and left out of every entry.
An entry then holds the remaining fields of: type, group, instance-high, instance-low, offset,
file size, memory size. Bit 31 of the file size means a uint16 compression id and a uint16
"committed" word follow.

Packages are opened read-only and only the header and index are read up front; resource
payloads are read on demand, so a 1.7 GB merged package costs a few hundred KB of memory.
"""

from __future__ import annotations

import os
import re
import struct
import zlib
from dataclasses import dataclass
from typing import BinaryIO
from collections.abc import Iterator

HEADER_SIZE = 96
MAGIC = b"DBPF"

COMP_NONE = 0x0000
COMP_ZLIB = 0x5A42
COMP_REFPACK = 0xFFFF
COMP_REFPACK_STREAMABLE = 0xFFFE
COMP_DELETED = 0xFFE0

_EXTENDED = 0x80000000
_SIZE_MASK = 0x7FFFFFFF
_U32 = 0xFFFFFFFF
_U64 = 0xFFFFFFFFFFFFFFFF
_MAX_PACKAGE_SIZE = _U32  # offsets in the index are 32-bit
MAX_PACKAGE_SIZE = _MAX_PACKAGE_SIZE
# Larger than any real resource (the biggest are textures of some tens of MB unpacked).
MAX_RESOURCE_SIZE = 256 << 20
_HEX = r"(?:0[xX])?([0-9A-Fa-f]{1,%d})"
_KEY_RE = re.compile(rf"{_HEX % 8}[:-]{_HEX % 8}[:-]{_HEX % 16}")


class DbpfError(ValueError):
    """A package or resource that can't be read. The message is meant to be shown to the user."""


@dataclass(frozen=True, slots=True, order=True)
class ResourceKey:
    type: int
    group: int
    instance: int

    def __post_init__(self) -> None:
        if not (0 <= self.type <= _U32 and 0 <= self.group <= _U32 and 0 <= self.instance <= _U64):
            raise ValueError(f"resource key out of range: {self.type:X}:{self.group:X}:"
                             f"{self.instance:X} (type and group are 32-bit, instance 64-bit)")

    def __str__(self) -> str:
        return f"{self.type:08X}:{self.group:08X}:{self.instance:016X}"

    @classmethod
    def parse(cls, text: str) -> ResourceKey:
        """Parse "TTTTTTTT:GGGGGGGG:IIIIIIIIIIIIIIII" (hex, with or without 0x, '-' also accepted)."""
        m = _KEY_RE.fullmatch(text.strip())
        if not m:
            raise ValueError(f"expected type:group:instance in hex, got {text!r}")
        return cls(*(int(g, 16) for g in m.groups()))


@dataclass(frozen=True, slots=True)
class IndexEntry:
    key: ResourceKey
    offset: int
    file_size: int
    mem_size: int
    compression: int
    committed: int = 1

    @property
    def is_deleted(self) -> bool:
        return self.compression == COMP_DELETED


@dataclass(frozen=True, slots=True)
class Header:
    major: int
    minor: int
    entry_count: int
    index_position: int
    index_size: int


def _parse_header(raw: bytes, source: str) -> Header:
    if len(raw) < HEADER_SIZE or raw[:4] != MAGIC:
        raise DbpfError(f"{source} is not a Sims 4 package (no DBPF header).")
    major, minor = struct.unpack_from("<II", raw, 4)
    if major != 2:
        raise DbpfError(f"{source} is a DBPF {major}.{minor} package; only 2.x is supported.")
    count, pos_legacy, size = struct.unpack_from("<III", raw, 36)
    pos = struct.unpack_from("<I", raw, 64)[0] or pos_legacy
    return Header(major, minor, count, pos, size)


# One index row: (type, group, instance, offset, file_size, mem_size, compression, committed).
IndexRow = tuple[int, int, int, int, int, int, int, int]


def _index_rows(idx: bytes, count: int, source: str) -> list[IndexRow]:
    """Decode the index into plain tuples (fast enough for millions of entries)."""
    try:
        flags = struct.unpack_from("<I", idx, 0)[0]
        p = 4
        const: list[int | None] = [None, None, None]
        for bit in range(3):
            if flags & (1 << bit):
                const[bit] = struct.unpack_from("<I", idx, p)[0]
                p += 4
        variable = [bit for bit in range(3) if const[bit] is None]

        # Fast path: every entry has the compression field (true for nearly all Sims 4
        # packages), so entries are fixed-size and can be unpacked in one go.
        fmt = struct.Struct("<" + "I" * len(variable) + "IIIIHH")
        end = p + fmt.size * count
        if end <= len(idx):
            raw = list(fmt.iter_unpack(idx[p:end]))
            nv = len(variable)
            if all(r[nv + 2] & _EXTENDED for r in raw):
                rows = []
                for r in raw:
                    tgh = list(const)
                    for i, bit in enumerate(variable):
                        tgh[bit] = r[i]
                    inst_lo, offset, fsize, msize, comp, committed = r[nv:]
                    rows.append((tgh[0], tgh[1], (tgh[2] << 32) | inst_lo, offset,
                                 fsize & _SIZE_MASK, msize, comp, committed))
                return rows

        rows = []
        for _ in range(count):
            fields = []
            for bit in range(3):
                if const[bit] is None:
                    fields.append(struct.unpack_from("<I", idx, p)[0])
                    p += 4
                else:
                    fields.append(const[bit])
            inst_lo, offset, file_size, mem_size = struct.unpack_from("<IIII", idx, p)
            p += 16
            compression, committed = COMP_NONE, 1
            if file_size & _EXTENDED:
                compression, committed = struct.unpack_from("<HH", idx, p)
                p += 4
            t, g, inst_hi = fields
            rows.append((t, g, (inst_hi << 32) | inst_lo, offset, file_size & _SIZE_MASK,
                         mem_size, compression, committed))
    except struct.error:
        raise DbpfError(f"{source} has a truncated or damaged index.") from None
    return rows


def _parse_index(idx: bytes, count: int, source: str) -> list[IndexEntry]:
    return [IndexEntry(ResourceKey(t, g, i), off, fs, ms, comp, com)
            for t, g, i, off, fs, ms, comp, com in _index_rows(idx, count, source)]


def _read_header_and_index(f: BinaryIO, size: int, name: str) -> tuple[Header, bytes]:
    header = _parse_header(f.read(HEADER_SIZE), name)
    if not header.entry_count:
        return header, b""
    if header.index_size < 4:
        raise DbpfError(f"{name} has a truncated or damaged index.")
    end = header.index_position + header.index_size
    if header.index_position < HEADER_SIZE or end > size:
        raise DbpfError(f"{name} has an index that points outside the file.")
    f.seek(header.index_position)
    return header, f.read(header.index_size)


def scan_index(path: str | os.PathLike[str]) -> list[IndexRow]:
    """The resources the game would see in a package, as index rows: for each key the last
    entry wins and deleted records are dropped. Reads only the header and index."""
    name = os.path.basename(os.fspath(path))
    with open(path, "rb") as f:
        header, idx = _read_header_and_index(f, os.fstat(f.fileno()).st_size, name)
    if not idx:
        return []
    effective: dict[tuple[int, int, int], IndexRow] = {}
    for row in _index_rows(idx, header.entry_count, name):
        effective[row[0], row[1], row[2]] = row
    return [r for r in effective.values() if r[6] != COMP_DELETED]


class Package:
    """A package opened read-only. Use as a context manager, or call close()."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        self._name = os.path.basename(self.path)
        self._file: BinaryIO = open(self.path, "rb")  # noqa: SIM115 - closed in close()
        try:
            self.size = os.fstat(self._file.fileno()).st_size
            self.header, idx = _read_header_and_index(self._file, self.size, self._name)
            all_entries = _parse_index(idx, self.header.entry_count, self._name) if idx else []
            # Later entries win, and a deleted record hides an earlier live one with its key.
            self._by_key: dict[ResourceKey, IndexEntry] = {e.key: e for e in all_entries}
            self.entries: list[IndexEntry] = [e for e in all_entries if not e.is_deleted]
        except BaseException:
            self._file.close()
            raise

    def __enter__(self) -> Package:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._file.close()

    def __iter__(self) -> Iterator[IndexEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def find(self, key: ResourceKey) -> IndexEntry | None:
        """The entry the game would use for this key, or None (absent or deleted)."""
        e = self._by_key.get(key)
        return None if e is None or e.is_deleted else e

    def read_raw(self, entry: IndexEntry) -> bytes:
        """The stored bytes exactly as in the file (possibly compressed)."""
        if entry.offset + entry.file_size > self.size:
            raise DbpfError(f"{self._name}: resource {entry.key} points past the end of the file.")
        self._file.seek(entry.offset)
        return self._file.read(entry.file_size)

    def read(self, entry: IndexEntry) -> bytes:
        """The decompressed resource bytes."""
        return decompress(self.read_raw(entry), entry.compression, entry.mem_size,
                          f"{self._name}: resource {entry.key}")


def read_index(path: str | os.PathLike[str]) -> tuple[Header, list[IndexEntry]]:
    """Header and live index entries of a package, without keeping the file open."""
    with Package(path) as pkg:
        return pkg.header, pkg.entries


def decompress(raw: bytes, compression: int, mem_size: int, what: str = "resource") -> bytes:
    if compression != COMP_NONE and mem_size > MAX_RESOURCE_SIZE:
        # A few MB of compressed data can claim gigabytes: CC packages come from anywhere.
        raise DbpfError(f"{what} says it unpacks to {mem_size:,} bytes, more than any Sims 4 "
                        "resource; it's damaged or not what it claims.")
    if compression == COMP_NONE:
        data = raw
    elif compression == COMP_ZLIB:
        # Bounded by mem_size so a damaged resource can't inflate to gigabytes, and strict about
        # the stream ending exactly at the end of the stored bytes.
        d = zlib.decompressobj()
        try:
            data = d.decompress(raw, mem_size + 1)
        except zlib.error as e:
            raise DbpfError(f"{what} has damaged zlib data ({e}).") from None
        if not d.eof or d.unused_data or d.unconsumed_tail:
            raise DbpfError(f"{what} has damaged zlib data (stream doesn't end where the "
                            f"resource does).")
    elif compression in (COMP_REFPACK, COMP_REFPACK_STREAMABLE):
        data = refpack_decompress(raw, what, mem_size)
    else:
        raise DbpfError(f"{what} uses unknown compression 0x{compression:04X}.")
    if len(data) != mem_size:
        raise DbpfError(f"{what} decompressed to {len(data)} bytes, the index says {mem_size}.")
    return data


def refpack_decompress(src: bytes, what: str = "resource", expected: int | None = None) -> bytes:
    """Decompress EA RefPack (QFS).

    Header: flags byte, 0xFB, [compressed size if flags & 0x01], uncompressed size; sizes are
    big-endian, 4 bytes if flags & 0x80 else 3. Then a stream of control codes, each copying
    some literal bytes from the input followed by a back-reference into the output.

    expected is the size the package index gives; the header has to agree with it before
    anything is unpacked, so a damaged header can't make the output grow to gigabytes.
    """
    if len(src) < 5 or src[1] != 0xFB:
        raise DbpfError(f"{what} is not valid RefPack data.")
    flags = src[0]
    width = 4 if flags & 0x80 else 3
    p = 2 + (width if flags & 0x01 else 0)
    size = int.from_bytes(src[p:p + width], "big")
    p += width
    if expected is not None and size != expected:
        raise DbpfError(f"{what} RefPack header says {size:,} bytes, the index says "
                        f"{expected:,}.")
    if size > MAX_RESOURCE_SIZE:
        raise DbpfError(f"{what} says it unpacks to {size:,} bytes, more than any Sims 4 "
                        "resource; it's damaged or not what it claims.")
    out = bytearray()
    n = len(src)
    try:
        while p < n:
            b0 = src[p]
            if b0 < 0x80:
                b1 = src[p + 1]
                p += 2
                lit = b0 & 0x03
                cnt = ((b0 >> 2) & 0x07) + 3
                dist = ((b0 & 0x60) << 3) + b1 + 1
            elif b0 < 0xC0:
                b1, b2 = src[p + 1], src[p + 2]
                p += 3
                lit = b1 >> 6
                cnt = (b0 & 0x3F) + 4
                dist = ((b1 & 0x3F) << 8) + b2 + 1
            elif b0 < 0xE0:
                b1, b2, b3 = src[p + 1], src[p + 2], src[p + 3]
                p += 4
                lit = b0 & 0x03
                cnt = ((b0 & 0x0C) << 6) + b3 + 5
                dist = ((b0 & 0x10) << 12) + (b1 << 8) + b2 + 1
            elif b0 < 0xFC:
                p += 1
                lit = ((b0 & 0x1F) << 2) + 4
                cnt = dist = 0
            else:
                p += 1
                lit = b0 & 0x03
                if p + lit > n:
                    raise IndexError
                out += src[p:p + lit]
                break
            if p + lit > n:
                raise IndexError
            out += src[p:p + lit]
            p += lit
            if cnt:
                start = len(out) - dist
                if start < 0:
                    raise DbpfError(f"{what} has a RefPack reference before the start of the data.")
                if dist >= cnt:
                    out += out[start:start + cnt]
                else:  # overlapping copy repeats the last `dist` bytes
                    pattern = out[start:]
                    out += (pattern * (cnt // dist + 1))[:cnt]
            if len(out) > size:
                raise DbpfError(f"{what} RefPack data unpacks past the {size} bytes its header "
                                "says.")
    except IndexError:
        raise DbpfError(f"{what} has truncated RefPack data.") from None
    if len(out) != size:
        raise DbpfError(f"{what} RefPack data unpacked to {len(out)} bytes, its header says {size}.")
    return bytes(out)


def refpack_compress(data: bytes) -> bytes:
    """Compress to RefPack. TrayTrim writes zlib; this exists so tests can build RefPack input."""
    n = len(data)
    flags = 0x10 | (0x80 if n > 0xFFFFFF else 0)
    out = bytearray([flags, 0xFB]) + n.to_bytes(4 if flags & 0x80 else 3, "big")
    table: dict[bytes, int] = {}
    lit_start = i = 0

    def flush_literals(end: int) -> None:
        nonlocal lit_start
        # Long literal runs go out in chunks of 4..112 bytes (a multiple of 4).
        while end - lit_start > 3:
            chunk = min((end - lit_start) & ~3, 112)
            out.append(0xE0 + (chunk - 4) // 4)
            out.extend(data[lit_start:lit_start + chunk])
            lit_start += chunk

    while i + 3 <= n:
        k = data[i:i + 3]
        cand = table.get(k)
        table[k] = i
        if cand is not None and i - cand <= 131072:
            dist = i - cand
            length = 3
            while length < 1028 and i + length < n and data[cand + length] == data[i + length]:
                length += 1
            fits = ((length <= 10 and dist <= 1024)
                    or (4 <= length <= 67 and dist <= 16384)
                    or (length >= 5))
            if fits:
                flush_literals(i)
                lit = i - lit_start
                lits = data[lit_start:i]
                d = dist - 1
                if length <= 10 and dist <= 1024:
                    out += bytes([((d >> 3) & 0x60) | ((length - 3) << 2) | lit, d & 0xFF])
                elif 4 <= length <= 67 and dist <= 16384:
                    out += bytes([0x80 | (length - 4), (lit << 6) | (d >> 8), d & 0xFF])
                else:
                    c = length - 5
                    out += bytes([0xC0 | ((d >> 12) & 0x10) | ((c >> 6) & 0x0C) | lit,
                                  (d >> 8) & 0xFF, d & 0xFF, c & 0xFF])
                out += lits
                for j in range(i + 1, min(i + length, n - 2)):
                    table[data[j:j + 3]] = j
                i += length
                lit_start = i
                continue
        i += 1
    flush_literals(n)
    out.append(0xFC | (n - lit_start))
    out += data[lit_start:n]
    return bytes(out)


class PackageWriter:
    """Write a new package by streaming resources to disk, then the index and header.

    Use add() for decompressed bytes (stored zlib-compressed) or add_raw() to copy a resource's
    stored bytes unchanged from another package.

    Everything goes to "<path>.partial" first; close() finishes the file and renames it into
    place, so a failed export never leaves a broken package under the real name. Leaving a
    `with` block through an exception calls abort() instead.
    """

    _ENTRY_SIZE = 32  # full index entry: 7 uint32 + 2 uint16

    def __init__(self, path: str | os.PathLike[str]):
        self.path = os.fspath(path)
        self._partial = self.path + ".partial"
        self._file: BinaryIO = open(self._partial, "wb")  # noqa: SIM115 - closed in close()
        self._file.write(bytes(HEADER_SIZE))
        self._pos = HEADER_SIZE
        self._entries: list[IndexEntry] = []
        self._keys: set[ResourceKey] = set()
        self._done = False

    def __enter__(self) -> PackageWriter:
        return self

    def __exit__(self, exc_type, *exc) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    def add(self, key: ResourceKey, data: bytes, *, compress: bool = True) -> IndexEntry:
        if compress:
            return self.add_raw(key, zlib.compress(data, 9), COMP_ZLIB, len(data))
        return self.add_raw(key, data, COMP_NONE, len(data))

    def add_raw(self, key: ResourceKey, raw: bytes, compression: int, mem_size: int) -> IndexEntry:
        if self._done:
            raise ValueError("package is already closed")
        if key in self._keys:
            raise ValueError(f"duplicate resource {key}")
        if compression == COMP_DELETED:
            raise ValueError("can't write a deleted record")
        if not 0 <= compression <= 0xFFFF or not 0 <= mem_size <= _U32:
            raise ValueError(f"resource {key}: compression or size out of range")
        if len(raw) > _SIZE_MASK:
            raise DbpfError(f"Resource {key} is {len(raw):,} bytes; a package entry can hold at "
                            "most 2 GB.")
        index_end = (self._pos + len(raw) + 4 + self._ENTRY_SIZE * (len(self._entries) + 1))
        if index_end > _MAX_PACKAGE_SIZE:
            raise DbpfError("The export would be larger than 4 GB, which a single package "
                            "can't hold.")
        self._file.write(raw)
        entry = IndexEntry(key, self._pos, len(raw), mem_size, compression, 1)
        self._pos += len(raw)
        self._entries.append(entry)
        self._keys.add(key)
        return entry

    def close(self) -> None:
        """Write the index and header and move the finished package into place."""
        if self._done:
            return
        self._done = True
        try:
            index = bytearray(struct.pack("<I", 0))  # no constant fields
            for e in self._entries:
                index += struct.pack("<7I", e.key.type, e.key.group, e.key.instance >> 32,
                                     e.key.instance & _U32, e.offset,
                                     e.file_size | _EXTENDED, e.mem_size)
                index += struct.pack("<HH", e.compression, e.committed)
            index_pos = self._pos
            self._file.write(index)
            header = bytearray(HEADER_SIZE)
            header[:4] = MAGIC
            struct.pack_into("<II", header, 4, 2, 1)
            struct.pack_into("<III", header, 36, len(self._entries), 0, len(index))
            struct.pack_into("<I", header, 60, 3)
            struct.pack_into("<I", header, 64, index_pos)
            self._file.seek(0)
            self._file.write(header)
            self._file.close()
            os.replace(self._partial, self.path)
        except BaseException:
            self._discard()
            raise

    def abort(self) -> None:
        """Throw away everything written so far."""
        if not self._done:
            self._done = True
            self._discard()

    def _discard(self) -> None:
        self._file.close()
        try:
            os.remove(self._partial)
        except OSError:
            pass
