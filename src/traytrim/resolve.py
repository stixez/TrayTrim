"""From tray references to resources.

locate() finds where each reference from a household's tray data lives: in Mods (CC), in the
game, or nowhere. plan.py then follows each CC resource's own references (meshes, textures,
thumbnails...).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

from traytrim import restypes
from traytrim.dbpf import ResourceKey
from traytrim.index import GameHit, ModsHit, ResourceIndex
from traytrim.tray import HouseholdContents, RefKind, TrayRef

# The resource type each kind of tray reference points to. None: not verified, so any resource
# with the instance is accepted and the result is marked approximate.
EXPECTED_TYPE: dict[RefKind, int | None] = {
    RefKind.CAS_PART: restypes.CASP,
    RefKind.SCULPT: restypes.SCULPT,
    RefKind.SIM_MODIFIER: restypes.SIM_MODIFIER,
    RefKind.SKIN_TONE: restypes.SKIN_TONE,
    RefKind.PELT_LAYER: None,
    RefKind.CUSTOM_TATTOO: None,
    RefKind.OBJECT: restypes.OBJECT_DEFINITION,
    RefKind.BUILD: None,  # exact key from the tray file; see locate()
}


class Where(str, Enum):
    CC = "cc"            # in Mods (if it's in the game too, the Mods copy overrides it)
    GAME = "game"        # EA content, never copied
    MISSING = "missing"  # neither: the Sim will look different without it


@dataclass
class LocatedRef:
    ref: TrayRef
    usage: dict[int, set[str]] = field(default_factory=dict)  # Sim id -> where it's used
    names: dict[int, str] = field(default_factory=dict)  # Sim id -> name to show
    keys: list[ResourceKey] = field(default_factory=list)
    mods: list[ModsHit] = field(default_factory=list)   # in load order: the last one is used
    game: list[GameHit] = field(default_factory=list)
    approximate: bool = False  # matched by instance only, resource type not verified

    @property
    def where(self) -> Where:
        if self.mods:
            return Where.CC
        return Where.GAME if self.game else Where.MISSING

    @property
    def used(self) -> ModsHit | None:
        """The Mods copy the game loads, if any."""
        return self.mods[-1] if self.mods else None

    @property
    def packs(self) -> set[str]:
        return {h.pack for h in self.game}

    def usage_by_name(self) -> list[tuple[str, set[str]]]:
        return [(self.names.get(sim_id, "Unnamed Sim"), uses)
                for sim_id, uses in self.usage.items()]


def sim_display_names(contents: HouseholdContents) -> dict[int, str]:
    """A name per Sim, made unique ("Alex Smith (2)") so two Sims never read as one."""
    names: dict[int, str] = {}
    seen: dict[str, int] = {}
    for sim in contents.sims:
        base = sim.sim.name or "Unnamed Sim"
        seen[base] = seen.get(base, 0) + 1
        names[sim.sim.sim_id] = base if seen[base] == 1 else f"{base} ({seen[base]})"
    return names


def locate(contents: HouseholdContents, index: ResourceIndex,
           sim_ids: set[int] | None = None) -> list[LocatedRef]:
    """Every distinct reference of the household's Sims (or just `sim_ids`), located."""
    names = sim_display_names(contents)
    merged: dict[TrayRef, LocatedRef] = {}
    for sim in contents.sims:
        if sim_ids is not None and sim.sim.sim_id not in sim_ids:
            continue
        for ref, where in sim.refs.items():
            # The same part can be listed with and without a body type; merge on kind+instance
            # (and the exact type for build items, whose instances can repeat across types).
            key = TrayRef(ref.kind, ref.instance, type_id=ref.type_id, res_group=ref.res_group)
            located = merged.get(key)
            if located is None:
                located = merged[key] = LocatedRef(ref, names=names)
            elif located.ref.body_type is None and ref.body_type is not None:
                located.ref = ref
            located.usage.setdefault(sim.sim.sim_id, set()).update(where)

    for located in merged.values():
        if located.ref.kind is RefKind.BUILD:
            located.keys = [ResourceKey(located.ref.type_id, located.ref.res_group or 0,
                                        located.ref.instance)]
            located.approximate = False
        else:
            expected = EXPECTED_TYPE[located.ref.kind]
            located.approximate = expected is None
            located.keys = index.keys_with_instance(located.ref.instance, expected)
        for key in located.keys:
            located.mods += index.mods_hits(key)
            located.game += index.game_hits(key)
        located.mods.sort(key=lambda h: h.load_order)
    return sorted(merged.values(), key=lambda r: (r.ref.group, r.ref.kind.value, r.ref.instance))


def default_excluded(contents: HouseholdContents, refs: list[LocatedRef]) -> set[TrayRef]:
    """What's left out until the user includes it. On lots and rooms that's game objects a mod
    replaces: mostly unlock and tuning mods that don't change how the build looks, and that
    would change the object for everyone who installs the export. Sims keep theirs, because
    there a replaced part (teeth, a head) is part of how the Sim looks."""
    if contents.kind == "household":
        return set()
    return {r.ref for r in refs if r.where is Where.CC and r.game}


def for_sims(refs: list[LocatedRef], sim_ids: set[int]) -> list[LocatedRef]:
    """The references only these Sims use, without looking anything up again."""
    out = []
    for r in refs:
        usage = {sim: uses for sim, uses in r.usage.items() if sim in sim_ids}
        if usage:
            out.append(replace(r, usage=usage))
    return out


@dataclass
class Summary:
    cc: list[LocatedRef]
    game: list[LocatedRef]
    missing: list[LocatedRef]

    @property
    def packages(self) -> set[str]:
        """Mods packages the CC comes from (the copies the game uses)."""
        return {r.used.rel for r in self.cc if r.used}

    @property
    def packs(self) -> list[str]:
        """Game packs the EA content comes from, base game last."""
        packs = {p for r in self.game for p in r.packs}
        return sorted(packs - {"base"}) + (["base"] if "base" in packs else [])


def summarize(refs: list[LocatedRef]) -> Summary:
    return Summary([r for r in refs if r.where is Where.CC],
                   [r for r in refs if r.where is Where.GAME],
                   [r for r in refs if r.where is Where.MISSING])


# Resource types that are CAS content; anything else a Sim's gameplay data points to in Mods is
# gameplay content (tuning, sim data): traits, moods, careers, aspirations...
_CAS_TYPES = ({restypes.CASP, restypes.GEOM, restypes.THUM, restypes.SIM_MODIFIER,
               restypes.SCULPT, restypes.SKIN_TONE, restypes.BONE_DELTA,
               restypes.BLEND_GEOMETRY, restypes.DEFORMER_MAP, restypes.REGION_MAP,
               restypes.CAS_PRESET} | restypes.TEXTURE_TYPES)


@dataclass
class GameplayMod:
    rel: str            # package path inside Mods
    ids: int            # how many of the Sims' gameplay ids it provides


def gameplay_mods(contents: HouseholdContents, index: ResourceIndex,
                  sim_ids: set[int] | None = None) -> list[GameplayMod]:
    """Mods packages holding gameplay content the Sims use that the game itself doesn't have.
    They can't be trimmed (they're tuning and scripts): the recipient needs them installed."""
    ids: set[int] = set()
    for sim in contents.sims:
        if sim_ids is None or sim.sim.sim_id in sim_ids:
            ids |= sim.gameplay_ids
    counts: dict[str, int] = {}
    for instance in ids:
        keys = index.keys_with_instance(instance)
        if not keys or any(index.game_hits(k) for k in keys):
            continue
        packages = {h.rel for k in keys if k.type not in _CAS_TYPES for h in index.mods_hits(k)}
        for rel in packages:
            counts[rel] = counts.get(rel, 0) + 1
    return sorted((GameplayMod(rel, n) for rel, n in counts.items()),
                  key=lambda m: m.rel.casefold())
