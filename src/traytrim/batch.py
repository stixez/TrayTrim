"""The steps from a tray item to an export, shared by the app, batch export and the CLI, so
they all start from the same defaults: locate what an item uses, scope it to the chosen Sims,
plan it with what's left out and which copies are used. Plus exporting several in a row, one
zip each."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from traytrim import tray
from traytrim.dbpf import ResourceKey
from traytrim.errors import describe_error
from traytrim.export import (ExportError, ExportOptions, ExportResult, Part, bundle_title,
                             default_base_name, export_household, export_parts, safe_name)
from traytrim.index import ResourceIndex
from traytrim.plan import Cancelled, Plan, build_plan
from traytrim.resolve import (GameplayMod, LocatedRef, default_excluded, for_sims,
                              gameplay_mods, locate)
from traytrim.tray import HouseholdContents, TrayItem, TrayRef


@dataclass
class Located:
    """What a tray item uses, for all of its Sims, before any choice."""
    item: TrayItem
    contents: HouseholdContents
    refs: list[LocatedRef]
    start_excluded: set[TrayRef]  # left out until the user includes them

    @property
    def sim_ids(self) -> set[int]:
        return {s.sim.sim_id for s in self.contents.sims}


def locate_item(item: TrayItem, index: ResourceIndex) -> Located:
    contents = tray.read_contents(item)
    refs = locate(contents, index)
    return Located(item, contents, refs, default_excluded(contents, refs))


def scoped_refs(refs: list[LocatedRef], all_ids: set[int], sim_ids: set[int]
                ) -> list[LocatedRef]:
    """The references of just these Sims (all of them when every Sim is chosen)."""
    return refs if sim_ids >= all_ids else for_sims(refs, sim_ids)


def plan_refs(contents: HouseholdContents, refs: list[LocatedRef], sim_ids: set[int],
              excluded: set[TrayRef], copies: dict[ResourceKey, Path], index: ResourceIndex,
              progress: Callable[[int, int], None] | None = None,
              cancel: threading.Event | None = None) -> tuple[Plan, list[GameplayMod]]:
    plan = build_plan(refs, index, excluded, progress, cancel, copies)
    return plan, gameplay_mods(contents, index, sim_ids)


@dataclass
class Prepared:
    item: TrayItem
    contents: HouseholdContents
    refs: list[LocatedRef]
    plan: Plan
    gameplay: list[GameplayMod]
    sim_ids: set[int]
    base_name: str


@dataclass
class Choice:
    """What the user picked for one item in the app; None means the defaults."""
    sim_ids: set[int] | None = None
    excluded: set[TrayRef] | None = None
    copies: dict[ResourceKey, Path] = field(default_factory=dict)  # resource -> package used


def prepare(item: TrayItem, index: ResourceIndex, choice: Choice | None = None,
            progress: Callable[[int, int], None] | None = None,
            cancel: threading.Event | None = None) -> Prepared:
    """Everything an export of `item` needs. Defaults: every Sim of a household, and for lots
    and rooms the EA objects changed by mods left out (see resolve.default_excluded)."""
    choice = choice or Choice()
    located = locate_item(item, index)
    all_ids = located.sim_ids
    sim_ids = (choice.sim_ids & all_ids if choice.sim_ids else set()) or all_ids
    refs = scoped_refs(located.refs, all_ids, sim_ids)
    excluded = choice.excluded if choice.excluded is not None else located.start_excluded
    plan, gameplay = plan_refs(located.contents, refs, sim_ids, excluded, choice.copies, index,
                               progress, cancel)
    return Prepared(item, located.contents, refs, plan, gameplay, sim_ids,
                    default_base_name(item, located.contents, sim_ids))


def export_prepared(p: Prepared, options: ExportOptions, index: ResourceIndex,
                    progress=None, cancel: threading.Event | None = None) -> ExportResult:
    return export_household(p.item, p.contents, p.refs, p.plan, p.gameplay, p.sim_ids,
                            replace(options, base_name=options.base_name or p.base_name),
                            index, progress, cancel)


@dataclass
class Outcome:
    item: TrayItem
    result: ExportResult | None = None
    skipped: str = ""   # why nothing was exported (no CC), not an error
    error: str = ""


# progress(position, step, done, total). position is the item's index in the list; step is
# "plan", or an export step ("package", "verify", "zip"), or "done" once its outcome is final.
BatchProgress = Callable[[int, str, int, int], None]


def export_many(items: list[TrayItem], options: ExportOptions, index: ResourceIndex,
                choices: dict[int, Choice] | None = None,
                progress: BatchProgress | None = None,
                cancel: threading.Event | None = None,
                on_outcome: Callable[[int, Outcome], None] | None = None) -> list[Outcome]:
    """Export each item to its own zip in options.out_dir, named after the item. One that fails
    doesn't stop the others; a cancel stops after nothing more is saved (the item in progress
    leaves nothing behind) and returns the outcomes so far. on_outcome(position, outcome) is
    called as soon as each one is final, so a caller knows what was saved even if something
    goes wrong later."""
    choices = choices or {}
    outcomes: list[Outcome] = []

    def report(position: int, step: str, done: int, total: int) -> None:
        if progress is not None:
            progress(position, step, done, total)

    for position, item in enumerate(items):
        if cancel is not None and cancel.is_set():
            break
        outcome = Outcome(item)
        try:
            p = prepare(item, index, choices.get(item.instance),
                        lambda d, t, i=position: report(i, "plan", d, t), cancel)
            if not p.plan.resources:
                outcome.skipped = "No CC to export"
            else:
                outcome.result = export_prepared(
                    p, replace(options, base_name=""), index,
                    lambda step, d, t, i=position: report(i, step, d, t), cancel)
        except Cancelled:
            break
        except Exception as e:  # one item's problem is reported, the rest still export
            if cancel is not None and cancel.is_set():
                break
            outcome.error = describe_error(e)
        outcomes.append(outcome)
        if on_outcome is not None:
            on_outcome(position, outcome)
        report(position, "done", 1, 1)
    return outcomes


def export_together(items: list[TrayItem], options: ExportOptions, index: ResourceIndex,
                    choices: dict[int, Choice] | None = None,
                    progress: Callable[[str, int, int], None] | None = None,
                    cancel: threading.Event | None = None) -> ExportResult:
    """Export several tray items into one zip with one package holding the CC of all of them
    (CC they share is in it once): a lot with the household that lives there, say.
    progress(step, done, total): "plan", then the export steps."""
    choices = choices or {}
    parts: list[Part] = []
    merged: dict[TrayRef, LocatedRef] = {}
    kept: set[TrayRef] = set()      # included by at least one item
    every: set[TrayRef] = set()
    copies: dict[ResourceKey, Path] = {}
    for item in items:
        if cancel is not None and cancel.is_set():
            raise Cancelled
        choice = choices.get(item.instance) or Choice()
        located = locate_item(item, index)
        all_ids = located.sim_ids
        sim_ids = (choice.sim_ids & all_ids if choice.sim_ids else set()) or all_ids
        refs = scoped_refs(located.refs, all_ids, sim_ids)
        excluded = choice.excluded if choice.excluded is not None else located.start_excluded
        parts.append(Part(item, located.contents, sim_ids))
        copies.update(choice.copies)
        for r in refs:
            every.add(r.ref)
            if r.ref not in excluded:
                kept.add(r.ref)
            if r.ref in merged:
                both = merged[r.ref]
                merged[r.ref] = replace(both, usage={**both.usage, **r.usage},
                                        names={**both.names, **r.names})
            else:
                merged[r.ref] = r
    # Left out only where every item that uses it leaves it out.
    excluded_all = every - kept
    refs_all = list(merged.values())
    plan = build_plan(refs_all, index, excluded_all,
                      (lambda d, t: progress("plan", d, t)) if progress else None, cancel,
                      copies)
    if not plan.resources:
        raise ExportError("None of them uses CC from your Mods folder (or all of it is left "
                          "out), so there's nothing to export.")
    gameplay: dict[str, GameplayMod] = {}
    for part in parts:
        for m in gameplay_mods(part.contents, index, part.sim_ids):
            gameplay.setdefault(m.rel, m)
    base = options.base_name or safe_name(bundle_title(parts), "TrayTrim")
    return export_parts(parts, refs_all, plan, list(gameplay.values()),
                        replace(options, base_name=base), index, progress, cancel)
