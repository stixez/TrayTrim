"""Exporting tray items without the app's detail view: prepare one (read, locate, plan) with the
same defaults the app starts from, or export several in a row, one zip each."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Callable

from traytrim import tray
from traytrim.export import ExportOptions, ExportResult, default_base_name, export_household
from traytrim.index import ResourceIndex
from traytrim.plan import Cancelled, Plan, build_plan
from traytrim.resolve import (GameplayMod, LocatedRef, default_excluded, for_sims,
                              gameplay_mods, locate)
from traytrim.tray import HouseholdContents, TrayItem, TrayRef


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


def prepare(item: TrayItem, index: ResourceIndex, choice: Choice | None = None,
            progress: Callable[[int, int], None] | None = None,
            cancel: threading.Event | None = None) -> Prepared:
    """Everything an export of `item` needs. Defaults: every Sim of a household, and for lots
    and rooms the EA objects changed by mods left out (see resolve.default_excluded)."""
    choice = choice or Choice()
    contents = tray.read_contents(item)
    all_ids = {s.sim.sim_id for s in contents.sims}
    sim_ids = (choice.sim_ids & all_ids if choice.sim_ids else set()) or all_ids
    refs = locate(contents, index)
    if sim_ids != all_ids:
        refs = for_sims(refs, sim_ids)
    excluded = (choice.excluded if choice.excluded is not None
                else default_excluded(contents, refs))
    plan = build_plan(refs, index, excluded, progress, cancel)
    gameplay = gameplay_mods(contents, index, sim_ids)
    return Prepared(item, contents, refs, plan, gameplay, sim_ids,
                    default_base_name(item, contents, sim_ids))


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
                cancel: threading.Event | None = None) -> list[Outcome]:
    """Export each item to its own zip in options.out_dir, named after the item. One that fails
    doesn't stop the others; a cancel stops after nothing more is saved (the item in progress
    leaves nothing behind) and returns the outcomes so far."""
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
            outcome.error = _reason(e)
        outcomes.append(outcome)
        report(position, "done", 1, 1)
    return outcomes


def _reason(error: BaseException) -> str:
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__
