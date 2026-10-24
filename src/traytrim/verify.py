"""Checks run on a finished export, before it's called done.

1. The packages (one per CC item) open with our own reader and hold exactly the planned
   resources between them, each in one package only.
2. Every resource decompresses to exactly the same bytes as the copy it was taken from.
3. Everything the exported CC refers to is in the packages or in the game (files that were
   already missing from the Mods folder are listed, they can't be exported).
4. Every CC item the Sims use, and that wasn't left out, is in the packages.

check_zip re-checks an export later from the zip alone (see there).
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from collections.abc import Callable

from traytrim import dbpf, restypes
from traytrim.dbpf import ResourceKey
from traytrim.errors import describe_error
from traytrim.index import ResourceIndex
from traytrim.plan import Cancelled, Plan, open_packages
from traytrim.references import LEAF_TYPES, ReferenceParseError, references, scan_candidates
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


def verify_export(package_paths: list[Path], plan: Plan, refs: list[LocatedRef],
                  index: ResourceIndex, progress: Progress | None = None,
                  cancel: threading.Event | None = None, from_tray: bool = True) -> Verification:
    """Check the exported packages (one per CC item) together. from_tray: the refs come from
    tray files; else they're CC items exported on their own. Their indexes are read first,
    then one package at a time is open: an export can hold more packages than a process may
    have files open (about 8,000 on Windows)."""
    checks: list[Check] = []
    indexes: list[tuple[Path, list[dbpf.IndexEntry]]] = []
    for path in package_paths:
        try:
            indexes.append((Path(path), dbpf.read_index(path)[1]))
        except (dbpf.DbpfError, OSError) as e:
            checks.append(Check("The packages open", False,
                                f"{Path(path).name} can't be read: {e}"))
            return Verification(checks)

    # Every file is in exactly one package: one in two would be a conflict of our own.
    entries = [e for _path, listed in indexes for e in listed]
    new_keys = {e.key for e in entries}
    planned = set(plan.resources)
    missing_from_pkg = planned - new_keys
    extra = new_keys - planned
    counts = Counter(e.key for e in entries)
    twice = sorted(k for k, n in counts.items() if n > 1)
    checks.append(Check(
        "The packages open and hold the planned files",
        not missing_from_pkg and not extra and not twice,
        f"{_plural(len(new_keys), 'file')} in {_plural(len(indexes), 'package')}, read back "
        "with TrayTrim's own reader.",
        [f"Not in the packages: {k}" for k in sorted(missing_from_pkg)]
        + [f"Not planned but in the packages: {k}" for k in sorted(extra)]
        + [f"In more than one package: {k}" for k in twice]))

    mismatched: list[str] = []
    by_instance: dict[int, list[ResourceKey]] = {}
    for e in entries:
        by_instance.setdefault(e.key.instance, []).append(e.key)
    candidates = scan_candidates(by_instance)
    dangling: list[str] = []
    known_missing: set[ResourceKey] = set(plan.missing)
    done = 0
    with open_packages() as sources:
        for path, listed in indexes:
            try:
                pkg = dbpf.Package(path)
            except (dbpf.DbpfError, OSError) as e:
                mismatched += [f"{entry.key}: couldn't be read ({e})" for entry in listed]
                continue
            with pkg:
                for entry in listed:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    done += 1
                    if progress and done % 20 == 0:
                        progress(done, len(entries))
                    problem = _check_entry(pkg, entry, plan, sources, index, by_instance,
                                           candidates, new_keys, known_missing, dangling)
                    if problem:
                        mismatched.append(problem)
    if progress:
        progress(len(entries), len(entries))

    checks.append(Check(
        "Every file matches its source", not mismatched,
        "Each file decompresses to exactly the same bytes as the copy it came from."
        if not mismatched else f"{_plural(len(mismatched), 'file')} don't match.",
        mismatched))

    detail = "Every mesh, texture and slider file the CC needs is in the export or the game."
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
        "Every CC item in the tray files is included" if from_tray
        else "The item is included", not absent,
        f"{_plural(n_cc, 'item')} checked against the tray files." if from_tray
        else "Its own file is in the export, with everything it refers to.",
        [f"{r.ref.kind.value} {r.ref.instance:016X}" for r in absent]))
    return Verification(checks)


def _check_entry(pkg: dbpf.Package, entry: dbpf.IndexEntry, plan: Plan, sources,
                 index: ResourceIndex, by_instance: dict[int, list[ResourceKey]],
                 candidates: set[int], new_keys: set[ResourceKey],
                 known_missing: set[ResourceKey], dangling: list[str]) -> str:
    """One exported file against its source (returns what's wrong, or ""); what it refers to
    that's nowhere goes into `dangling`."""
    planned_res = plan.resources.get(entry.key)
    if planned_res is None:
        return ""
    try:
        data = pkg.read(entry)
        # Compare with the source package as it is now, through its own index, not through
        # the cached position the export copied from.
        source_pkg = sources.get(planned_res.copy.path)
        current = source_pkg.find(entry.key)
        if current is None:
            return f"{entry.key}: no longer in {planned_res.copy.rel}"
        source = source_pkg.read(current)
    except (dbpf.DbpfError, OSError) as e:
        return f"{entry.key}: couldn't be read ({e})"
    if hashlib.sha256(data).digest() != hashlib.sha256(source).digest():
        return f"{entry.key}: differs from {planned_res.copy.rel}"
    if entry.key.type in LEAF_TYPES:
        return ""
    try:
        found = references(entry.key, data, by_instance, candidates)
    except ReferenceParseError as e:
        dangling.append(f"{entry.key}: its references can't be read ({e})")
        return ""
    for ref in found:
        key = plan.aliases.get(ref.key, ref.key)
        if key in new_keys or key in known_missing or key in plan.from_game:
            continue
        if index.game_hits(key):
            continue
        dangling.append(f"{restypes.name(key.type)} {key}, needed by {entry.key}")
    return ""


def packages_digest(counts: dict[str, int]) -> str:
    """One value for a set of packages and how many files each holds."""
    text = "\n".join(f"{name}\t{n}" for name, n in sorted(counts.items()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def record_check(meta: dict | None, packages: list[str], counts: dict[str, int]) -> Check | None:
    """The packages of an export against the record export._zip_comment wrote (a list, or
    for a very big export a digest of it); None when there's no record."""
    if not meta:
        return None
    listed, digest = meta.get("packages"), meta.get("packages_digest")
    if isinstance(listed, dict):
        return matches_record(listed, packages, counts)
    if isinstance(digest, str):
        found = {PurePosixPath(n).name: counts.get(PurePosixPath(n).name, 0) for n in packages}
        same = packages_digest(found) == digest
        return Check(
            "It holds what was exported", same,
            f"The same {_plural(len(found), 'package')}, with the same number of files each, "
            "as when it was exported." if same else
            "Its packages, or the files in them, aren't the ones it was exported with: one may "
            "have been taken out, added or replaced.")
    return None


def matches_record(listed: dict, packages: list[str], counts: dict[str, int]) -> Check:
    """The packages of an export against the record in its zip comment: none missing, none
    added, each with the files it had."""
    found = {PurePosixPath(n).name for n in packages}
    differ = ([f"{name}: missing from the zip" for name in sorted(listed.keys() - found)]
              + [f"{name}: not in the export" for name in sorted(found - listed.keys())]
              + [f"{name}: {counts.get(name, 0):,} files, it had {listed[name]}"
                 for name in sorted(found & listed.keys()) if counts.get(name) != listed[name]])
    return Check(
        "It holds what was exported", not differ,
        f"The same {_plural(len(listed), 'package')}, with the same number of files each, as "
        "when it was exported." if not differ
        else f"{_plural(len(differ), 'difference')} from when it was exported.", differ)


def read_back(paths: list[Path], cancel: threading.Event | None = None
              ) -> tuple[Check, dict[str, int]]:
    """Every file of these packages decompresses; also how many files each holds, by name."""
    problems: list[str] = []
    counts: dict[str, int] = {}
    for path in paths:
        try:
            pkg = dbpf.Package(path)
        except (dbpf.DbpfError, OSError) as e:
            problems.append(f"{path.name} can't be opened: {describe_error(e)}")
            continue
        with pkg:
            counts[path.name] = len(pkg)
            for entry in pkg:
                if cancel is not None and cancel.is_set():
                    raise Cancelled
                try:
                    pkg.read(entry)
                except dbpf.DbpfError as e:
                    problems.append(f"{path.name}: {entry.key}: {e}")
    total = sum(counts.values())
    return Check(
        "The packages read back", not problems,
        f"{_plural(total, 'file')} in {_plural(len(paths), 'package')}, each decompresses."
        if not problems else f"{_plural(len(problems), 'problem')} reading them back.",
        problems), counts


# progress(step, done, total) for check_zip: step "read" counts bytes of the packages copied
# out of the zip, "files" counts resources decompressed.
CheckProgress = Callable[[str, int, int], None]
# A file in a zip that doesn't read back: damaged (bad checksum or data), cut short, encrypted
# (RuntimeError) or compressed in a way zipfile can't read (NotImplementedError). Disk errors
# (OSError) aren't among them: a full temporary drive isn't a damaged zip, so they're raised.
DAMAGE_ERRORS = (EOFError, zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError,
                 ValueError)  # e.g. a name that says it's UTF-8 and isn't
MAX_REPORT = 16 << 20  # a report.json larger than this isn't one TrayTrim wrote
# Opening a zip: it may also be missing or unreadable.
ZIP_ERRORS = (OSError, *DAMAGE_ERRORS)


def zip_packages(names: list[str]) -> list[str]:
    """The packages in an export's Mods folder, in zip order. Any case: a zip repacked by
    another tool can spell it "mods/" or ".PACKAGE"."""
    return [n for n in names if n.lower().startswith("mods/") and n.lower().endswith(".package")]


def read_zip_comment(comment: bytes) -> dict | None:
    """What export._zip_comment wrote in a TrayTrim export's zip comment, or None for any
    other zip."""
    if not comment.startswith(b'{"traytrim"'):
        return None
    try:
        meta = json.loads(comment)
    except (ValueError, RecursionError):  # nested thousands deep: not one TrayTrim wrote
        return None
    return meta if isinstance(meta, dict) else None


class MemberDamaged(Exception):
    """A file in a zip didn't read back (the cause is chained)."""


def copy_member(z: zipfile.ZipFile, member, dst=None, on_chunk=None,
                cancel: threading.Event | None = None) -> None:
    """Read a file in a zip to its end, into `dst` (an open file) if given. Anything that goes
    wrong reading the zip, an OSError too (a damaged zip can send a seek far off), raises
    MemberDamaged; writing `dst` raises as it is, since a full disk isn't a damaged zip.
    on_chunk(bytes so far) after each chunk."""
    try:
        src = z.open(member)
    except (OSError, *DAMAGE_ERRORS) as e:
        raise MemberDamaged(describe_error(e)) from e
    with src:
        done = 0
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled
            try:
                chunk = src.read(4 << 20)
            except (OSError, *DAMAGE_ERRORS) as e:
                raise MemberDamaged(describe_error(e)) from e
            if not chunk:
                return
            if dst is not None:
                dst.write(chunk)
            done += len(chunk)
            if on_chunk is not None:
                on_chunk(done)


def check_zip(zip_path: Path, progress: CheckProgress | None = None,
              cancel: threading.Event | None = None,
              keep_package: Path | None = None) -> Verification:
    """Check an earlier export on its own, without the Mods folder it came from (which may have
    changed since): the zip is intact and its packages read back. Exports made before
    TrayTrim stopped writing a report.json are also checked against it. Each package is copied
    out of the zip once (its checksum is checked on the way) into a temporary folder, then read
    from there; with keep_package, a zip's only package is copied there instead and left for
    the caller (removed if it's damaged)."""
    try:
        z = zipfile.ZipFile(zip_path)
    except ZIP_ERRORS as e:
        return Verification([Check("The zip opens", False,
                                   f"It can't be read: {describe_error(e)}.")])
    with z, tempfile.TemporaryDirectory(prefix="traytrim-check-",
                                        ignore_cleanup_errors=True) as tmp:
        names = z.namelist()
        packages = zip_packages(names)
        meta = read_zip_comment(z.comment)
        if not packages or ("report.json" not in names and meta is None):
            return Verification([Check(
                "It's a TrayTrim export", False,
                "It doesn't hold packages in a Mods folder and TrayTrim's record of them, like "
                "a TrayTrim export.")])

        damaged: list[str] = []
        package_set = set(packages)
        for name in names:  # the small files; the packages are checked while they're copied
            if name in package_set:
                continue
            try:
                copy_member(z, name)
            except MemberDamaged as e:
                damaged.append(f"{name}: {e}")
        total = sum(z.getinfo(n).file_size for n in packages)
        copied: list[Path] = []
        before = 0
        for i, name in enumerate(packages):
            target = (keep_package if keep_package is not None and len(packages) == 1
                      else Path(tmp) / f"{i}.package")
            info = z.getinfo(name)
            try:
                with open(target, "wb") as dst:
                    copy_member(z, info, dst, (lambda n, b=before: progress("read", b + n, total))
                                if progress else None, cancel)
                copied.append(target)
            except MemberDamaged as e:
                damaged.append(f"{name}: {e}")
                target.unlink(missing_ok=True)
            before += info.file_size
        checks = [Check(
            "The zip is intact", not damaged,
            "Every file in it reads back with the checksum it was saved with." if not damaged
            else f"{_plural(len(damaged), 'file')} in it {'is' if len(damaged) == 1 else 'are'}"
            " damaged.", damaged)]
        if damaged:
            return Verification(checks)

        problems: list[str] = []
        found: dict[str, int] = {}
        # Indexes first, then one package open at a time (see verify_export).
        indexes: list[tuple[str, Path, list[dbpf.IndexEntry]]] = []
        for name, path in zip(packages, copied, strict=True):  # none was damaged
            try:
                indexes.append((name, path, dbpf.read_index(path)[1]))
            except (dbpf.DbpfError, OSError) as e:
                checks.append(Check("The packages read back", False,
                                    f"{PurePosixPath(name).name} can't be opened: "
                                    f"{describe_error(e)}"))
                return Verification(checks)
        entries_total = sum(len(listed) for _n, _p, listed in indexes)
        counts = {PurePosixPath(n).name: len(listed) for n, _p, listed in indexes}
        done = 0
        for name, path, listed in indexes:
            try:
                pkg = dbpf.Package(path)
            except (dbpf.DbpfError, OSError) as e:
                problems.append(f"{PurePosixPath(name).name} can't be opened: "
                                f"{describe_error(e)}")
                continue
            with pkg:
                for entry in listed:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled
                    try:
                        pkg.read(entry)
                    except dbpf.DbpfError as e:
                        problems.append(f"{entry.key}: {e}")
                    found[str(entry.key)] = entry.file_size
                    done += 1
                    if progress and (done % 50 == 0 or done == entries_total):
                        progress("files", done, entries_total)
        checks.append(Check(
            "The packages read back", not problems,
            f"{_plural(entries_total, 'file')} in {_plural(len(packages), 'package')}, each "
            "decompresses." if not problems
            else f"{_plural(len(problems), 'file')} can't be decompressed.", problems))

        if "report.json" not in names:
            record = record_check(meta, packages, counts)
            if record is not None:
                checks.append(record)
            return Verification(checks)
        try:
            if z.getinfo("report.json").file_size > MAX_REPORT:
                raise ValueError("it's far larger than a report")
            report = json.loads(z.read("report.json"))
            listed = {str(f["key"]): f["bytes"] for f in report["files"]}
            if not all(isinstance(n, int) for n in listed.values()):
                raise TypeError("a file size isn't a number")
        except (ValueError, KeyError, TypeError, RecursionError) as e:
            checks.append(Check("It matches its report", False,
                                f"report.json can't be read ({e})."))
            return Verification(checks)
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
