"""Display names for CC items, read from their packages.

Objects and build items (walls, floors, stairs...) carry the catalog name players see: the
catalog resource (the object's ObjectCatalog, or the build item's own catalog resource) holds a
32-bit name hash at offset 8, and a string table (STBL) in the same package maps it to the text.
Observed on every CC object and build item of a real Tray folder; the offset follows the
catalog common block as s4pi describes it (version, then common block version, name hash).

CAS parts have no catalog name in practice (creators rarely add string tables for them), but
every CAS part has the internal name its creator gave it in Sims 4 Studio, right after the
preset list (s4pi CASPartResource; checked on 1,940 real parts, none had presets). It looks
like "LVNDR_yfAcc_ManiRoundSolidColor_beige_202511021041562324" and is turned into
"Mani Round Solid Color beige" by `readable_cas_name`, keeping the original for tooltips.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

from traytrim import dbpf, restypes
from traytrim.resolve import LocatedRef, Where
from traytrim.tray import RefKind, TrayRef

_ENGLISH = 0x00  # a string table's language is the top byte of its instance
_TAG = re.compile(r"<[^>]*>")
_STAMP = re.compile(r"^\d{8,}$")
# "yfAcc", "ymHair", "puTop", "cuShoes": age, gender, then the part category, as Sims 4 Studio
# names parts. Everything before it is the creator's prefix.
_AGE_GENDER = re.compile(r"^[bpcytaeu][fmu][A-Z][A-Za-z]*$")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass(frozen=True)
class ItemName:
    title: str             # what to show
    creator: str = ""      # the prefix of a CAS part's internal name, when there is one
    raw: str = ""          # the name as stored, for tooltips


def string_table(data: bytes) -> dict[int, str]:
    """Key hash -> text of an STBL resource (version 5, the one the game writes)."""
    if len(data) < 21 or data[:4] != b"STBL":
        return {}
    count = struct.unpack_from("<Q", data, 7)[0]
    p, out = 21, {}
    for _ in range(count):
        if p + 7 > len(data):
            break
        key, _flags, length = struct.unpack_from("<IBH", data, p)
        p += 7
        out[key] = _text(data[p:p + length])
        p += length
    return out


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # some tools wrote Windows-1252 ("Spot Léon")
        return raw.decode("cp1252", "replace")


def catalog_name_hash(data: bytes) -> int:
    """The name hash of a catalog resource (ObjectCatalog, walls, floors...), 0 if absent."""
    return struct.unpack_from("<I", data, 8)[0] if len(data) >= 12 else 0


def cas_internal_name(data: bytes) -> str:
    """The name a CAS part was given in Sims 4 Studio, or "" if it can't be read."""
    if len(data) < 13:
        return ""
    presets = struct.unpack_from("<I", data, 8)[0]
    if presets:
        return ""  # presets come first and have no fixed size; not seen on real CC
    p, n, shift = 12, 0, 0
    while p < len(data):
        b = data[p]
        p += 1
        n |= (b & 0x7F) << shift
        if b < 0x80:
            break
        shift += 7
        if shift > 28:
            return ""
    if n > 1024 or p + n > len(data) or n % 2:
        return ""
    return data[p:p + n].decode("utf-16-be", "replace").strip()


def readable_cas_name(raw: str) -> ItemName:
    """"LVNDR_yfAcc_ManiRoundSolidColor_beige_2025..." -> "Mani Round Solid Color beige",
    creator "LVNDR". Names that don't follow that pattern are shown as they are."""
    parts = [p for p in raw.split("_") if p and not _STAMP.match(p)]
    at = next((i for i, p in enumerate(parts) if _AGE_GENDER.match(p)), None)
    if at is None or at == len(parts) - 1:
        return ItemName(" ".join(parts) or raw, "", raw)
    creators = list(dict.fromkeys(parts[:at]))  # "A_A_A_yfAcc" -> "A"
    words = [_CAMEL.sub(" ", p) for p in parts[at + 1:]]
    return ItemName(" ".join(words), " ".join(creators).strip(" ."), raw)


def clean(text: str) -> str:
    """Catalog names sometimes carry markup (<font color=...>); players see the text only."""
    return " ".join(_TAG.sub("", text).split())


class _Package:
    def __init__(self, path: Path):
        self.pkg = dbpf.Package(path)
        self._strings: dict[int, str] | None = None
        self._by_instance: dict[int, list[dbpf.IndexEntry]] = {}
        for e in self.pkg:
            self._by_instance.setdefault(e.key.instance, []).append(e)

    def strings(self) -> dict[int, str]:
        """English string tables first; any other language fills the gaps."""
        if self._strings is None:
            tables = [e for e in self.pkg if e.key.type == restypes.STRING_TABLE]
            tables.sort(key=lambda e: (e.key.instance >> 56) != _ENGLISH)
            self._strings = {}
            for e in reversed(tables):  # English last, so it wins
                try:
                    self._strings.update(string_table(self.pkg.read(e)))
                except (dbpf.DbpfError, struct.error):
                    continue
        return self._strings

    def entry(self, type_id: int, instance: int) -> dbpf.IndexEntry | None:
        return next((e for e in self._by_instance.get(instance, ()) if e.key.type == type_id),
                    None)


def item_names(refs: list[LocatedRef], cancel=None) -> dict[TrayRef, ItemName]:
    """Names for the CC items among refs that have one (objects, build items, CAS parts),
    read from the package each one is used from."""
    out: dict[TrayRef, ItemName] = {}
    packages: dict[Path, _Package | None] = {}
    try:
        for r in refs:
            if cancel is not None and cancel.is_set():
                break
            if r.where is not Where.CC or r.used is None:
                continue
            kind = r.ref.kind
            if kind not in (RefKind.OBJECT, RefKind.BUILD, RefKind.CAS_PART):
                continue
            path = r.used.path
            if path not in packages:
                try:
                    packages[path] = _Package(path)
                except (dbpf.DbpfError, OSError):
                    packages[path] = None
            pkg = packages[path]
            if pkg is None:
                continue
            try:
                name = _name(r, kind, pkg)
            except (dbpf.DbpfError, struct.error, OSError):
                continue
            if name is not None:
                out[r.ref] = name
    finally:
        for pkg in packages.values():
            if pkg is not None:
                pkg.pkg.close()
    return out


def _name(r: LocatedRef, kind: RefKind, pkg: _Package) -> ItemName | None:
    if kind is RefKind.CAS_PART:
        raw = cas_internal_name(pkg.pkg.read(r.used.entry))
        return readable_cas_name(raw) if raw else None
    if kind is RefKind.OBJECT:
        entry = pkg.entry(restypes.OBJECT_CATALOG, r.ref.instance)
    else:
        entry = r.used.entry  # a build item is its own catalog resource
    if entry is None:
        return None
    key = catalog_name_hash(pkg.pkg.read(entry))
    text = clean(pkg.strings().get(key, "")) if key else ""
    return ItemName(text, "", text) if text else None
