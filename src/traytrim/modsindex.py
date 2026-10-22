"""Which packages in the Mods folder the game loads, and in what order.

Depth: the game reads Mods/Resource.cfg, whose "PackedFile */*/*.package" lines say how many
folder levels deep it looks (the default file goes 5 levels deep). Packages deeper than that
are not loaded by the game, so they are left out and reported.

Load order (an assumption, not documented by EA): packages load in alphabetical order of their
path inside Mods, compared case-insensitively, and when two packages hold the same resource the
one loaded later wins. Conflict reports say so.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DEPTH = 5
_PACKED_FILE = re.compile(r"^\s*PackedFile\s+(\S+)", re.IGNORECASE)


@dataclass(frozen=True)
class ModsPackage:
    path: Path
    rel: str          # path inside Mods with forward slashes, as shown to the user
    load_order: int


@dataclass(frozen=True)
class ModsScan:
    packages: list[ModsPackage]
    too_deep: list[str]  # relative paths the game won't load because of Resource.cfg depth
    depth: int


def max_depth(mods_root: Path) -> int:
    """Deepest folder level Resource.cfg tells the game to look in; the default when unreadable."""
    try:
        text = (mods_root / "Resource.cfg").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return DEFAULT_DEPTH
    depths = [m.group(1).count("/") for line in text.splitlines()
              if (m := _PACKED_FILE.match(line)) and m.group(1).lower().endswith(".package")]
    return max(depths) if depths else DEFAULT_DEPTH


def load_order_key(rel: str) -> str:
    return rel.casefold()


class ScanCancelled(Exception):
    pass


def scan_mods(mods_root: Path, cancel: threading.Event | None = None) -> ModsScan:
    depth = max_depth(mods_root)
    found: list[tuple[str, Path]] = []
    too_deep: list[str] = []
    root = os.fspath(mods_root)
    for dirpath, _dirnames, filenames in os.walk(root):
        if cancel is not None and cancel.is_set():
            raise ScanCancelled
        rel_dir = os.path.relpath(dirpath, root)
        level = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        for name in filenames:
            if not name.lower().endswith(".package"):
                continue
            rel = name if level == 0 else f"{rel_dir.replace(os.sep, '/')}/{name}"
            if level > depth:
                too_deep.append(rel)
            else:
                found.append((rel, Path(dirpath, name)))
    found.sort(key=lambda item: load_order_key(item[0]))
    packages = [ModsPackage(path, rel, i) for i, (rel, path) in enumerate(found)]
    return ModsScan(packages, sorted(too_deep, key=load_order_key), depth)
