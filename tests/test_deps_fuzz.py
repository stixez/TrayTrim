"""Random Mods histories for the dependency index and the Mods tab's answers.

Packages are added, changed and deleted at random, and each update may be cancelled at a random
point (later updates finish it). After every step:
- the references kept step by step must match a build from scratch (cross-package references
  to keys no package has any more are left out: only a fresh build drops them, nothing reads
  them);
- for every file a CC item's export plan takes from a package P that no other package has,
  P's report must say so: the package on the plan's way to it (and the item's own package,
  unless a copy elsewhere takes over on the way) in "what breaks", the item in "used by".
"""

import os
import random
import struct
import threading

import pytest

from test_plan import casp_bytes, geom_bytes
from traytrim import restypes
from traytrim.dbpf import PackageWriter, ResourceKey
from traytrim.depindex import DependencyIndex
from traytrim.index import ResourceIndex
from traytrim.pkgreport import TrayUsage, dependents, package_report, usage_keys, users
from traytrim.plan import Cancelled, build_plan
from traytrim.tray import TrayItem

CC = 0x80000000


class World:
    """A Mods folder whose packages share a small pool of instances, so CC refers across them."""

    def __init__(self, mods, rng: random.Random):
        self.mods, self.rng = mods, rng
        mods.mkdir(parents=True)
        self.instances = [rng.getrandbits(62) | (1 << 61) for _ in range(14)]
        self.versions: dict[str, int] = {}

    def key(self, t, group=CC):
        return ResourceKey(t, group, self.rng.choice(self.instances))

    def resources(self) -> dict:
        rng, out = self.rng, {}
        for _ in range(rng.randint(1, 6)):
            kind = rng.choice(["casp", "geom", "rle2", "dst", "model", "objd"])
            if kind == "casp":
                refs = [self.key(restypes.GEOM),
                        self.key(rng.choice([restypes.RLE2, restypes.DST]), rng.choice([CC, 0]))]
                out[self.key(restypes.CASP)] = casp_bytes(refs, filler=rng.randbytes(8))
            elif kind == "geom":
                out[self.key(restypes.GEOM)] = geom_bytes([self.key(restypes.RLE2)])
            elif kind in ("rle2", "dst"):
                t = restypes.RLE2 if kind == "rle2" else restypes.DST
                out[self.key(t, rng.choice([CC, 0]))] = rng.randbytes(16)
            elif kind == "model":
                out[self.key(restypes.MODEL, 0)] = rng.randbytes(16)
            else:
                out[self.key(restypes.OBJECT_DEFINITION, 0)] = (
                    b"objd" + struct.pack("<Q", rng.choice(self.instances)) + rng.randbytes(4))
        return out

    def write(self, name: str) -> None:
        path = self.mods / name
        with PackageWriter(path) as w:
            for key, data in self.resources().items():
                w.add(key, data)
        self.versions[name] = self.versions.get(name, 0) + 1
        st = path.stat()  # a new mtime even within one clock tick
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + self.versions[name] * 10**6))

    def mutate(self) -> None:
        rng = self.rng
        op = rng.choice(["add", "add", "change", "delete"]) if self.versions else "add"
        if op == "add":
            self.write(f"p{rng.randrange(10**6):06d}.package")
        elif op == "change":
            self.write(rng.choice(sorted(self.versions)))
        else:
            name = rng.choice(sorted(self.versions))
            (self.mods / name).unlink()
            del self.versions[name]


def refs_of(deps: DependencyIndex, existing: set) -> set:
    rows = deps.db.execute(
        "SELECT r.to_inst, r.to_type, r.to_grp, p.path, r.from_inst, r.from_type, r.from_grp, "
        "r.cross FROM refs r JOIN packages p ON p.id = r.from_pkg")
    return {r for r in rows if not r[7] or (r[0], r[1], r[2]) in existing}


def cancelling_after(limit: int | None):
    """A cancel event and a progress callback that sets it after `limit` reports (never: None)."""
    cancel, steps = threading.Event(), [0]

    def progress(_phase, _done, _total):
        steps[0] += 1
        if limit is not None and steps[0] > limit:
            cancel.set()

    return cancel, progress


def update_with_cancels(deps: DependencyIndex, index: ResourceIndex, rng: random.Random):
    for _ in range(20):
        cancel, progress = cancelling_after(rng.choice([None, 0, 1, 2, 3]))
        try:
            deps.update(index, progress, cancel)
            return
        except Cancelled:
            continue
    deps.update(index)


def answers_problems(index: ResourceIndex, deps: DependencyIndex) -> list:
    reports = {p.path: package_report(p.path, index) for p in index.mods_packages()}
    usage = TrayUsage()  # every CC item worn by a household of its own
    for report in reports.values():
        for ref in report.items:
            n = len(usage.items) + 1
            usage.items[n] = TrayItem(n, 1, str(ref.keys[0]))
            for key in usage_keys(ref, index):
                usage.by_key.setdefault(key, set()).add(n)
    problems, listed_by, used_by = [], {}, {}
    for q, report in reports.items():
        for ref in report.items:
            plan = build_plan([ref], index)
            for res in plan.resources.values():
                owner = res.copy.path
                if owner == q or len(res.copies) > 1 or res.via == "tray":
                    continue
                if owner not in used_by:
                    used_by[owner] = {i.name for i in users(reports[owner], deps, usage, index)}
                    listed_by[owner] = {d.path for d in dependents(reports[owner], deps, index)}
                if str(ref.keys[0]) not in used_by[owner]:
                    problems.append(("not in used by", owner.name, str(ref.keys[0])))
                # Up the plan from the file to the item: a file of the owner with a copy
                # elsewhere takes over when the owner goes, so nothing breaks through it.
                needed, frontier, seen, reaches_item = set(), {res.key}, set(), False
                while frontier:
                    k = frontier.pop()
                    if k in seen or k not in plan.resources:
                        continue
                    seen.add(k)
                    node = plan.resources[k]
                    if k != res.key and node.copy.path == owner and len(node.copies) > 1:
                        continue
                    reaches_item |= node.via == "tray"
                    for pk in node.parents:
                        parent = plan.resources.get(pk)
                        if parent is not None and node.copy.path == owner != parent.copy.path:
                            needed.add(parent.copy.path)
                        frontier.add(pk)
                if reaches_item and not (needed | {q}) <= listed_by[owner]:
                    problems.append(("not in what breaks", owner.name,
                                     sorted(p.name for p in (needed | {q}) - listed_by[owner])))
    return problems


@pytest.mark.parametrize("seed", range(100))
def test_random_mods_histories(tmp_path, seed):
    rng = random.Random(seed)
    world = World(tmp_path / "Mods", rng)
    with ResourceIndex(tmp_path / "index.sqlite") as index, \
            DependencyIndex(tmp_path / "deps.sqlite") as deps:
        for step in range(15):
            world.mutate()
            index.update(world.mods, None)
            update_with_cancels(deps, index, rng)
            existing = {tuple(r) for r in index.db.execute("SELECT inst, type, grp FROM mods_res")}
            with DependencyIndex(tmp_path / f"fresh{step}.sqlite") as fresh:
                fresh.update(index)
                assert refs_of(deps, existing) == refs_of(fresh, existing), f"step {step}"
            assert answers_problems(index, deps) == [], f"step {step}"
