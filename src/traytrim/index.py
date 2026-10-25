"""The resource index: which package holds each resource key, for Mods and for the game.

Stored in SQLite in TrayTrim's cache folder. update() only re-reads packages whose size or
modification time changed (and ones that failed last time), so after the first run it takes
about as long as listing the folders. The cache can always be deleted and rebuilt.

Mods rows keep where the resource sits in its package (offset, sizes, compression), so an export
can copy it later. Game rows only record that the key exists and in which pack: game content is
never copied.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from traytrim import dbpf, gameindex, modsindex
from traytrim.dbpf import ResourceKey
from traytrim.paths import cache_dir

SCHEMA_VERSION = 1
MODS, GAME = "mods", "game"

# progress(phase, done, total) with phase MODS or GAME.
Progress = Callable[[str, int, int], None]

_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE packages (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,          -- 'mods' or 'game'
    path TEXT NOT NULL UNIQUE,
    rel TEXT NOT NULL,
    pack TEXT,                     -- game only: 'base', 'EP03', ...
    delta INTEGER NOT NULL DEFAULT 0,
    load_order INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    resources INTEGER NOT NULL DEFAULT 0,
    error TEXT                     -- why the package couldn't be read, or NULL
);
CREATE TABLE mods_res (
    inst INTEGER NOT NULL, type INTEGER NOT NULL, grp INTEGER NOT NULL, pkg INTEGER NOT NULL,
    offset INTEGER NOT NULL, fsize INTEGER NOT NULL, msize INTEGER NOT NULL, comp INTEGER NOT NULL,
    PRIMARY KEY (inst, type, grp, pkg)
) WITHOUT ROWID;
CREATE TABLE game_res (
    inst INTEGER NOT NULL, type INTEGER NOT NULL, grp INTEGER NOT NULL, pkg INTEGER NOT NULL,
    PRIMARY KEY (inst, type, grp, pkg)
) WITHOUT ROWID;
"""


def _s64(u: int) -> int:
    """SQLite integers are signed; store 64-bit instances as two's complement."""
    u &= 0xFFFF_FFFF_FFFF_FFFF  # never more than 64 bits, whatever a file held
    return u - (1 << 64) if u >= 1 << 63 else u


def _chunks(keys: list, size: int = 2000):
    for i in range(0, len(keys), size):
        yield keys[i:i + size]


@dataclass(frozen=True)
class ModsHit:
    path: Path
    rel: str
    load_order: int
    entry: dbpf.IndexEntry


@dataclass(frozen=True)
class GameHit:
    path: Path
    rel: str
    pack: str
    delta: bool


@dataclass(frozen=True)
class ModsPackageInfo:
    path: Path
    rel: str
    load_order: int
    size: int
    mtime_ns: int
    resources: int
    items: int = 0  # resources of the types asked for (see mods_packages)
    kinds: dict[int, int] = field(default_factory=dict, hash=False, compare=False)  # type -> n


@dataclass
class UpdateResult:
    mods_packages: int = 0
    mods_resources: int = 0
    game_packages: int = 0
    game_resources: int = 0
    reread: int = 0                      # packages read this time (new, changed or retried)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (path, reason)
    too_deep: list[str] = field(default_factory=list)
    mods_depth: int = modsindex.DEFAULT_DEPTH
    cancelled: bool = False
    changed: bool = False                # packages were added, changed or removed


class _Cancelled(Exception):
    pass


class ResourceIndex:
    """One connection per thread: create an instance in the thread that uses it."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else cache_dir() / "index.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = self._open()

    def _open(self) -> sqlite3.Connection:
        """Open the cache, creating or upgrading the schema if needed.

        Another TrayTrim process (the app and the CLI) may use the same file at the same time,
        so the schema check runs inside a write transaction, and the file is only deleted when
        SQLite says it isn't a database at all. "Locked" or "busy" errors are passed on.
        """
        try:
            return self._open_schema()
        except sqlite3.OperationalError:
            raise
        except sqlite3.DatabaseError:  # not a database / malformed: it's only a cache, start over
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(f"{self.path}{suffix}")
                except FileNotFoundError:
                    pass
            return self._open_schema()

    def _open_schema(self) -> sqlite3.Connection:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            has_meta = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            version = None
            if has_meta:
                row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                version = row[0] if row else None
            if version != str(SCHEMA_VERSION):
                # Empty, or written by another TrayTrim version: replace the tables.
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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.DatabaseError:
            db.close()  # a damaged file fails here already; close it so it can be deleted
            raise
        return db

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> ResourceIndex:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # Building

    def update(self, mods_root: Path | None, game_root: Path | None,
               progress: Progress | None = None,
               cancel: threading.Event | None = None) -> UpdateResult:
        """Bring the index in line with the folders.

        A folder that is None, or that can't be reached at all, leaves that part of the index as
        it is: not finding a folder this time (a drive not plugged in, detection failing) is no
        reason to throw away a cache that took a while to build. A different folder replaces
        it, and a Sims 4 folder that simply has no Mods folder counts as an empty one.
        """
        result = UpdateResult()
        try:
            if mods_root is not None and mods_root.is_dir():
                scan = modsindex.scan_mods(mods_root, cancel)
                result.too_deep, result.mods_depth = scan.too_deep, scan.depth
                self._sync(MODS, mods_root,
                           [(p.path, p.rel, None, False, p.load_order) for p in scan.packages],
                           result, progress, cancel)
            elif mods_root is not None and mods_root.parent.is_dir():
                self._sync(MODS, mods_root, [], result, progress, cancel)
            if game_root is not None and game_root.is_dir():
                self._sync(GAME, game_root,
                           [(p.path, p.rel, p.pack, p.delta, 0)
                            for p in gameindex.scan_game(game_root)],
                           result, progress, cancel)
        except (_Cancelled, modsindex.ScanCancelled):
            result.cancelled = True
        self._fill_totals(result)
        return result

    def _sync(self, source: str, root: Path, found: list[tuple], result: UpdateResult,
              progress: Progress | None, cancel: threading.Event | None) -> None:
        db = self.db
        root_key = f"{source}_root"
        complete_key = f"{source}_complete"
        stored_root = db.execute("SELECT value FROM meta WHERE key=?", (root_key,)).fetchone()
        root_text = str(root)
        if stored_root is None or stored_root[0] != root_text:
            self._drop_packages(source, None)
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (root_key, root_text))
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, '0')", (complete_key,))
            db.commit()

        existing = {row[1]: row for row in db.execute(
            "SELECT id, path, size, mtime_ns, error, load_order, rel FROM packages "
            "WHERE source=?", (source,))}
        todo = []
        retries: set[str] = set()
        seen = set()
        for n, (path, rel, pack, delta, order) in enumerate(found):
            if n % 256 == 0 and cancel is not None and cancel.is_set():
                raise _Cancelled
            key = str(path)
            try:
                st = os.stat(path)
            except OSError as e:
                # Not in `seen`: an earlier entry for it is dropped rather than trusted.
                result.failed.append((key, e.strerror or str(e)))
                continue
            seen.add(key)
            old = existing.get(key)
            same = bool(old) and old[2] == st.st_size and old[3] == st.st_mtime_ns
            if same and old[4] is None:
                if source == MODS and (old[5], old[6]) != (order, rel):
                    db.execute("UPDATE packages SET load_order=?, rel=? WHERE id=?",
                               (order, rel, old[0]))
                continue
            if same:
                retries.add(key)  # failed last time, unchanged: read again, but not a change
            todo.append((key, rel, pack, delta, order, st.st_size, st.st_mtime_ns))
        gone = [row[0] for path, row in existing.items() if path not in seen]
        changed = [existing[t[0]][0] for t in todo if t[0] in existing]
        if (any(t[0] not in retries for t in todo) or gone
                or stored_root is None or stored_root[0] != root_text):
            result.changed = True
        if todo or gone:
            # From here until the end of this sync the index is between two states; lookups
            # made meanwhile (another process, or a cancelled run) shouldn't be trusted.
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, '0')", (complete_key,))
        self._drop_packages(source, gone + changed)
        db.commit()

        table = "mods_res" if source == MODS else "game_res"
        last_commit = time.monotonic()
        for done, (key, rel, pack, delta, order, size, mtime) in enumerate(todo, 1):
            if cancel is not None and cancel.is_set():
                db.commit()
                raise _Cancelled
            error, rows = None, []
            try:
                rows = dbpf.scan_index(key)
            except (dbpf.DbpfError, OSError) as e:
                error = str(e) if isinstance(e, dbpf.DbpfError) else (e.strerror or str(e))
                result.failed.append((key, error))
            if error is None and key in retries:
                result.changed = True  # readable now
            cur = db.execute(
                "INSERT INTO packages (source, path, rel, pack, delta, load_order, size, "
                "mtime_ns, resources, error) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (source, key, rel, pack, int(delta), order, size, mtime, len(rows), error))
            pkg = cur.lastrowid
            if source == MODS:
                db.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?,?,?,?,?)",
                               ((_s64(i), t, g, pkg, off, fs, ms, c)
                                for t, g, i, off, fs, ms, c, _ in rows))
            else:
                db.executemany(f"INSERT OR REPLACE INTO {table} VALUES (?,?,?,?)",
                               ((_s64(i), t, g, pkg) for t, g, i, *_ in rows))
            result.reread += 1
            if progress:
                progress(source, done, len(todo))
            if time.monotonic() - last_commit > 2:
                db.commit()
                last_commit = time.monotonic()
        db.execute("INSERT OR REPLACE INTO meta VALUES (?, '1')", (complete_key,))
        db.commit()

    def is_complete(self, mods_root: Path | None, game_root: Path | None) -> bool:
        """True when the index fully reflects these folders as of its last finished update
        (folders that are None aren't checked). It may be out of date, but it's consistent."""
        meta = dict(self.db.execute("SELECT key, value FROM meta"))
        for source, root in ((MODS, mods_root), (GAME, game_root)):
            if root is None:
                continue
            if meta.get(f"{source}_root") != str(root) or meta.get(f"{source}_complete") != "1":
                return False
        return True

    def _drop_packages(self, source: str, ids: list[int] | None) -> None:
        """Remove packages (all of a source when ids is None) and their resources."""
        db = self.db
        table = "mods_res" if source == MODS else "game_res"
        if ids is None:
            db.execute(f"DELETE FROM {table}")
            db.execute("DELETE FROM packages WHERE source=?", (source,))
            return
        if not ids:
            return
        # One pass over the resource table for all dropped packages. There is deliberately no
        # index on pkg: it would add ~60 MB to the cache, and this scan costs ~0.3 s on a full
        # game install, once per update that found changes.
        db.execute("CREATE TEMP TABLE IF NOT EXISTS drop_ids (id INTEGER PRIMARY KEY)")
        db.execute("DELETE FROM drop_ids")
        db.executemany("INSERT INTO drop_ids VALUES (?)", ((i,) for i in ids))
        db.execute(f"DELETE FROM {table} WHERE pkg IN (SELECT id FROM drop_ids)")
        db.execute("DELETE FROM packages WHERE id IN (SELECT id FROM drop_ids)")

    def _fill_totals(self, result: UpdateResult) -> None:
        for source, attr in ((MODS, "mods"), (GAME, "game")):
            n, res = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(resources), 0) FROM packages WHERE source=?",
                (source,)).fetchone()
            setattr(result, f"{attr}_packages", n)
            setattr(result, f"{attr}_resources", res)

    # Lookups

    def mods_hits(self, key: ResourceKey) -> list[ModsHit]:
        """Every Mods package holding this key, in load order (the last one wins in game)."""
        rows = self.db.execute(
            "SELECT p.path, p.rel, p.load_order, r.offset, r.fsize, r.msize, r.comp "
            "FROM mods_res r JOIN packages p ON p.id = r.pkg "
            "WHERE r.inst=? AND r.type=? AND r.grp=? ORDER BY p.load_order",
            (_s64(key.instance), key.type, key.group))
        return [ModsHit(Path(path), rel, order, dbpf.IndexEntry(key, off, fs, ms, comp))
                for path, rel, order, off, fs, ms, comp in rows]

    def mods_hits_many(self, keys: list[ResourceKey]) -> dict[ResourceKey, list[ModsHit]]:
        """mods_hits for many keys in a few queries (a package's worth); keys without any are
        left out."""
        out: dict[ResourceKey, list[ModsHit]] = {}
        paths: dict[str, Path] = {}
        keys = list(dict.fromkeys(keys))  # a key asked twice would come back twice
        for chunk in _chunks(keys):
            # As in mods_keys_for_instances: candidates from one JSON array, the primary key
            # of mods_res inside the loop.
            rows = self.db.execute(
                "SELECT r.inst, r.type, r.grp, p.path, p.rel, p.load_order, r.offset, r.fsize, "
                "r.msize, r.comp FROM json_each(?) f CROSS JOIN mods_res r "
                "JOIN packages p ON p.id = r.pkg "
                "WHERE r.inst = json_extract(f.value, '$[0]') "
                "AND r.type = json_extract(f.value, '$[1]') "
                "AND r.grp = json_extract(f.value, '$[2]') ORDER BY p.load_order",
                (json.dumps([[_s64(k.instance), k.type, k.group] for k in chunk]),))
            for inst, t, g, path, rel, order, off, fs, ms, comp in rows:
                key = ResourceKey(t, g, inst + (1 << 64) if inst < 0 else inst)
                where = paths.get(path) or paths.setdefault(path, Path(path))
                out.setdefault(key, []).append(
                    ModsHit(where, rel, order, dbpf.IndexEntry(key, off, fs, ms, comp)))
        return out

    def game_keys(self, keys: list[ResourceKey]) -> set[ResourceKey]:
        """Which of these keys the game has."""
        found: set[ResourceKey] = set()
        for chunk in _chunks(list(dict.fromkeys(keys))):
            rows = self.db.execute(
                "SELECT DISTINCT r.inst, r.type, r.grp FROM json_each(?) f CROSS JOIN game_res r "
                "WHERE r.inst = json_extract(f.value, '$[0]') "
                "AND r.type = json_extract(f.value, '$[1]') "
                "AND r.grp = json_extract(f.value, '$[2]')",
                (json.dumps([[_s64(k.instance), k.type, k.group] for k in chunk]),))
            found.update(ResourceKey(t, g, i + (1 << 64) if i < 0 else i) for i, t, g in rows)
        return found

    def game_hits(self, key: ResourceKey) -> list[GameHit]:
        rows = self.db.execute(
            "SELECT p.path, p.rel, p.pack, p.delta FROM game_res r JOIN packages p ON p.id = r.pkg "
            "WHERE r.inst=? AND r.type=? AND r.grp=? ORDER BY p.delta, p.rel",
            (_s64(key.instance), key.type, key.group))
        return [GameHit(Path(path), rel, pack, bool(delta)) for path, rel, pack, delta in rows]

    def keys_with_instance(self, instance: int, type_id: int | None = None) -> list[ResourceKey]:
        """All indexed keys with this instance (and type, if given), whatever their group."""
        inst = _s64(instance)
        if type_id is None:
            rows = self.db.execute(
                "SELECT type, grp FROM mods_res WHERE inst=? UNION "
                "SELECT type, grp FROM game_res WHERE inst=? ORDER BY 1, 2", (inst, inst))
        else:
            rows = self.db.execute(
                "SELECT type, grp FROM mods_res WHERE inst=? AND type=? UNION "
                "SELECT type, grp FROM game_res WHERE inst=? AND type=? ORDER BY 1, 2",
                (inst, type_id, inst, type_id))
        return [ResourceKey(t, g, instance) for t, g in rows]

    def mods_keys_for_instances(self, instances: set[int],
                                types: set[int]) -> list[ResourceKey]:
        """Keys in Mods with any of these instances and one of these types, in one query."""
        if not instances or not types:
            return []
        marks = ",".join("?" * len(types))
        found: set[tuple[int, int, int]] = set()
        for chunk in _chunks([_s64(i) for i in instances], 50_000):
            # The candidates go in as one JSON array, read with json_each, so the lookup writes
            # nothing. CROSS JOIN fixes the loop order: the candidates outside, the primary key
            # of mods_res (instance first) inside. Left to itself SQLite scans all of mods_res.
            found.update(self.db.execute(
                f"SELECT DISTINCT r.type, r.grp, r.inst FROM json_each(?) f CROSS JOIN mods_res r "
                f"WHERE r.inst = f.value AND r.type IN ({marks})",
                (json.dumps(chunk), *types)))
        return [ResourceKey(t, g, i + (1 << 64) if i < 0 else i) for t, g, i in sorted(found)]

    def mods_packages(self, item_types: set[int] | None = None) -> list[ModsPackageInfo]:
        """Every readable Mods package, in load order. With item_types, also how many resources
        of those types each holds (one pass over the Mods resources)."""
        counts: dict[int, int] = {}
        kinds: dict[int, dict[int, int]] = {}
        if item_types:
            marks = ",".join("?" * len(item_types))
            for pkg, type_id, n in self.db.execute(
                    f"SELECT pkg, type, COUNT(*) FROM mods_res WHERE type IN ({marks}) "
                    "GROUP BY pkg, type", tuple(item_types)):
                counts[pkg] = counts.get(pkg, 0) + n
                kinds.setdefault(pkg, {})[type_id] = n
        rows = self.db.execute(
            "SELECT id, path, rel, load_order, size, mtime_ns, resources FROM packages "
            "WHERE source=? AND error IS NULL ORDER BY load_order", (MODS,))
        return [ModsPackageInfo(Path(path), rel, order, size, mtime, resources,
                                counts.get(pkg, 0), kinds.get(pkg, {}))
                for pkg, path, rel, order, size, mtime, resources in rows]

    def mods_instances(self, types: set[int]):
        """Every Mods instance of these types (unsigned; repeats possible), streamed."""
        marks = ",".join("?" * len(types))
        for (inst,) in self.db.execute(
                f"SELECT inst FROM mods_res WHERE type IN ({marks})", tuple(types)):
            yield inst + (1 << 64) if inst < 0 else inst

    def mods_keys_of_types(self, types: set[int]) -> list[ResourceKey]:
        """Every Mods key of these types (one pass over the Mods resources)."""
        marks = ",".join("?" * len(types))
        rows = self.db.execute(
            f"SELECT DISTINCT type, grp, inst FROM mods_res WHERE type IN ({marks}) "
            "ORDER BY type, inst, grp", tuple(types))
        return [ResourceKey(t, g, i + (1 << 64) if i < 0 else i) for t, g, i in rows]

    def rel_of(self, path: Path) -> str | None:
        """A Mods package's path inside Mods, as shown to the user."""
        row = self.db.execute("SELECT rel FROM packages WHERE path=? AND source=?",
                              (str(path), MODS)).fetchone()
        return row[0] if row else None

    def installed_packs(self) -> set[str]:
        """Packs of the indexed game install ("base", "EP03", ...)."""
        return {pack for (pack,) in self.db.execute(
            "SELECT DISTINCT pack FROM packages WHERE source=? AND pack IS NOT NULL", (GAME,))}

    def failed_packages(self) -> list[tuple[str, str]]:
        return list(self.db.execute(
            "SELECT path, error FROM packages WHERE error IS NOT NULL ORDER BY source, rel"))

    def roots(self) -> dict[str, str | None]:
        rows = dict(self.db.execute(
            "SELECT key, value FROM meta WHERE key IN ('mods_root', 'game_root')"))
        return {MODS: rows.get("mods_root"), GAME: rows.get("game_root")}
