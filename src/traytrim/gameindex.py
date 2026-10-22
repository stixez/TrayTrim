"""The game's own packages: base game (Data), packs (EPxx, GPxx, SPxx, FPxx) and their delta
updates (Delta/<pack>). Anything a household needs that lives here is EA content and is never
copied into an export; the pack it comes from tells the recipient which packs they need.

Only those folders are read. The install folder can hold other .package files (for example an
unused Mods folder) that the game doesn't load as game content.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from traytrim.paths import game_content_root

BASE_GAME = "base"
_PACK = re.compile(r"^(EP|GP|SP|FP)\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class GamePackage:
    path: Path
    rel: str      # inside the install folder, forward slashes
    pack: str     # "base" or a pack code such as "EP03"
    delta: bool   # from the Delta folder (updates that override the full builds)


def _walk_packages(folder: Path, root: Path) -> list[tuple[str, Path]]:
    out = []
    for dirpath, _dirs, files in os.walk(folder):
        for name in files:
            if name.lower().endswith(".package"):
                path = Path(dirpath, name)
                out.append((path.relative_to(root).as_posix(), path))
    return out


def _pack_of(folder_name: str) -> str | None:
    if folder_name.lower() == "data":
        return BASE_GAME
    if _PACK.match(folder_name):
        return folder_name.upper()
    return None


def scan_game(game_dir: Path) -> list[GamePackage]:
    root = game_content_root(game_dir) or game_dir
    found: list[GamePackage] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        if entry.name.lower() == "delta":
            for sub in sorted(p for p in entry.iterdir() if p.is_dir()):
                pack = _pack_of(sub.name)
                if pack:
                    found += [GamePackage(p, rel, pack, True)
                              for rel, p in _walk_packages(sub, root)]
        elif pack := _pack_of(entry.name):
            found += [GamePackage(p, rel, pack, False) for rel, p in _walk_packages(entry, root)]
    return found
