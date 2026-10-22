"""The export plan: every CC resource a household needs, which copy to take, and why.

Starting from the household's CC references (CAS parts, sliders, sculpts, skin tones...), each
resource's own references are followed (see references.py) until everything needed is either
in the plan or in the game.

Decisions, all shown in the report:
- Several Mods copies of a resource: identical copies aren't a conflict. Copies with different
  content are; the plan takes the one the game uses, assumed to be the one that loads last
  (alphabetical order in Mods; see modsindex.py).
- A dependency that's EA content (in the game) isn't copied, even when a Mods package overrides
  it: that override is a default replacement that changes the item for every Sim.
- A tray reference that's EA content overridden by Mods (the Sim wears an EA item that a mod
  replaces) is included, so the Sim looks the same, and flagged as a default replacement.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict, deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from traytrim import dbpf, restypes
from traytrim.dbpf import ResourceKey
from traytrim.index import ModsHit, ResourceIndex
from traytrim.references import (ASSET_TYPES, CROSS_SCAN_TYPES, LEAF_TYPES, ReferenceError_,
                                 large_values, references)
from traytrim.resolve import LocatedRef, Where
from traytrim.tray import RefKind, TrayRef

Progress = Callable[[int, int], None]  # resources handled, resources known so far


@dataclass
class PlannedResource:
    key: ResourceKey
    copy: ModsHit                    # the copy that goes into the export
    copies: list[ModsHit]            # every Mods copy, in load order
    via: str                         # "tray", "tgi", "scan" or "thumbnail"
    owner: TrayRef                   # the tray item that first needed it
    parents: set[ResourceKey] = field(default_factory=set)
    conflict: bool = False           # copies with different content
    chosen: bool = False             # the user picked `copy` instead of the one loading last
    default_replacement: bool = False  # EA resource replaced by a mod

    @property
    def size(self) -> int:
        """Bytes it adds to the export (stored size, as copied)."""
        return self.copy.entry.file_size


@dataclass
class Plan:
    refs: list[LocatedRef]
    excluded: set[TrayRef]
    resources: dict[ResourceKey, PlannedResource] = field(default_factory=dict)
    from_game: dict[ResourceKey, set[str]] = field(default_factory=dict)  # dependency -> packs
    overridden: dict[ResourceKey, ModsHit] = field(default_factory=dict)  # EA dep a mod replaces
    missing: dict[ResourceKey, set[ResourceKey]] = field(default_factory=dict)  # dep -> needers
    aliases: dict[ResourceKey, ResourceKey] = field(default_factory=dict)  # texture -> stored as
    whole_size: int = 0  # bytes of every package holding a copy (see packages_with_copies)
    unreadable: list[tuple[ResourceKey, Path, str]] = field(default_factory=list)

    @property
    def export_size(self) -> int:
        return sum(r.size for r in self.resources.values())

    @property
    def source_packages(self) -> set[Path]:
        """Packages the exported resources are taken from."""
        return {r.copy.path for r in self.resources.values()}

    @property
    def packages_with_copies(self) -> set[Path]:
        """Every package holding any copy of a needed resource: what copying whole packages
        (the way tray importers export) would take."""
        return {h.path for r in self.resources.values() for h in r.copies}

    def whole_packages_size(self) -> int:
        """Stats every package; build_plan stores the result in whole_size."""
        total = 0
        for path in self.packages_with_copies:
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    @property
    def conflicts(self) -> list[PlannedResource]:
        return [r for r in self.resources.values() if r.conflict]

    @property
    def default_replacements(self) -> list[PlannedResource]:
        return [r for r in self.resources.values() if r.default_replacement]

    def owned_by(self, ref: TrayRef) -> list[PlannedResource]:
        return [r for r in self.resources.values() if r.owner == ref]


class _Packages:
    """Open packages, a few at a time, plus each package's keys by instance."""

    def __init__(self, limit: int = 64):
        self._open: OrderedDict[Path, dbpf.Package] = OrderedDict()
        self._keys: dict[Path, dict[int, list[ResourceKey]]] = {}
        self._limit = limit

    def get(self, path: Path) -> dbpf.Package:
        pkg = self._open.pop(path, None)
        if pkg is None:
            pkg = dbpf.Package(path)
            if path not in self._keys:
                by_instance: dict[int, list[ResourceKey]] = {}
                for e in pkg:
                    by_instance.setdefault(e.key.instance, []).append(e.key)
                self._keys[path] = by_instance
        self._open[path] = pkg
        while len(self._open) > self._limit:
            _path, old = self._open.popitem(last=False)
            old.close()
        return pkg

    def keys(self, path: Path) -> dict[int, list[ResourceKey]]:
        self.get(path)
        return self._keys[path]

    def read(self, hit: ModsHit) -> bytes:
        return self.get(hit.path).read(hit.entry)

    def read_raw(self, hit: ModsHit) -> bytes:
        return self.get(hit.path).read_raw(hit.entry)

    def close(self) -> None:
        for pkg in self._open.values():
            pkg.close()
        self._open.clear()


class Cancelled(Exception):
    pass


@contextmanager
def open_packages() -> Iterator[_Packages]:
    """Package handles shared across many reads, closed at the end."""
    packages = _Packages()
    try:
        yield packages
    finally:
        packages.close()


def build_plan(refs: list[LocatedRef], index: ResourceIndex,
               excluded: set[TrayRef] | None = None, progress: Progress | None = None,
               cancel: threading.Event | None = None,
               copies_chosen: dict[ResourceKey, Path] | None = None) -> Plan:
    """What an export holds. Of several copies of a resource the one the game loads last is
    taken, unless copies_chosen names another package for that resource."""
    excluded = set(excluded or ())
    copies_chosen = copies_chosen or {}
    plan = Plan(refs, excluded)
    packages = _Packages()
    queue: deque[tuple[ResourceKey, TrayRef, ResourceKey | None, str]] = deque()
    for ref in refs:
        if ref.where is not Where.CC or ref.ref in excluded:
            continue
        for key in ref.keys:
            queue.append((key, ref.ref, None, "tray"))
    done = 0
    try:
        while queue:
            if cancel is not None and cancel.is_set():
                raise Cancelled
            key, owner, parent, via = queue.popleft()
            done += 1
            if progress and done % 25 == 0:
                progress(done, done + len(queue))
            existing = plan.resources.get(key)
            if existing is not None:
                if parent is not None:
                    existing.parents.add(parent)
                continue
            alias = plan.aliases.get(key)
            if alias is not None:
                queue.append((alias, owner, parent, "texture"))
                continue
            if key in plan.from_game or key in plan.missing:
                if key in plan.missing and parent is not None:
                    plan.missing[key].add(parent)
                continue
            copies = index.mods_hits(key)
            game = index.game_hits(key)
            if not copies and not game and key.type in restypes.TEXTURE_TYPES:
                alternative = _texture_by_instance(index, key)
                if alternative is not None:
                    plan.aliases[key] = alternative
                    queue.append((alternative, owner, parent, "texture"))
                    continue
            if not copies:
                if game:
                    plan.from_game[key] = {h.pack for h in game}
                else:
                    plan.missing.setdefault(key, set()).update({parent} if parent else set())
                continue
            if game and via != "tray":
                plan.from_game[key] = {h.pack for h in game}
                plan.overridden[key] = copies[-1]
                continue
            chosen = next((h for h in copies if h.path == copies_chosen.get(key)), None)
            item = PlannedResource(key, chosen or copies[-1], copies, via, owner,
                                   {parent} if parent else set(),
                                   default_replacement=bool(game),
                                   chosen=chosen is not None and chosen is not copies[-1])
            if len(copies) > 1:
                item.conflict = _contents_differ(copies, packages, plan)
            plan.resources[key] = item

            if key.type in LEAF_TYPES:
                continue
            try:
                data = packages.read(item.copy)
                found = references(key, data, packages.keys(item.copy.path))
            except (dbpf.DbpfError, ReferenceError_, OSError) as e:
                plan.unreadable.append((key, item.copy.path, str(e)))
                continue
            for r in found:
                queue.append((r.key, owner, key, r.how))
            if key.type in CROSS_SCAN_TYPES:
                own = packages.keys(item.copy.path)
                for k in index.mods_keys_for_instances(large_values(data), ASSET_TYPES):
                    if k not in own.get(k.instance, ()):
                        queue.append((k, owner, key, "scan"))
            if key.type == restypes.CASP:
                # CAS shows the part's thumbnail; it's in the package but not in the key list.
                for k in packages.keys(item.copy.path).get(key.instance, []):
                    if k.type == restypes.THUM:
                        queue.append((k, owner, key, "thumbnail"))
            elif via == "tray" and owner.kind is RefKind.BUILD:
                # Build mode shows a wall's or floor's thumbnail, stored under its instance.
                for k in packages.keys(item.copy.path).get(key.instance, []):
                    if k.type == restypes.OBJECT_THUMB:
                        queue.append((k, owner, key, "thumbnail"))
            elif key.type == restypes.OBJECT_DEFINITION and via == "tray":
                # An object is its definition plus everything with the same id in its package:
                # catalog entry, thumbnails, its sim data.
                for k in packages.keys(item.copy.path).get(key.instance, []):
                    if k != key:
                        queue.append((k, owner, key, "object"))
    finally:
        packages.close()
    plan.whole_size = plan.whole_packages_size()
    if progress:
        progress(done, done)
    return plan


def _texture_by_instance(index: ResourceIndex, key: ResourceKey) -> ResourceKey | None:
    """The texture stored under this instance in another format, preferring the same group."""
    candidates = [k for k in index.keys_with_instance(key.instance)
                  if k.type in restypes.TEXTURE_TYPES]
    if not candidates:
        return None
    return min(candidates, key=lambda k: (k.group != key.group, k.type, k.group))


def _contents_differ(copies: list[ModsHit], packages: _Packages, plan: Plan) -> bool:
    if len({h.entry.mem_size for h in copies}) > 1:
        return True  # different sizes: different content, no need to read anything
    digests = set()
    for hit in copies:
        try:
            digests.add(hashlib.sha1(packages.read(hit)).digest())
        except (dbpf.DbpfError, OSError) as e:
            plan.unreadable.append((hit.entry.key, hit.path, str(e)))
        if len(digests) > 1:
            return True
    return False
