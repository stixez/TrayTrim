"""Is there a newer TrayTrim? Asks GitHub for the latest release, once at start, in the
background. Nothing is downloaded or installed: the app offers the release's download for
this platform and the user decides."""

from __future__ import annotations

import json
import platform as host
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from traytrim import __version__

REPO = "stixez/TrayTrim"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_MAX_RESPONSE = 1 << 20  # a release's JSON is a few KB
_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


@dataclass(frozen=True)
class Release:
    version: str          # "0.4.0"
    page: str             # the release on GitHub
    notes: str            # what's new, as written for the release (Markdown)
    download: str         # this platform's file, or the release page if there's none


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.match(text.strip())
    if m is None:
        return None
    return int(m[1]), int(m[2]), int(m[3] or 0)


def is_newer(latest: str, current: str | None = None) -> bool:
    a, b = parse_version(latest), parse_version(current or __version__)
    return a is not None and b is not None and a > b


def _portable() -> bool:
    """A Windows build run from the portable zip: the installer leaves its uninstaller next to
    TrayTrim.exe."""
    if not getattr(sys, "frozen", False):
        return False
    return not any(Path(sys.executable).parent.glob("unins*.exe"))


def _asset_for_platform(assets: list[dict], platform: str = sys.platform,
                        machine: str | None = None, portable: bool = False) -> str | None:
    """The download a user of this platform wants: the installer on Windows (the portable zip
    for a portable copy), the app on macOS, the archive on Linux (see packaging/ for the
    names). None when no build runs on this machine (an Intel Mac, an ARM Linux): they get the
    release page."""
    machine = (machine or host.machine()).lower()
    if platform == "win32":  # x64, which Windows on ARM also runs
        suffixes: tuple[str, ...] = ("-setup.exe", "-windows-portable.zip")
        if portable:
            suffixes = suffixes[::-1]
    elif platform == "darwin":
        suffixes = ("-macos-arm64.zip",) if machine in ("arm64", "aarch64") else ()
    else:
        suffixes = ("-linux-x86_64.tar.gz",) if machine in ("x86_64", "amd64") else ()
    for suffix in suffixes:
        for asset in assets:
            name, url = asset.get("name"), asset.get("browser_download_url")
            if isinstance(name, str) and isinstance(url, str) and name.endswith(suffix):
                return url
    return None


def parse_release(data: object, platform: str = sys.platform,
                  machine: str | None = None, portable: bool | None = None) -> Release | None:
    """The release GitHub's API describes, or None if it isn't one we can offer."""
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        return None
    tag = data.get("tag_name")
    if not isinstance(tag, str) or parse_version(tag) is None:
        return None
    page = data.get("html_url") if isinstance(data.get("html_url"), str) else RELEASES_PAGE
    if not page.startswith(f"https://github.com/{REPO}/"):
        page = RELEASES_PAGE
    notes = data.get("body") if isinstance(data.get("body"), str) else ""
    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    download = _asset_for_platform([a for a in assets if isinstance(a, dict)], platform, machine,
                                   _portable() if portable is None else portable)
    if download is None or not download.startswith(f"https://github.com/{REPO}/"):
        download = page
    return Release(tag.lstrip("v"), page, notes.strip(), download)


def latest_release(timeout: float = 8.0) -> Release | None:
    """The latest published release, or None when GitHub can't be reached (offline, rate
    limited): a failed check is silent, the next start tries again."""
    request = urllib.request.Request(LATEST_API, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"TrayTrim/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_RESPONSE + 1)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if len(raw) > _MAX_RESPONSE:
        return None
    try:
        return parse_release(json.loads(raw))
    except ValueError:
        return None


def available_update(skipped: str | None = None) -> Release | None:
    """A release newer than this one that the user hasn't chosen to skip."""
    release = latest_release()
    if release is None or not is_newer(release.version) or release.version == skipped:
        return None
    return release
