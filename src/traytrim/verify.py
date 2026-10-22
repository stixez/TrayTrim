"""Checks run on a finished export, before it's called done.

1. The package opens with our own reader and holds exactly the planned resources.
2. Every resource decompresses to exactly the same bytes as the copy it was taken from.
3. Everything the exported CC refers to is in the package or in the game (files that were
   already missing from the Mods folder are listed, they can't be exported).
4. Every CC item the Sims use, and that wasn't left out, is in the package.

check_zip re-checks an export later from the zip alone (see there).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from traytrim import dbpf, restypes
from traytrim.dbpf import ResourceKey
from traytrim.errors import describe_error
from traytrim.index import ResourceIndex
from traytrim.plan import Cancelled, Plan, open_packages
from traytrim.references import LEAF_TYPES, ReferenceError_, references
from traytrim.resolve import LocatedRef, Where

Progress = Callable[[int, int], None]


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    problems: list[str] = field(default_factory=list)


@dataclass
class Verification:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)


def _plural(n: int, one: str) -> str:
    return f"{n:,} {one}{'' if n == 1 else 's'}"


def verify_export(package_path: Path, plan: Plan, refs: list[LocatedRef], index: ResourceIndex,
                  progress: Progress | None = None,
                  cancel: threading.Event | None = None) -> Verification:
    checks: list[Check] = []
    try:
        pkg = dbpf.Package(package_path)
    except (dbpf.DbpfError, OSError) as e:
        checks.append(Check("The package opens", False, f"It can't be read: {e}"))
        return Verification(checks)

    with pkg, open_packages() as sources:
        new_keys = {e.key for e in pkg}
        planned = set(plan.resources)
        missing_from_pkg = planned - new_keys
        extra = new_keys - planned
        checks.append(Check(
            "The package opens and holds the planned files",
            not missing_from_pkg and not extra and len(pkg) == len(new_keys),
            f"{_plural(len(new_keys), 'file')}, read back with TrayTrim's own reader.",
            [f"Not in the package: {k}" for k in sorted(missing_from_pkg)]
            + [f"Not planned but in the package: {k}" for k in sorted(extra)]))

        mismatched: list[str] = []
        by_instance: dict[int, list[ResourceKey]] = {}
        for e in pkg:
            by_instance.setdefault(e.key.instance, []).append(e.key)
        dangling: list[str] = []
        known_missing: set[ResourceKey] = set(plan.missing)
        entries = list(pkg)
        for done, entry in enumerate(entries, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled
            if progress and done % 20 == 0:
                progress(done, len(entries))
            planned_res = plan.resources.get(entry.key)
            if planned_res is None:
                continue
            try:
                data = pkg.read(entry)
                # Compare with the source package as it is now, through its own index, not
                # through the cached position the export copied from.
                source_pkg = sources.get(planned_res.copy.path)
                current = source_pkg.find(entry.key)
                if current is None:
                    mismatched.append(f"{entry.key}: no longer in {planned_res.copy.rel}")
                    continue
                source = source_pkg.read(current)
            except (dbpf.DbpfError, OSError) as e:
                mismatched.append(f"{entry.key}: couldn't be read ({e})")
                continue
            if hashlib.sha256(data).digest() != hashlib.sha256(source).digest():
                mismatched.append(f"{entry.key}: differs from {planned_res.copy.rel}")
                continue
            if entry.key.type in LEAF_TYPES:
                continue
            try:
                found = references(entry.key, data, by_instance)
            except ReferenceError_ as e:
                dangling.append(f"{entry.key}: its references can't be read ({e})")
                continue
            for ref in found:
                key = plan.aliases.get(ref.key, ref.key)
                if key in new_keys or key in known_missing or key in plan.from_game:
                    continue
                if index.game_hits(key):
                    continue
                dangling.append(f"{restypes.name(key.type)} {key}, needed by {entry.key}")
        if progress:
            progress(len(entries), len(entries))

        checks.append(Check(
            "Every file matches its source", not mismatched,
            "Each file decompresses to exactly the same bytes as the copy it came from."
            if not mismatched else f"{_plural(len(mismatched), 'file')} don't match.",
            mismatched))

        detail = "Every mesh, texture and slider file the CC needs is in the package or the game."
        if known_missing:
            detail += (f" {_plural(len(known_missing), 'file')} that some CC refers to "
                       "{} already missing from your Mods folder and couldn't be exported."
                       .format("was" if len(known_missing) == 1 else "were"))
        checks.append(Check("Everything the CC needs is there", not dangling, detail, dangling))

        left_out = plan.excluded
        absent = [r for r in refs if r.where is Where.CC and r.ref not in left_out
                  and not any(k in new_keys for k in r.keys)]
        n_cc = sum(1 for r in refs if r.where is Where.CC and r.ref not in left_out)
        checks.append(Check(
            "Every CC item in the tray files is included", not absent,
            f"{_plural(n_cc, 'item')} checked against the tray files.",
            [f"{r.ref.kind.value} {r.ref.instance:016X}" for r in absent]))
    return Verification(checks)


# progress(step, done, total) for check_zip: step "read" counts bytes of the package copied
# out of the zip, "files" counts resources decompressed.
CheckProgress = Callable[[str, int, int], None]
_ZIP_ERRORS = (OSError, EOFError, zipfile.BadZipFile, zlib.error)


def check_zip(zip_path: Path, progress: CheckProgress | None = None,
              cancel: threading.Event | None = None) -> Verification:
    """Check an earlier export on its own, without the Mods folder it came from (which may have
    changed since): the zip is intact, its package reads back, and both still match the
    report.json written with it. The package is copied out of the zip once (its checksum is
    checked on the way) into a temporary folder, then read from there."""
    try:
        z = zipfile.ZipFile(zip_path)
    except _ZIP_ERRORS as e:
        return Verification([Check("The zip opens", False,
                                   f"It can't be read: {describe_error(e)}.")])
    with z, tempfile.TemporaryDirectory(prefix="traytrim-check-",
                                        ignore_cleanup_errors=True) as tmp:
        names = z.namelist()
        packages = [n for n in names if n.startswith("Mods/") and n.endswith(".package")]
        if len(packages) != 1 or "report.json" not in names:
            return Verification([Check(
                "It's a TrayTrim export", False,
                "It doesn't hold one package and a report.json, like a TrayTrim export.")])

        damaged: list[str] = []
        for name in names:  # the small files; the package is checked while it's copied
            if name == packages[0]:
                continue
            try:
                with z.open(name) as f:
                    while f.read(1 << 20):
                        pass
            except _ZIP_ERRORS as e:
                damaged.append(f"{name}: {describe_error(e)}")
        package = Path(tmp) / "export.package"
        info = z.getinfo(packages[0])
        try:
            with z.open(info) as src, open(package, "wb") as dst:
                copied = 0
                while chunk := src.read(4 << 20):
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    dst.write(chunk)
                    copied += len(chunk)
                    if progress:
                        progress("read", copied, info.file_size)
        except _ZIP_ERRORS as e:
            damaged.append(f"{packages[0]}: {describe_error(e)}")
        checks = [Check(
            "The zip is intact", not damaged,
            "Every file in it reads back with the checksum it was saved with." if not damaged
            else f"{_plural(len(damaged), 'file')} in it {'is' if len(damaged) == 1 else 'are'}"
            " damaged.", damaged)]
        if damaged:
            return Verification(checks)

        problems: list[str] = []
        try:
            pkg = dbpf.Package(package)
        except (dbpf.DbpfError, OSError) as e:
            checks.append(Check("The package reads back", False,
                                f"It can't be opened: {describe_error(e)}"))
            return Verification(checks)
        with pkg:
            entries = list(pkg)
            for done, entry in enumerate(entries, 1):
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                try:
                    pkg.read(entry)
                except dbpf.DbpfError as e:
                    problems.append(f"{entry.key}: {e}")
                if progress and (done % 50 == 0 or done == len(entries)):
                    progress("files", done, len(entries))
        checks.append(Check(
            "The package reads back", not problems,
            f"{_plural(len(entries), 'file')}, each decompresses." if not problems
            else f"{_plural(len(problems), 'file')} can't be decompressed.", problems))

        try:
            report = json.loads(z.read("report.json"))
            listed = {f["key"]: f["bytes"] for f in report["files"]}
        except (ValueError, KeyError, TypeError) as e:
            checks.append(Check("It matches its report", False,
                                f"report.json can't be read ({e})."))
            return Verification(checks)
        found = {str(e.key): e.file_size for e in entries}
        differ = ([f"{k}: missing from the package" for k in sorted(listed.keys() - found)]
                  + [f"{k}: not in the report" for k in sorted(found.keys() - listed)]
                  + [f"{k}: {found[k]:,} bytes, the report says {listed[k]:,}"
                     for k in sorted(found.keys() & listed.keys()) if found[k] != listed[k]])
        checks.append(Check(
            "It matches its report", not differ,
            f"The same {_plural(len(listed), 'file')}, at the same sizes, as when it was "
            "exported." if not differ else f"{_plural(len(differ), 'difference')} from "
            "report.json.", differ))
    return Verification(checks)
