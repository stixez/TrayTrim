"""Where things live: the Sims 4 user folder, the game install, and TrayTrim's own folders.

Detection is best effort and the user can always pick folders by hand. Verified on Windows only;
the macOS and Linux (Steam/Proton) locations follow the documented defaults and are untested.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "TrayTrim"
SIMS4_STEAM_APP_ID = "1222670"

# The user folder is named after the game's language ("The Sims 4", "Die Sims 4", "Les Sims 4"...),
# so we look for any folder under "Electronic Arts" that has a Mods or Tray folder.


def cache_dir() -> Path:
    if os.environ.get("TRAYTRIM_CACHE_DIR"):  # tests and tooling: keep TrayTrim's data apart
        return Path(os.environ["TRAYTRIM_CACHE_DIR"])
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / APP_NAME


def config_dir() -> Path:
    if os.environ.get("TRAYTRIM_CONFIG_DIR"):
        return Path(os.environ["TRAYTRIM_CONFIG_DIR"])
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def desktop_dir() -> Path:
    """The user's Desktop, following a OneDrive redirect on Windows."""
    if sys.platform == "win32":
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "Desktop")
                path = Path(os.path.expandvars(value))
                if path.is_dir():
                    return path
        except (ImportError, OSError):
            pass
    return Path.home() / "Desktop"


def _windows_documents(home: Path, use_registry: bool) -> list[Path]:
    """Documents folder(s), including a OneDrive-redirected one from the registry."""
    found: list[Path] = []
    if use_registry:
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                value, _ = winreg.QueryValueEx(k, "Personal")
                found.append(Path(os.path.expandvars(value)))
        except (ImportError, OSError):
            pass
    return found + [home / "Documents", home / "OneDrive" / "Documents"]


def _steam_roots(home: Path) -> list[Path]:
    return [
        home / ".local" / "share" / "Steam",
        home / ".steam" / "steam",
        home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam",
    ]


def documents_candidates(platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    use_registry = home is None  # tests pass a fake home and must not see the real registry
    home = home or Path.home()
    if platform == "win32":
        return _windows_documents(home, use_registry)
    if platform == "darwin":
        return [home / "Documents"]
    # Linux: the game runs under Proton, so the user folder sits inside the Wine prefix.
    return [root / "steamapps" / "compatdata" / SIMS4_STEAM_APP_ID / "pfx" / "drive_c" / "users"
            / "steamuser" / "Documents" for root in _steam_roots(home)]


def looks_like_user_dir(path: Path) -> bool:
    try:
        return (path / "Mods").is_dir() or (path / "Tray").is_dir()
    except OSError:
        return False


def find_user_dir(platform: str = sys.platform, home: Path | None = None) -> Path | None:
    seen: set[Path] = set()
    for docs in documents_candidates(platform, home):
        ea = docs / "Electronic Arts"
        if ea in seen or not ea.is_dir():
            continue
        seen.add(ea)
        preferred = ea / "The Sims 4"
        if looks_like_user_dir(preferred):
            return preferred
        try:
            children = sorted(ea.iterdir())
        except OSError:  # locked, denied, or an offline OneDrive folder
            continue
        for child in children:
            if looks_like_user_dir(child):
                return child
    return None


def game_dir_candidates(platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    use_registry = home is None
    home = home or Path.home()
    if platform == "win32":
        found: list[Path] = []
        try:
            if not use_registry:
                raise ImportError
            import winreg
            for view in (winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY):
                try:
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Maxis\The Sims 4",
                                        0, winreg.KEY_READ | view) as k:
                        found.append(Path(winreg.QueryValueEx(k, "Install Dir")[0]))
                except OSError:
                    pass
        except ImportError:
            pass
        for env in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env)
            if base:
                found += [Path(base) / "Origin Games" / "The Sims 4",
                          Path(base) / "EA Games" / "The Sims 4",
                          Path(base) / "Steam" / "steamapps" / "common" / "The Sims 4"]
        return found
    if platform == "darwin":
        return [Path("/Applications/The Sims 4.app"), home / "Applications" / "The Sims 4.app"]
    return [root / "steamapps" / "common" / "The Sims 4" for root in _steam_roots(home)]


def game_content_root(game_dir: Path) -> Path | None:
    """The folder holding the game's Data/Client: the install folder itself, or a folder inside
    the macOS app bundle. None when there isn't one."""
    for sub in (".", "Contents", "Contents/Resources"):
        root = game_dir / sub if sub != "." else game_dir
        try:
            if (root / "Data" / "Client").is_dir():
                return root
        except OSError:
            return None
    return None


def looks_like_game_dir(path: Path) -> bool:
    return game_content_root(path) is not None


def find_game_dir(platform: str = sys.platform, home: Path | None = None) -> Path | None:
    for path in game_dir_candidates(platform, home):
        if looks_like_game_dir(path):
            return path
    return None


@dataclass(frozen=True)
class Sims4Folders:
    user_dir: Path | None
    game_dir: Path | None

    @property
    def mods(self) -> Path | None:
        return self.user_dir / "Mods" if self.user_dir else None

    @property
    def tray(self) -> Path | None:
        return self.user_dir / "Tray" if self.user_dir else None


def resolve_user_dir(chosen: str | None) -> Path | None:
    """The folder the user chose while it still looks right; otherwise whatever detection finds."""
    if chosen and looks_like_user_dir(Path(chosen)):
        return Path(chosen)
    return find_user_dir()


def resolve_game_dir(chosen: str | None) -> Path | None:
    if chosen and looks_like_game_dir(Path(chosen)):
        return Path(chosen)
    return find_game_dir()


def resolve_folders(user_dir: str | None = None, game_dir: str | None = None) -> Sims4Folders:
    return Sims4Folders(resolve_user_dir(user_dir), resolve_game_dir(game_dir))
