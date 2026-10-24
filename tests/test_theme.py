"""WCAG contrast targets (4.5:1 text, 3:1 UI), checked for both themes."""

import pytest

pytest.importorskip("PySide6")

from traytrim.gui.theme import DARK, LIGHT


def luminance(hex_colour: str) -> float:
    def channel(c: int) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


TEXT_PAIRS = [  # (foreground, background), need 4.5:1
    ("text", "window"), ("text", "sidebar"), ("text", "surface"), ("text", "selection"),
    ("text_secondary", "window"), ("text_secondary", "sidebar"), ("text_secondary", "surface"),
    ("text_tertiary", "window"), ("text_tertiary", "sidebar"), ("text_tertiary", "surface"),
    ("on_accent", "accent"), ("on_accent", "accent_hover"), ("on_accent", "accent_pressed"),
    ("accent_text", "window"), ("accent_text", "sidebar"),
    ("warning_text", "warning_bg"), ("warning_text", "window"),
    ("success_text", "window"), ("success_text", "sidebar"),
    ("danger_text", "danger_bg"), ("danger_text", "window"), ("text", "danger_bg"),
    ("text_secondary", "selection"), ("text", "surface_hover"), ("text_secondary", "surface_hover"),
    ("window", "text"),  # tooltips are inverted
    ("coffee_text", "coffee_bg"), ("coffee_text", "coffee_hover"),
]

UI_PAIRS = [  # (foreground, background), need 3:1
    ("border_strong", "surface"), ("border_strong", "sidebar"), ("border_strong", "window"),
    ("focus", "window"), ("focus", "sidebar"), ("accent", "window"),
]


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
@pytest.mark.parametrize("fg, bg", TEXT_PAIRS)
def test_text_contrast(palette, fg, bg):
    ratio = contrast(getattr(palette, fg), getattr(palette, bg))
    assert ratio >= 4.5, f"{palette.name}: {fg} on {bg} is {ratio:.2f}:1"


@pytest.mark.parametrize("palette", [LIGHT, DARK], ids=["light", "dark"])
@pytest.mark.parametrize("fg, bg", UI_PAIRS)
def test_ui_contrast(palette, fg, bg):
    ratio = contrast(getattr(palette, fg), getattr(palette, bg))
    assert ratio >= 3.0, f"{palette.name}: {fg} on {bg} is {ratio:.2f}:1"
