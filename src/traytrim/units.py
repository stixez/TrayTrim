"""Sizes for people: "740 KB", "31.9 MB", "5.32 GB".

Units are 1024-based, the way Windows Explorer (and Sims 4 Tray Importer) show file sizes, so
the numbers match what the user sees elsewhere.
"""

from __future__ import annotations


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit, scale in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3)):
        value = n / scale
        if value < 1000 or unit == "GB":
            if unit == "GB":
                return f"{value:.2f} GB"
            return f"{value:.0f} {unit}" if value >= 100 else f"{value:.1f} {unit}"
    raise AssertionError


def percent_smaller(small: int, large: int) -> str | None:
    """"99.4% smaller", or None when there's nothing meaningful to say."""
    if large <= 0 or small >= large:
        return None
    saved = 100 * (1 - small / large)
    return f"{saved:.1f}% smaller" if saved < 99.95 else "over 99.9% smaller"
