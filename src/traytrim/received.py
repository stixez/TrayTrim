"""Checking a zip before installing it: is it intact, which packs does it need that you don't
have, and which of its CC you already have (and whose copy the game would use).

Works on TrayTrim exports and on any zip with tray files and packages, as other tray importers
make them. Nothing is installed: the zip's tray files and packages are copied into a temporary
folder, indexed there on their own, and compared with your index of Mods and the game. The
temporary folder lives until close(), so the views can read thumbnails and names from it.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from traytrim import dbpf, tray
from traytrim.errors import describe_error
from traytrim.dbpf import MAX_PACKAGE_SIZE, ResourceKey
from traytrim.units import human_size
from traytrim.index import ModsHit, ResourceIndex
from traytrim.modsindex import load_order_key
from traytrim.pkgreport import ITEM_TYPES, item_ref
from traytrim.plan import Cancelled
from traytrim.resolve import EXPECTED_TYPE, LocatedRef, Where, locate
from traytrim.tray import RefKind, TrayItem, TrayRef
from traytrim.verify import (MAX_REPORT, ZIP_ERRORS, Check, MemberDamaged, Verification,
                             check_zip, copy_member, read_back, record_check,
                             read_zip_comment)

# progress(step, done, total); step is "check", "copy", "index" or "compare".
Progress = Callable[[str, int, int], None]
_SHOWN_KINDS = (tray.ITEM_HOUSEHOLD, tray.ITEM_LOT, tray.ITEM_ROOM)
_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}
# A received zip is someone else's file: limits keep a broken or hostile one from filling the
# disk or the memory. Far above anything real (a big tray file is a few MB).
MAX_ENTRIES = 100_000
MAX_TRAY_FILE = 256 << 20
_SPARE = 512 << 20  # left free on the temporary drive


@dataclass
class Overlap:
    """A CC item in the zip that's also in your Mods folder."""
    ref: LocatedRef          # as in the zip
    yours: ModsHit           # your copy, the one the game loads now
    same: bool               # same content
    theirs_loads: bool       # put into Mods as it is, the zip's copy loads last and is used


@dataclass
class ReceivedItem:
    """A tray item in the zip. Its contents aren't kept: a zip may hold hundreds of households,
    and the view only needs what each one is."""
    item: TrayItem
    kind: str                # "household", "lot" or "room"


@dataclass
class Received:
    zip_path: Path
    zip_size: int
    workdir: Path
    integrity: Verification
    report: dict | None = None                 # a TrayTrim export's report.json or zip comment
    items: list[ReceivedItem] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    package_bytes: int = 0
    cc: list[LocatedRef] = field(default_factory=list)   # CC items in the zip
    packs_needed: list[str] = field(default_factory=list)
    packs_missing: list[str] = field(default_factory=list)
    overlap: list[Overlap] = field(default_factory=list)
    not_included: list[LocatedRef] = field(default_factory=list)  # nowhere to be found
    gameplay_mods: list[str] = field(default_factory=list)
    game_known: bool = True   # False: no indexed game to compare with, so game content and
                              # packs can't be checked (and aren't reported as missing)
    jobs: int = 0             # views still reading the temporary folder (see close())
    closing: bool = False

    @property
    def index_path(self) -> Path:
        """The zip's own resource index (for thumbnails)."""
        return self.workdir / "index.sqlite"

    @property
    def name(self) -> str:
        if self.report and self.report.get("name"):
            return str(self.report["name"])
        names = [i.item.name for i in self.items if i.item.name]
        return names[0] if len(names) == 1 else self.zip_path.stem

    @property
    def kind(self) -> str:
        """"household", "lot", "room", "item" (one CC item) or "bundle"."""
        if self.report and self.report.get("kind"):
            return str(self.report["kind"])
        if not self.items:
            return "item" if self.cc else "bundle"
        return self.items[0].kind if len(self.items) == 1 else "bundle"

    def close(self, now: bool = False) -> None:
        """Delete the temporary folder, as soon as the last job reading it is done (or now:
        the app is quitting)."""
        self.closing = True
        if self.jobs <= 0 or now:
            shutil.rmtree(self.workdir, ignore_errors=True)

    def job_done(self) -> None:
        self.jobs -= 1
        if self.closing:
            self.close()


TEMP_PREFIXES = ("traytrim-received-", "traytrim-check-")


def remove_stale_temp(older_than: float = 24 * 3600, root: Path | None = None) -> int:
    """Delete TrayTrim's temporary folders left behind (the app was killed while checking a
    zip), once they're a day old: a running check's folder is never that old. Returns how
    many went."""
    root = Path(root or tempfile.gettempdir())
    cutoff = time.time() - older_than
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (entry.name.startswith(TEMP_PREFIXES) and entry.is_dir(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                shutil.rmtree(entry.path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def check_received(zip_path: Path, index: ResourceIndex, progress: Progress | None = None,
                   cancel: threading.Event | None = None) -> Received:
    """Check a zip against your Mods and game (`index`). The caller closes the result.

    Everything is unpacked once: a TrayTrim export's package by its full check (check_zip),
    which leaves it in the temporary folder; any other zip's tray files and packages as they're
    copied out, with every other file read back too, so each file's checksum is checked."""
    workdir = Path(tempfile.mkdtemp(prefix="traytrim-received-"))
    try:
        size = zip_path.stat().st_size if zip_path.is_file() else 0
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
        except ZIP_ERRORS as e:
            return Received(zip_path, size, workdir, Verification([Check(
                "The zip opens", False, f"It can't be read: {describe_error(e)}.")]))
        if len(names) > MAX_ENTRIES:
            return Received(zip_path, size, workdir, Verification([Check(
                "The zip opens", False, f"It holds {len(names):,} files, far more than a tray "
                "export; TrayTrim doesn't unpack it.")]))
        (workdir / "Tray").mkdir()
        (workdir / "Mods").mkdir()
        packages = [n for n in names if n.startswith("Mods/") and n.endswith(".package")]
        _check_space(zip_path, names, workdir)
        exported = "report.json" in names and len(packages) == 1
        if exported:
            with zipfile.ZipFile(zip_path) as z:
                exported = z.getinfo(packages[0]).file_size <= MAX_PACKAGE_SIZE
        result = Received(zip_path, size, workdir, Verification([]))
        if exported:
            kept = workdir / "Mods" / (_safe_name(packages[0]) or "export.package")
            result.integrity = check_zip(
                zip_path, (lambda _s, d, t: progress("check", d, t)) if progress else None,
                cancel, keep_package=kept)
            if kept.exists():
                result.packages.append(kept.name)
                result.package_bytes += kept.stat().st_size
        with zipfile.ZipFile(zip_path) as z:
            if "report.json" in names and z.getinfo("report.json").file_size <= MAX_REPORT:
                try:
                    report = json.loads(z.read("report.json"))
                    result.report = report if isinstance(report, dict) else None
                except ZIP_ERRORS:
                    pass
            else:  # newer exports keep it in the zip's comment, not in a file
                result.report = read_zip_comment(z.comment)
            damaged = _copy_out(z, names, result, packages[0] if exported else None,
                                not exported, progress, cancel)
        if not exported:
            n = len(damaged)
            result.integrity = Verification([Check(
                "The zip is intact", not damaged,
                "Every file in it reads back with the checksum it was saved with."
                if not damaged else
                f"{n:,} file{'' if n == 1 else 's'} in it {'is' if n == 1 else 'are'} damaged.",
                damaged)])
            if not damaged and result.report is not None and (
                    "packages" in result.report or "packages_digest" in result.report):
                # A TrayTrim export that records its packages: as thorough as check_zip.
                read, counts = read_back([result.workdir / "Mods" / p for p in result.packages],
                                         cancel)
                record = record_check(result.report, [f"Mods/{p}" for p in result.packages],
                                      counts)
                result.integrity.checks += [read] + ([record] if record is not None else [])
        _compare(result, index, progress, cancel)
        return result
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def _check_space(zip_path: Path, names: list[str], workdir: Path) -> None:
    """Refuse to unpack what the temporary folder's drive has no room for (by the sizes the
    zip states; reading never goes past them)."""
    with zipfile.ZipFile(zip_path) as z:
        needed = sum(z.getinfo(n).file_size for n in names
                     if (base := _safe_name(n)) is not None
                     and (base.lower().endswith(".package") or tray.parse_tray_filename(base)))
    free = shutil.disk_usage(workdir).free
    if needed > free - _SPARE:
        raise OSError(errno.ENOSPC, f"Checking it needs {human_size(needed)} free on the drive "
                      f"with the temporary folder, and {human_size(max(free, 0))} is free")


def _safe_name(name: str) -> str | None:
    """A zip entry's file name, safe to write in the temporary folder: only the last part of
    its path (by / or \\), and None for names that could point elsewhere on some system (a
    drive like "D:x", "..", control characters), that Windows would change, or that Windows
    opens as a device (CON, NUL, COM1...)."""
    base = re.split(r"[\\/]", name)[-1]
    if (not base or base in (".", "..") or ":" in base or base != base.rstrip(". ")
            or any(ord(c) < 32 for c in base)
            or base.split(".")[0].upper() in _DEVICE_NAMES):
        return None
    return base


def _copy_out(z: zipfile.ZipFile, names: list[str], result: Received, skip: str | None,
              read_rest: bool, progress: Progress | None,
              cancel: threading.Event | None) -> list[str]:
    """Tray files into workdir/Tray, packages into workdir/Mods, by file name only (a path in
    the zip never decides where anything is written). `skip` is already copied; with
    read_rest every other file is read too. Returns the files that didn't read back."""
    tray_dir, mods_dir = result.workdir / "Tray", result.workdir / "Mods"
    jobs: list[tuple[str, Path | None]] = []
    # Compared without case: on Windows and macOS "Hair.package" and "hair.package" are one.
    taken = {p.name.casefold() for p in mods_dir.iterdir()}
    for name in names:
        if name == skip or name.endswith("/"):
            continue
        base = _safe_name(name)
        if base is None:
            if read_rest:
                jobs.append((name, None))  # still read, for the checksum
            continue
        if tray.parse_tray_filename(base) is not None:
            jobs.append((name, tray_dir / base))
        elif base.lower().endswith(".package"):
            target = mods_dir / base
            n = 2
            while target.name.casefold() in taken:
                target = mods_dir / f"{Path(base).stem} ({n}).package"
                n += 1
            taken.add(target.name.casefold())
            jobs.append((name, target))
        elif read_rest:
            jobs.append((name, None))
    damaged: list[str] = []
    sizes = {name: z.getinfo(name).file_size for name, _target in jobs}
    for name, target in jobs:
        limit = (MAX_TRAY_FILE if target is not None and target.parent == tray_dir
                 else MAX_PACKAGE_SIZE if target is not None else None)
        if limit is not None and sizes[name] > limit:
            damaged.append(f"{name}: says it's {sizes[name]:,} bytes, too large to be what its "
                           "name says")
    if damaged:
        return damaged
    for done, (name, target) in enumerate(jobs, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        try:
            if target is None:
                copy_member(z, name, cancel=cancel)
            else:
                with open(target, "wb") as dst:
                    copy_member(z, name, dst, cancel=cancel)
        except MemberDamaged as e:
            damaged.append(f"{name}: {e}")
            if target is not None:
                target.unlink(missing_ok=True)
            continue
        if target is not None and target.parent == mods_dir:
            result.packages.append(target.name)
            result.package_bytes += target.stat().st_size
        if progress:
            progress("copy", done, len(jobs))
    return damaged


def _compare(result: Received, index: ResourceIndex, progress: Progress | None,
             cancel: threading.Event | None) -> None:
    workdir = result.workdir
    with ResourceIndex(result.index_path) as own:
        update = own.update(workdir / "Mods", None,
                            (lambda _phase, d, t: progress("index", d, t)) if progress else None,
                            cancel)
        if update.cancelled:
            raise Cancelled
        try:
            items, _failed = tray.scan_tray(workdir / "Tray")
        except OSError:
            items = []
        cc: dict[TrayRef, LocatedRef] = {}
        missing: dict[TrayRef, LocatedRef] = {}
        installed = index.installed_packs()
        result.game_known = bool(installed)
        shown = [i for i in items if i.type in _SHOWN_KINDS]
        for done, item in enumerate(shown, 1):
            if cancel is not None and cancel.is_set():
                raise Cancelled
            try:
                contents = tray.read_contents(item)
            except (tray.TrayError, OSError, ValueError):
                continue
            theirs = locate(contents, own)
            result.items.append(ReceivedItem(item, contents.kind))
            for r in theirs:
                if r.where is Where.CC:
                    cc.setdefault(r.ref, r)
                elif r.ref not in missing and not _installed(r.ref, index):
                    missing[r.ref] = r
            if progress:
                progress("compare", done, len(shown))
        if not shown:  # one CC item exported on its own: the items are what's in the package
            for key in own.mods_keys_of_types(ITEM_TYPES):
                r = item_ref(key, own)
                cc.setdefault(r.ref, r)
        result.cc = sorted(cc.values(), key=lambda r: (r.ref.group, r.ref.instance))
        # Without a game to look in, everything from the game would look missing.
        result.not_included = list(missing.values()) if result.game_known else []
        result.overlap = _overlap(result.cc, index, progress, cancel)

    report = result.report or {}
    needed = report.get("game_packs")
    needed = needed if isinstance(needed, list) else []  # a string would be split into letters
    result.packs_needed = [p for p in needed if isinstance(p, str)]
    if installed:  # without an indexed game there's nothing to compare with
        result.packs_missing = [p for p in result.packs_needed if p not in installed]
    mods = report.get("gameplay_mods")
    result.gameplay_mods = [m for m in mods if isinstance(m, str)] if isinstance(mods, list) else []


def _installed(ref: TrayRef, index: ResourceIndex) -> bool:
    """Whether your Mods or game has what a tray reference points to (as locate() finds it)."""
    if ref.kind is RefKind.BUILD:
        key = ResourceKey(ref.type_id, ref.res_group or 0, ref.instance)
        return bool(index.mods_hits(key) or index.game_hits(key))
    return bool(index.keys_with_instance(ref.instance, EXPECTED_TYPE[ref.kind]))


def _overlap(cc: list[LocatedRef], index: ResourceIndex, progress: Progress | None,
             cancel: threading.Event | None) -> list[Overlap]:
    """Which of the zip's CC items you already have, and whether your copy is the same. Only
    copies of the same size are read, each package once (comparisons that jump between
    thousands of packages would otherwise open them over and over)."""
    yours_by_key = index.mods_hits_many([r.used.entry.key for r in cc if r.used is not None])
    pairs = []
    for r in cc:
        hits = yours_by_key.get(r.used.entry.key, []) if r.used is not None else []
        if hits:
            pairs.append((r, hits[-1]))
    to_read: dict[Path, list[dbpf.IndexEntry]] = {}
    for r, yours in pairs:
        if r.used.entry.mem_size == yours.entry.mem_size:
            to_read.setdefault(r.used.path, []).append(r.used.entry)
            to_read.setdefault(yours.path, []).append(yours.entry)
    digests: dict[tuple[Path, ResourceKey], bytes] = {}
    for done, (path, entries) in enumerate(to_read.items(), 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled
        try:
            with dbpf.Package(path) as pkg:
                for e in entries:
                    try:
                        digests[(path, e.key)] = hashlib.sha1(pkg.read(e)).digest()
                    except dbpf.DbpfError:
                        continue  # unreadable: not counted as the same
        except (dbpf.DbpfError, OSError):
            continue
        if progress and done % 20 == 0:
            progress("compare", done, len(to_read))
    out = []
    for r, yours in pairs:
        a, b = digests.get((r.used.path, r.used.entry.key)), digests.get((yours.path, yours.entry.key))
        out.append(Overlap(r, yours, a is not None and a == b,
                           load_order_key(r.used.rel) > load_order_key(yours.rel)))
    return out
