"""Which CC needs which: the references between Mods resources, for the Mods tab ("what breaks
if I delete this package").

Kept in deps.sqlite next to the resource index, as a cache that can always be deleted. It's built
from the index's Mods packages, and a package is read again only when its size or modification
time changed. References are found the way plan.py follows them: the key lists of CAS parts and
meshes, the scan for instances of the same package (references.py), what an item takes along
from its own package (_taken_along), and for object definitions, catalog entries and models the
assets of other packages whose instances they hold.

Those cross-package matches depend on what else is in Mods. A package read again is matched
against all of Mods as it is then. When it brings meshes or textures no other package has, the
packages that weren't read again are searched for them too (those keys wait in `pending` until
that search finished, so a cancel doesn't lose them; a package that can't be searched right now
is read again in full at the next update).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from traytrim import dbpf, restypes
from traytrim.dbpf import ResourceKey
from traytrim.index import ResourceIndex, _s64
from traytrim.paths import cache_dir
from traytrim.plan import Cancelled
from traytrim.references import (ASSET_TYPES, CROSS_SCAN_TYPES, MAX_CROSS_SCAN_BYTES,
                                 MIN_SCAN_INSTANCE, ReferenceParseError, large_values, may_refer,
                                 references, scan_candidates, scan_instances)

SCHEMA_VERSION = 4  # 4: images and videos are no longer scanned for references
READ, MATCH = "read", "match"

# progress(phase, done, total): READ counts packages read, MATCH packages searched for new
# meshes and textures.
Progress = Callable[[str, int, int], None]

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE packages (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    cross INTEGER NOT NULL DEFAULT 0       -- holds resources matched against other packages
);
CREATE TABLE refs (
    to_inst INTEGER NOT NULL, to_type INTEGER NOT NULL, to_grp INTEGER NOT NULL,
    from_pkg INTEGER NOT NULL,
    from_inst INTEGER NOT NULL, from_type INTEGER NOT NULL, from_grp INTEGER NOT NULL,
    cross INTEGER NOT NULL,                -- 1: found by matching against other packages
    PRIMARY KEY (to_inst, to_type, to_grp, from_pkg, from_inst, from_type, from_grp)
) WITHOUT ROWID;
CREATE TABLE pending (                     -- new assets not yet searched for in other packages
    inst INTEGER NOT NULL, type INTEGER NOT NULL, grp INTEGER NOT NULL,
    PRIMARY KEY (inst, type, grp)
) WITHOUT ROWID;
"""


def _u64(s: int) -> int:
    return s + (1 << 64) if s < 0 else s


def _taken_along(pkg: dbpf.Package, by_instance: dict[int, list[ResourceKey]]
                 ) -> list[tuple[ResourceKey, ResourceKey]]:
    """What the export plan takes along with an item from its own package without a reference
    to follow (plan.py): an object everything under its instance (catalog entry, thumbnails,
    sim data), a CAS part its thumbnail, a build item its thumbnail. Stored as references, so
    "what breaks" and "used by" follow them too."""
    rows = []
    for e in pkg:
        t, same = e.key.type, by_instance.get(e.key.instance, ())
        if t == restypes.OBJECT_DEFINITION:
            rows += [(e.key, k) for k in same if k != e.key]
        elif t == restypes.CASP:
            rows += [(e.key, k) for k in same if k.type == restypes.THUM]
        elif t in restypes.BUILD_NAMES:
            rows += [(e.key, k) for k in same if k.type == restypes.OBJECT_THUMB]
    return rows


_ASSETS_FROM = 50  # packages to read before _asset_instances pays for itself


def _asset_instances(index: ResourceIndex) -> set[int]:
    """The instances of every asset in Mods that a scan can find (large_values() keeps only
    values of at least 2^32): what object definitions, catalog entries and models are matched
    against. A few hundred thousand on a big Mods folder."""
    return {i for i in index.mods_instances(ASSET_TYPES) if i >= MIN_SCAN_INSTANCE}


@dataclass(frozen=True)
class Need:
    """`by` (in the package at `path`) refers to `key`."""
    key: ResourceKey
    path: Path
    by: ResourceKey


class DependencyIndex:
    """One connection per thread, like ResourceIndex."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else cache_dir() / "deps.sqlite"
        self.unreadable: list[Path] = []  # packages the last update couldn't read
        self._assets: set[int] | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._open()

    def _open(self) -> sqlite3.Connection:
        try:
            return self._open_schema()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError:  # not a database: it's only a cache, start over
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(f"{self.path}{suffix}")
                except FileNotFoundError:
                    pass
            return self._open_schema()

    def _open_schema(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("BEGIN IMMEDIATE")
            has_meta = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone() \
                if has_meta else None
            if (row[0] if row else None) != str(SCHEMA_VERSION):
                for (name,) in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'").fetchall():
                    db.execute(f'DROP TABLE "{name}"')
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                db.execute("INSERT INTO meta VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
            db.commit()
        except BaseException:
            db.close()
            raise
        return db

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> DependencyIndex:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # Building

    def update(self, index: ResourceIndex, progress: Progress | None = None,
               cancel: threading.Event | None = None) -> None:
        """Bring the references in line with the resource index's Mods packages. A cancel
        keeps what's done; the next update carries on from there."""
        db = self.db
        packages = index.mods_packages()
        existing = {path: (pkg, size, mtime) for pkg, path, size, mtime in db.execute(
            "SELECT id, path, size, mtime_ns FROM packages")}
        current = {str(p.path) for p in packages}
        todo = [p for p in packages
                if existing.get(str(p.path), (None,))[1:] != (p.size, p.mtime_ns)]
        dropped = [row[0] for path, row in existing.items() if path not in current]
        dropped += [existing[str(p.path)][0] for p in todo if str(p.path) in existing]
        if dropped:
            self._drop(dropped)
        db.commit()

        self.unreadable = []
        # Object definitions and models hold thousands of 64-bit values each, nearly all of
        # them not an asset in Mods. Reading many packages, they're matched against all the
        # assets there in one go; for a few, listing those assets would take longer.
        self._assets = _asset_instances(index) if len(todo) >= _ASSETS_FROM else None
        last_commit = time.monotonic()
        for done, p in enumerate(todo, 1):
            self._check(cancel)
            read = self._read(p.path, cancel, self._assets)
            if read is None:
                # Locked or damaged right now: not stored, so the next update tries again.
                self.unreadable.append(p.path)
                continue
            rows, holders, own = read
            cross_rows = self._match(index, holders, own)
            cur = db.execute("INSERT INTO packages (path, size, mtime_ns, cross) VALUES (?,?,?,?)",
                             (str(p.path), p.size, p.mtime_ns, int(bool(holders))))
            pkg = cur.lastrowid
            for flag, pairs in ((0, rows), (1, cross_rows)):
                db.executemany("INSERT OR IGNORE INTO refs VALUES (?,?,?,?,?,?,?,?)",
                               ((_s64(t.instance), t.type, t.group, pkg,
                                 _s64(f.instance), f.type, f.group, flag) for f, t in pairs))
            # Its assets wait in `pending`, committed with the package: packages that aren't
            # read again may refer to them, and a cancel mustn't lose that.
            db.executemany("INSERT OR IGNORE INTO pending VALUES (?,?,?)",
                           ((_s64(k.instance), k.type, k.group) for k in own
                            if k.type in ASSET_TYPES))
            if progress:
                progress(READ, done, len(todo))
            if time.monotonic() - last_commit > 2:
                db.commit()
                last_commit = time.monotonic()
        # The packages read just now were matched against all of Mods already; the others are
        # searched for the pending assets.
        read_paths = {p.path for p in todo}
        db.commit()
        self._assets = None
        self._search_pending(read_paths, progress, cancel)

    def _search_pending(self, skip: set[Path], progress: Progress | None,
                        cancel: threading.Event | None) -> None:
        """Look for the pending assets in every package's object definitions, catalog entries
        and models (by instance, as the export plan does)."""
        db = self.db
        by_instance: dict[int, list[ResourceKey]] = {}
        for inst, t, g in db.execute("SELECT inst, type, grp FROM pending"):
            key = ResourceKey(t, g, _u64(inst))
            by_instance.setdefault(key.instance, []).append(key)
        if not by_instance:
            return
        wanted = {i for i in by_instance if i >= MIN_SCAN_INSTANCE}  # what a scan can find
        holders = [(pkg_id, path) for pkg_id, path in db.execute(
            "SELECT id, path FROM packages WHERE cross=1") if Path(path) not in skip]
        missed: list[int] = []
        for done, (pkg_id, path) in enumerate(holders, 1):
            self._check(cancel)
            rows = []
            try:
                with dbpf.Package(path) as pkg:
                    own = {e.key for e in pkg}
                    for e in pkg:
                        if e.key.type not in CROSS_SCAN_TYPES:
                            continue
                        try:
                            found = scan_instances(pkg.read(e), wanted, MAX_CROSS_SCAN_BYTES)
                        except dbpf.DbpfError:
                            continue
                        rows += [(e.key, k) for inst in found for k in by_instance[inst]
                                 if k not in own]
            except (dbpf.DbpfError, OSError):
                missed.append(pkg_id)  # gone or locked right now: read in full next time
                self.unreadable.append(Path(path))
                continue
            db.executemany("INSERT OR IGNORE INTO refs VALUES (?,?,?,?,?,?,?,1)",
                           ((_s64(t.instance), t.type, t.group, pkg_id,
                             _s64(f.instance), f.type, f.group) for f, t in rows))
            if progress:
                progress(MATCH, done, len(holders))
        # A package that couldn't be searched keeps what it had, and is marked as changed: the
        # next update reads it again in full, matched against all of Mods (the pending assets
        # too). So the pending assets are done either way; keeping them would search every
        # package again at each update.
        db.executemany("UPDATE packages SET mtime_ns = -1 WHERE id = ?", ((i,) for i in missed))
        db.execute("DELETE FROM pending")
        db.commit()

    @staticmethod
    def _match(index: ResourceIndex, holders: list[tuple[ResourceKey, set[int]]],
               own: set[ResourceKey]) -> list[tuple[ResourceKey, ResourceKey]]:
        """The assets in other packages that one package's object definitions, catalog
        entries and models refer to (one lookup for the whole package)."""
        if not holders:
            return []
        values = set().union(*(vals for _key, vals in holders))
        by_instance: dict[int, list[ResourceKey]] = {}
        for k in index.mods_keys_for_instances(values, ASSET_TYPES):
            if k not in own:
                by_instance.setdefault(k.instance, []).append(k)
        return [(key, k) for key, vals in holders for inst in vals & by_instance.keys()
                for k in by_instance[inst]]

    @staticmethod
    def _read(path: Path, cancel: threading.Event | None = None, assets: set[int] | None = None
              ) -> tuple[list, list, set] | None:
        """For one package: (referring key, referred key) for every reference inside it, its
        object definitions, catalog entries and models with the values they hold that are
        asset instances in Mods (every large value without `assets`), matched against other
        packages, and its keys. None if it can't be read."""
        rows: list[tuple[ResourceKey, ResourceKey]] = []
        holders: list[tuple[ResourceKey, set[int]]] = []
        try:
            with dbpf.Package(path) as pkg:
                by_instance: dict[int, list[ResourceKey]] = {}
                for e in pkg:
                    by_instance.setdefault(e.key.instance, []).append(e.key)
                candidates = scan_candidates(by_instance)
                for n, e in enumerate(pkg):
                    if n % 64 == 0:  # a big merge takes a while: stop when asked
                        DependencyIndex._check(cancel)
                    if not may_refer(e.key.type):
                        continue
                    try:
                        data = pkg.read(e)
                        rows += [(e.key, r.key)
                                 for r in references(e.key, data, by_instance, candidates)]
                    except (dbpf.DbpfError, ReferenceParseError):
                        continue  # the export plan reports unreadable resources
                    if e.key.type in CROSS_SCAN_TYPES:
                        holders.append((e.key, large_values(data) if assets is None else
                                        scan_instances(data, assets, MAX_CROSS_SCAN_BYTES)))
                rows += _taken_along(pkg, by_instance)
                own = {e.key for e in pkg}
        except (dbpf.DbpfError, OSError):
            return None
        return rows, holders, own

    @staticmethod
    def _check(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise Cancelled

    def _drop(self, ids: list[int]) -> None:
        db = self.db
        db.execute("CREATE TEMP TABLE IF NOT EXISTS drop_ids (id INTEGER PRIMARY KEY)")
        db.execute("DELETE FROM drop_ids")
        db.executemany("INSERT INTO drop_ids VALUES (?)", ((i,) for i in ids))
        db.execute("DELETE FROM refs WHERE from_pkg IN (SELECT id FROM drop_ids)")
        db.execute("DELETE FROM packages WHERE id IN (SELECT id FROM drop_ids)")

    # Lookups

    def needing(self, keys: Iterable[ResourceKey], exclude: Path | None = None) -> list[Need]:
        """Every resource that refers to any of these keys (except those in `exclude`)."""
        return self._needs("r.to_inst = w.inst AND r.to_type = w.type AND r.to_grp = w.grp",
                           ((_s64(k.instance), k.type, k.group) for k in keys), exclude)

    def needing_texture_instances(self, instances: Iterable[int],
                                  exclude: Path | None = None) -> list[Need]:
        """Every reference to a texture with one of these instances, in any format or group:
        a reference to a texture that isn't there is resolved to the one stored under the same
        instance (see plan.py), so those can depend on a texture without naming it."""
        types = ",".join(str(t) for t in sorted(restypes.TEXTURE_TYPES))
        return self._needs(f"r.to_inst = w.inst AND r.to_type IN ({types})",
                           ((_s64(i), 0, 0) for i in instances), exclude)

    def _needs(self, match: str, wanted, exclude: Path | None) -> list[Need]:
        db = self.db
        db.execute("CREATE TEMP TABLE IF NOT EXISTS wanted "
                   "(inst INTEGER, type INTEGER, grp INTEGER, PRIMARY KEY (inst, type, grp))")
        db.execute("DELETE FROM wanted")
        db.executemany("INSERT OR IGNORE INTO wanted VALUES (?,?,?)", wanted)
        rows = db.execute(
            "SELECT r.to_inst, r.to_type, r.to_grp, p.path, r.from_inst, r.from_type, r.from_grp "
            f"FROM wanted w CROSS JOIN refs r ON {match} JOIN packages p ON p.id = r.from_pkg "
            "WHERE p.path != ?", (str(exclude) if exclude is not None else "",))
        paths: dict[str, Path] = {}
        return [Need(ResourceKey(tt, tg, _u64(ti)), paths.get(path) or paths.setdefault(path, Path(path)),
                     ResourceKey(ft, fg, _u64(fi)))
                for ti, tt, tg, path, fi, ft, fg in rows]
