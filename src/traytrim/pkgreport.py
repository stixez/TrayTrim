"""One Mods package, for the Mods tab: the CC items in it, which of its files the game actually
loads, what other CC breaks without it, and which tray items use it.

Both "what breaks" and "uses" follow references backwards (depindex.py), through any number of
packages, and only through the copies the game loads: a copy another package replaces breaks
nothing. Only the Tray is looked at: Sims and builds that live only in save games aren't, and the
report says so.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from traytrim import dbpf, restypes, tray
from traytrim.dbpf import ResourceKey
from traytrim.depindex import DependencyIndex, Need
from traytrim.index import ModsHit, ResourceIndex
from traytrim.plan import Cancelled
from traytrim.resolve import LocatedRef, Where, locate
from traytrim.tray import RefKind, TrayItem, TrayRef

# The resources that are CC items of their own, the way the game's catalogs and CAS list them.
ITEM_KINDS: dict[int, RefKind] = {
    restypes.CASP: RefKind.CAS_PART,
    restypes.OBJECT_DEFINITION: RefKind.OBJECT,
    restypes.SIM_MODIFIER: RefKind.SIM_MODIFIER,
    restypes.SCULPT: RefKind.SCULPT,
    restypes.SKIN_TONE: RefKind.SKIN_TONE,
    **{t: RefKind.BUILD for t in restypes.BUILD_NAMES},
}
ITEM_TYPES = set(ITEM_KINDS)
_MAX_STEPS = 8  # how far back references are followed; real chains are 2 or 3 long


def item_ref(key: ResourceKey, index: ResourceIndex, mods: list[ModsHit] | None = None,
             in_game: bool | None = None) -> LocatedRef:
    """A CC item found in a package, shaped like a located tray reference so the views and the
    export plan treat it like one. mods and in_game: what's already been looked up."""
    kind = ITEM_KINDS[key.type]
    ref = (TrayRef(kind, key.instance, type_id=key.type, res_group=key.group)
           if kind is RefKind.BUILD else TrayRef(kind, key.instance))
    return LocatedRef(ref, keys=[key], mods=index.mods_hits(key) if mods is None else mods,
                      game=index.game_hits(key) if in_game is not False else [])


@dataclass
class PackageReport:
    path: Path
    rel: str
    size: int
    files: int
    items: list[LocatedRef]
    loaded: list[ResourceKey]                 # files the game loads from this package
    shadowed: list[tuple[ResourceKey, ModsHit]]  # files a later package's copy replaces
    replaces_game: list[ResourceKey]          # loaded files that replace the game's own


def package_report(path: Path, index: ResourceIndex) -> PackageReport:
    rel = index.rel_of(path) or path.name
    with dbpf.Package(path) as pkg:
        keys = list(dict.fromkeys(e.key for e in pkg))  # an index can list a key twice
        size = pkg.size
    loaded, shadowed, replaces_game, items = [], [], [], []
    all_hits = index.mods_hits_many(keys)
    in_game = index.game_keys(keys)
    for key in keys:
        hits = all_hits.get(key, [])
        if hits and hits[-1].path != path:
            shadowed.append((key, hits[-1]))
            continue
        loaded.append(key)
        if key in in_game:
            replaces_game.append(key)
        if key.type in ITEM_KINDS:
            items.append(item_ref(key, index, hits, key in in_game))
    return PackageReport(path, rel, size, len(keys), items, loaded, shadowed, replaces_game)


@dataclass
class Dependent:
    """Another package with CC that refers to files only this package has, or (`through`
    set) to files of a package that does: a recolor using a mesh whose textures are here."""
    path: Path
    rel: str
    needs: list[Need] = field(default_factory=list)
    through: str = ""  # the package in between, for one that needs this package indirectly


def dependents(report: PackageReport, deps: DependencyIndex, index: ResourceIndex,
               via_textures: list[Need] | None = None) -> list[Dependent]:
    """What breaks without the package: other packages' resources referring to files of it
    that no other package and not the game has, and what uses those, through any number of
    packages. Only the copies the game loads count: a copy another package replaces breaks
    nothing. via_textures: texture_needs(), if known."""
    if via_textures is None:
        via_textures = texture_needs(report, deps, index)

    def loaded(needs: list[Need]) -> list[Need]:
        """The needs whose referring resource is the copy the game loads."""
        hits = index.mods_hits_many(list({n.by for n in needs}))
        return [n for n in needs if hits.get(n.by) and hits[n.by][-1].path == n.path]

    needs = loaded(deps.needing(report.loaded, report.path) + via_textures)
    keys = list({n.key for n in needs})
    hits = index.mods_hits_many(keys)
    in_game = index.game_keys(keys)
    only_here = {k for k in keys if k not in in_game
                 and not any(h.path != report.path for h in hits.get(k, []))}
    out: dict[Path, Dependent] = {}
    broken: dict[ResourceKey, Path] = {}  # loaded resources that break, and their package
    for need in needs:
        if need.key not in only_here:
            continue
        dep = out.get(need.path)
        if dep is None:
            dep = out[need.path] = Dependent(need.path, index.rel_of(need.path) or need.path.name)
        dep.needs.append(need)
        broken.setdefault(need.by, need.path)
    # What uses a broken resource breaks with it, through any number of packages.
    frontier = set(broken)
    for _ in range(_MAX_STEPS):
        if not frontier:
            break
        found = loaded(deps.needing(frontier, report.path))
        frontier = set()
        for need in found:
            if need.by not in broken:
                broken[need.by] = need.path
                frontier.add(need.by)
            if need.path not in out:
                via = out.get(broken[need.key])
                out[need.path] = Dependent(need.path, index.rel_of(need.path) or need.path.name,
                                           through=via.rel if via else "")
            if out[need.path].through and need not in out[need.path].needs:
                out[need.path].needs.append(need)
    return sorted(out.values(), key=lambda d: (bool(d.through), d.rel.casefold()))


def texture_needs(report: PackageReport, deps: DependencyIndex,
                  index: ResourceIndex) -> list[Need]:
    """References that reach this package's textures without naming them: a reference to a
    texture nobody has is resolved to the texture stored under the same instance in another
    format or group, as the export plan does. Each comes back as a need of our texture."""
    ours = {k.instance: k for k in report.loaded if k.type in restypes.TEXTURE_TYPES}
    if not ours:
        return []
    own = set(report.loaded)
    raw = [n for n in deps.needing_texture_instances(ours, report.path) if n.key not in own]
    named = list({n.key for n in raw})
    exists = set(index.mods_hits_many(named)) | index.game_keys(named)
    return [Need(ours[n.key.instance], n.path, n.by) for n in raw if n.key not in exists]


@dataclass
class TrayUsage:
    """Which tray items use which resources directly, for every item in the Tray."""
    items: dict[int, TrayItem] = field(default_factory=dict)
    by_key: dict[ResourceKey, set[int]] = field(default_factory=dict)
    unreadable: list[str] = field(default_factory=list)  # tray items (names) that failed
    tray_read: bool = True  # False: there was no Tray to read, so nothing is known to use it


def scan_usage(items: list[TrayItem], index: ResourceIndex,
               progress: Callable[[int, int], None] | None = None,
               cancel: threading.Event | None = None) -> TrayUsage:
    usage = TrayUsage()
    for done, item in enumerate(items, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        try:
            refs = locate(tray.read_contents(item), index)
        except (tray.TrayError, OSError, ValueError):
            usage.unreadable.append(item.name or f"Unnamed {item.kind}")
        else:
            usage.items[item.instance] = item
            for r in refs:
                if r.where is Where.CC:
                    for key in usage_keys(r, index):
                        usage.by_key.setdefault(key, set()).add(item.instance)
        if progress:  # every item, read or not, so the bar gets to the end
            progress(done, len(items))
    return usage


def usage_keys(ref: LocatedRef, index: ResourceIndex) -> set[ResourceKey]:
    """The resources a tray reference uses directly: its own keys, and everything stored under
    its instance. The export plan takes those along without a reference to follow (an object's
    catalog entry and thumbnails, a CAS part's thumbnail, a build item's), so a package they
    lead to is used too."""
    return set(ref.keys) | set(index.keys_with_instance(ref.ref.instance))


def users(report: PackageReport, deps: DependencyIndex, usage: TrayUsage,
          index: ResourceIndex, via_textures: list[Need] | None = None) -> list[TrayItem]:
    """Tray items that use the package, in Tray order. References are followed backwards from
    its files; the package's own references to itself lead nowhere new and are skipped."""
    if via_textures is None:
        via_textures = texture_needs(report, deps, index)
    seen = set(report.loaded) | {n.by for n in via_textures}
    frontier = set(seen)
    for _ in range(_MAX_STEPS):
        if not frontier:
            break
        frontier = {n.by for n in deps.needing(frontier, report.path)} - seen
        seen |= frontier
    found: set[int] = set()
    for key in seen:
        found |= usage.by_key.get(key, set())
    return [item for instance, item in usage.items.items() if instance in found]
