"""How CC and tray items are described on screen: labels, captions, usage lines and icons."""

from __future__ import annotations

import time
from pathlib import Path

from traytrim import restypes
from traytrim.names import ItemName
from traytrim.resolve import LocatedRef, ref_label
from traytrim.tray import RefKind, TrayItem

# Section order, as CAS shows things; a lot or room has objects and build items.
GROUP_ORDER = ["Hair", "Clothing", "Accessories", "Makeup", "Eyes", "Skin details", "Body",
               "Other CAS parts", "Skin tone", "Sculpts", "Sliders", "Custom tattoos",
               "Pet coat", "Objects", "Build"]
# Kinds with a thumbnail in their package, and its resource type.
THUMB_TYPES = {RefKind.CAS_PART: restypes.THUM, RefKind.OBJECT: restypes.OBJECT_THUMB,
               RefKind.BUILD: restypes.OBJECT_THUMB}


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def saved_text(item: TrayItem, now: float | None = None) -> str:
    """"Saved 3 Oct", or "Saved 3 Oct 2025" in another year: tells apart tray items with the
    same name (the game saves a new one each time). English months whatever the locale."""
    if not item.modified:
        return ""
    t = time.localtime(item.modified)
    year = time.localtime(now if now is not None else time.time()).tm_year
    when = f"{t.tm_mday} {_MONTHS[t.tm_mon - 1]}" + ("" if t.tm_year == year else f" {t.tm_year}")
    return f"Saved {when}, {t.tm_hour:02}:{t.tm_min:02}"  # the same name is often saved twice a day


def duplicate_names(items: list[TrayItem]) -> set[tuple[int, str]]:
    """(type, name) of the tray items whose name another item of the same kind has too."""
    seen: dict[tuple[int, str], int] = {}
    for item in items:
        key = (item.type, item.name.strip().casefold())
        seen[key] = seen.get(key, 0) + 1
    return {key for key, n in seen.items() if n > 1}


def is_duplicate(item: TrayItem, duplicates: set[tuple[int, str]]) -> bool:
    return (item.type, item.name.strip().casefold()) in duplicates


def item_label(ref: LocatedRef) -> str:
    return ref_label(ref.ref)


def item_subtitle(item: TrayItem, with_price: bool = False) -> str:
    """"40 × 30 · 2 bedrooms · 1 bathroom" for a lot, "6 × 4" for a room."""
    parts = []
    if item.size and all(item.size):
        parts.append(f"{item.size[0]} × {item.size[1]}")
    if with_price and item.price:
        parts.append(f"§{item.price:,}")
    if item.bedrooms:
        parts.append(plural(item.bedrooms, "bedroom"))
    if item.bathrooms:
        parts.append(plural(item.bathrooms, "bathroom"))
    return " · ".join(parts)


def card_caption(ref: LocatedRef) -> str:
    """The small line above a card's title: what kind of item it is. Every card in the Objects
    section is an object, so there it says how often it's placed instead."""
    if ref.ref.kind is RefKind.OBJECT and ref.usage:
        return summarize_uses(set().union(*ref.usage.values()))
    return item_label(ref)


def package_name(ref: LocatedRef) -> str:
    return Path(ref.used.rel).stem if ref.used else ""


def display_name(ref: LocatedRef, name: ItemName | None) -> str:
    """The name of its package made readable when the package is this one item (a CAS part's
    own name is mostly the EA item it was cloned from), else the item's own name, else the
    package's file name."""
    if name is not None and name.package:
        return name.package
    return name.title if name is not None and name.title else package_name(ref)


def full_name(ref: LocatedRef, name: ItemName | None) -> str:
    """`display_name` with the swatch, for lists and messages: one line where a card has two."""
    return name.full if name is not None and name.package else display_name(ref, name)


def usage_text(ref: LocatedRef) -> str:
    """Every use, one Sim per line (for tooltips)."""
    return "\n".join(f"{sim}: {', '.join(sorted(uses))}" for sim, uses in ref.usage_by_name())


def summarize_uses(uses: set[str]) -> str:
    """"Everyday outfit 1" for one or two outfits, "12 outfits" for more, plus other uses."""
    outfits = sorted(u for u in uses if " outfit " in u)
    other = sorted(u for u in uses if " outfit " not in u)
    parts = outfits if len(outfits) <= 2 else [f"{len(outfits)} outfits"]
    return ", ".join(parts + other)


def usage_summary(ref: LocatedRef, with_names: bool = False) -> str:
    """Who uses it and how, short enough for one line. Names are left out for one-Sim reports."""
    if len(ref.usage) == 1 and not with_names:
        return summarize_uses(next(iter(ref.usage.values())))
    return "; ".join(f"{sim}: {summarize_uses(uses)}" for sim, uses in ref.usage_by_name())


_CLOTHING_ICONS = {6: "cat-top", 7: "cat-bottom", 5: "cat-dress", 8: "cat-shoes",
                   36: "cat-shoes", 42: "cat-bottom"}
_GROUP_ICONS = {"Hair": "cat-hair", "Accessories": "cat-accessory", "Makeup": "cat-makeup",
                "Eyes": "cat-eyes", "Skin details": "cat-skin-detail", "Body": "cat-body",
                "Sliders": "cat-slider", "Sculpts": "cat-sculpt", "Skin tone": "cat-skin-tone",
                "Custom tattoos": "cat-skin-detail", "Pet coat": "cat-skin-tone",
                "Objects": "cat-object", "Build": "cat-build"}


def category_icon(ref: LocatedRef) -> str:
    """The category icon shown when an item has no preview image."""
    if ref.ref.group == "Clothing":
        return _CLOTHING_ICONS.get(ref.ref.body_type, "cat-top")
    return _GROUP_ICONS.get(ref.ref.group, "package")


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def group_refs(refs: list[LocatedRef], title=package_name) -> list[tuple[str, list[LocatedRef]]]:
    """CC items in sections, in CAS order (GROUP_ORDER), each sorted by label then `title`."""
    by_group: dict[str, list[LocatedRef]] = {}
    for ref in refs:
        by_group.setdefault(ref.ref.group, []).append(ref)
    order = {g: i for i, g in enumerate(GROUP_ORDER)}
    return [(g, sorted(rs, key=lambda x: (item_label(x), title(x).casefold())))
            for g, rs in sorted(by_group.items(), key=lambda kv: order.get(kv[0], 99))]
