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

That name mostly isn't the CC's: what follows "yfAcc" is the EA item the part was cloned
from, which the creator rarely renames (in a real Mods folder a corset mini dress was
"DressMaxi_Lemon" and a Miu Miu bag "IndexLeftEyeballRing"). The package's file name is the
name players know. So when a package holds one item, in any number of swatches, the item goes
by its package's name made readable (`readable_package_name`); in a merge of many items each
keeps its part name.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass, replace
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
_COPY = re.compile(r"\s*\(\d+\)$")          # "Slingbacks(1)": a second download of the file
_JOINED = re.compile(r"(?<=[^\W_])-(?=[^\W_])")  # "Metallic-Corset", not "NA - Skyla"
# "Hair_Merge", "MERGED", "MyMerge"; not "Submerged" or "Emergency".
_MERGE = re.compile(r"(?i:(?<![a-z])merge)|(?<=[a-z])Merge")
# What a package holds next to one CAS item that makes it a set, named after the whole set.
_NOT_CAS = {restypes.OBJECT_CATALOG, restypes.OBJECT_DEFINITION, *restypes.BUILD_NAMES}


@dataclass(frozen=True)
class ItemName:
    title: str             # the item's own name
    creator: str = ""      # the prefix of a CAS part's internal name, when there is one
    raw: str = ""          # the name as stored, for tooltips
    package: str = ""      # its package's name made readable, when the package is this item
    swatch: str = ""       # which swatch of the item it is ("Solid Magenta"), when that shows

    @property
    def full(self) -> str:
        """On one line: the package's name and the swatch for an item named after its
        package (its swatches would read the same otherwise), else the item's own name."""
        return " \u00b7 ".join(filter(None, (self.package, self.swatch))) or self.title


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


def readable_package_name(stem: str) -> str:
    """A package's file name as a name: "Bergdorf_MiuMiu_WanderTeddyBag_LH" -> "Bergdorf Miu
    Miu Wander Teddy Bag LH", "Nivelle_yfTop_HalterBackZip_202601011200" -> "Nivelle Top Halter
    Back Zip", "[SUNBERRY]V.2 Logo Belt" -> "[SUNBERRY] V.2 Logo Belt"."""
    text = _COPY.sub("", stem.replace("+", " "))
    words = []
    for part in text.split("_"):
        if not part or _STAMP.match(part):
            continue
        if _AGE_GENDER.match(part):  # "yfTop": the category is enough
            part = part[2:]
        if part.count("-") > 1:  # "Metallic-Corset-Mini-Dress": the hyphens are spaces;
            part = _JOINED.sub(" ", part)  # one is part of a word ("T-Shirt", "cc-star")
        words.append(_CAMEL.sub(" ", part))
    text = " ".join(words)
    text = re.sub(r"\]\s*-?", "] ", text).replace("[", " [")  # "[BRAND]-Long" -> "[BRAND] Long"
    text = " ".join(text.split())
    return text or stem


def _item_and_swatch(raw: str) -> tuple[str, list[str]]:
    """Which item a CAS part is a swatch of, and the swatch's words:
    "LVNDR_yfAcc_ManiRound_beige_2025..." is ("lvndr_acc_maniround", ["beige"]). The swatch is
    the last part, so "Me_yfTop_Halter_Neck_red" and "Me_yfTop_Halter_Backless_red" are two
    items. Age and gender don't count: "Me_yfHair_Bob_black" and "Me_cuHair_Bob_black" are one
    hair for two frames. A name of the creator's own, "Me_Long_Waves_black", is all but its
    last part too (two parts are a name)."""
    parts = [p for p in raw.split("_") if p and not _STAMP.match(p)]
    at = next((i for i, p in enumerate(parts) if _AGE_GENDER.match(p)), None)
    if at is None:
        cut = len(parts) - 1 if len(parts) >= 3 else len(parts)
        return "_".join(parts[:cut]).casefold(), parts[cut:]
    cut = len(parts) - 1 if len(parts) > at + 2 else len(parts)
    creators = dict.fromkeys(p.casefold() for p in parts[:at])  # "A_A_yfAcc" is A's too
    item = [parts[at][2:], *parts[at + 1:cut]]
    return "_".join([*creators, *(p.casefold() for p in item)]), parts[cut:]


def item_base(raw: str) -> str:
    """Which item a CAS part is a swatch of: "LVNDR_yfAcc_ManiRound_beige_2025..." and
    "LVNDR_yfAcc_ManiRound_red_2025..." are both "lvndr_acc_maniround". The creator's prefix
    counts: two creators' parts cloned from one EA item are two items."""
    return _item_and_swatch(raw)[0]


def swatch_name(raw: str) -> str:
    """"Bergdorf_yfAcc_IndexLeftEyeballRing_SolidMagenta_2023..." -> "Solid Magenta"."""
    return " ".join(_CAMEL.sub(" ", p) for p in _item_and_swatch(raw)[1])


def readable_cas_name(raw: str) -> ItemName:
    """"LVNDR_yfAcc_ManiRoundSolidColor_beige_2025..." -> "Mani Round Solid Color beige",
    creator "LVNDR". Names that don't follow that pattern are shown as they are."""
    parts = [p for p in raw.split("_") if p and not _STAMP.match(p)]
    at = next((i for i, p in enumerate(parts) if _AGE_GENDER.match(p)), None)
    if at is None or at == len(parts) - 1:
        text = " ".join(parts) or raw
        # Only numbers ("-1889534009:976845349", left by some tools) say nothing: no title, so
        # the package's name is shown instead.
        return ItemName(text if any(c.isalpha() for c in text) else "", "", raw)
    creators = list(dict.fromkeys(parts[:at]))  # "A_A_A_yfAcc" -> "A"
    words = [_CAMEL.sub(" ", p) for p in parts[at + 1:]]
    return ItemName(" ".join(words), " ".join(creators).strip(" ."), raw)


def cas_file_stem(raw: str) -> str:
    """A CAS part's internal name as a file name, the way creators name their packages:
    "A_A_yfTop_HalterBackZip_SolidBlack_2026..." -> "A_yfTop_HalterBackZip_SolidBlack" (no
    date stamp, no part repeated right after itself)."""
    parts = [p for p in raw.split("_") if p and not _STAMP.match(p)]
    return "_".join(p for i, p in enumerate(parts) if i == 0 or p != parts[i - 1])


def clean(text: str) -> str:
    """Catalog names sometimes carry markup (<font color=...>); players see the text only."""
    return " ".join(_TAG.sub("", text).split())


class _Package:
    def __init__(self, path: Path):
        self.pkg = dbpf.Package(path)
        self._strings: dict[int, str] | None = None
        self._one_item: bool | None = None
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

    def one_item(self) -> bool:
        """Whether the package is one CAS item, in any number of swatches: not a merge, and no
        objects or build items next to it (a set named after the whole set). Worked out once
        per package (cached by file, size and time across households); False if it can't be
        read."""
        if self._one_item is None:
            try:
                stat = os.stat(self.pkg.path)
                self._one_item = _one_item(self.pkg, (str(self.pkg.path), stat.st_size,
                                                      stat.st_mtime_ns))
            except (dbpf.DbpfError, struct.error, OSError):
                self._one_item = False
        return self._one_item


def _one_item(pkg: dbpf.Package, file: tuple) -> bool:
    if file in _ONE_ITEM:
        return _ONE_ITEM[file]
    answer = not _MERGE.search(os.path.basename(file[0]))
    bases: set[str] = set()
    for e in pkg if answer else ():
        if e.key.type in _NOT_CAS:
            answer = False
            break
        if e.key.type != restypes.CASP:
            continue
        try:
            raw = cas_internal_name(pkg.read(e))
        except (dbpf.DbpfError, struct.error):
            continue
        if raw and any(c.isalpha() for c in raw):  # numbers only say nothing
            bases.add(item_base(raw))
            if len(bases) > 1:
                break
    answer = answer and len(bases) == 1
    if len(_ONE_ITEM) > 20_000:  # a whole Mods folder is a few thousand packages
        _ONE_ITEM.clear()
    _ONE_ITEM[file] = answer
    return answer


# (path, size, modified) -> whether the package holds one item; read once per change of the
# file. A merge stops at its second item, so only a package of one item is read through (a few
# hundred swatches at most).
_ONE_ITEM: dict[tuple, bool] = {}


def item_names(refs: list[LocatedRef], cancel=None) -> dict[TrayRef, ItemName]:
    """Names for the CC items among refs that have one (objects, build items, CAS parts),
    read from the package each one is used from."""
    out: dict[TrayRef, ItemName] = {}
    wanted = [r for r in refs if r.where is Where.CC and r.used is not None
              and r.ref.kind in (RefKind.OBJECT, RefKind.BUILD, RefKind.CAS_PART)]
    # A package at a time: a big household's CC comes from more packages than a process may
    # have open at once.
    wanted.sort(key=lambda r: str(r.used.path))
    path: Path | None = None
    pkg: _Package | None = None
    try:
        for r in wanted:
            if cancel is not None and cancel.is_set():
                break
            if r.used.path != path:
                if pkg is not None:
                    pkg.pkg.close()
                path = r.used.path
                try:
                    pkg = _Package(path)
                except (dbpf.DbpfError, OSError):
                    pkg = None
            if pkg is None:
                continue
            try:
                name = _name(r, r.ref.kind, pkg)
            except (dbpf.DbpfError, struct.error, OSError):
                continue
            if name is not None:
                out[r.ref] = name
    finally:
        if pkg is not None:
            pkg.pkg.close()
    return out


def _name(r: LocatedRef, kind: RefKind, pkg: _Package) -> ItemName | None:
    if kind is RefKind.CAS_PART:
        raw = cas_internal_name(pkg.pkg.read(r.used.entry))
        if not raw:
            return None
        name = readable_cas_name(raw)
        if pkg.one_item():
            name = replace(name, package=readable_package_name(r.used.path.stem),
                           swatch=swatch_name(raw))
        return name
    if kind is RefKind.OBJECT:
        entry = pkg.entry(restypes.OBJECT_CATALOG, r.ref.instance)
    else:
        entry = r.used.entry  # a build item is its own catalog resource
    if entry is None:
        return None
    key = catalog_name_hash(pkg.pkg.read(entry))
    text = clean(pkg.strings().get(key, "")) if key else ""
    return ItemName(text, "", text) if text else None
