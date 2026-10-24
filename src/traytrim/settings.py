"""User settings, stored as JSON in TrayTrim's config folder."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from traytrim.paths import config_dir

THEMES = ("system", "light", "dark")
TRAY_KINDS = ("household", "lot", "room", "mods")
# The choices settings keep; gallery.py and mods_list.py name them (one list, here).
TRAY_SORTS = ("newest", "oldest", "name", "name_desc", "sims", "largest")
MODS_SORTS = ("name", "name_desc", "size", "size_asc", "newest", "items")
MODS_SHOWS = ("all", "cas", "objects", "build", "sliders", "none")


@dataclass
class Settings:
    user_dir: str | None = None     # Sims 4 user folder (holds Mods and Tray), when picked by hand
    game_dir: str | None = None     # game install, when picked by hand
    output_dir: str | None = None   # last folder exports went to
    theme: str = "system"           # "system", "light" or "dark"
    window_geometry: str | None = None
    tray_kind: str = "household"    # which kind of tray item the gallery shows, or "mods"
    tray_sort: str = "newest"       # the households, lots or rooms list (see TRAY_SORTS)
    mods_sort: str = "name"         # the Mods list: "name" is load order (see MODS_SORTS)
    mods_show: str = "all"          # which Mods packages are listed (see MODS_SHOWS)
    skipped_version: str | None = None  # an update the user chose not to be asked about again
    export_test_kit: bool = True    # exports make an in-game test kit unless turned off here
    check_updates: bool = True      # ask GitHub for a newer release when the app starts

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        path = path or default_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        settings = cls()
        if not isinstance(data, dict):
            return settings
        # Keep a value only if it has the right type, so a hand-edited or damaged file can't
        # stop the app from starting.
        for f in fields(cls):
            value = data.get(f.name)
            if f.type == "bool":  # a switch: true or false, nothing else
                if isinstance(value, bool):
                    setattr(settings, f.name, value)
                continue
            allowed = {"theme": THEMES, "tray_kind": TRAY_KINDS, "tray_sort": TRAY_SORTS,
                       "mods_sort": MODS_SORTS, "mods_show": MODS_SHOWS}.get(f.name)
            if isinstance(value, str) and (allowed is None or value in allowed):
                setattr(settings, f.name, value)
        return settings

    def save(self, path: Path | None = None) -> None:
        path = path or default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def default_path() -> Path:
    return config_dir() / "settings.json"
