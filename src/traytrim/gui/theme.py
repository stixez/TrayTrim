"""Design tokens and the stylesheet built from them.

Every colour a widget uses comes from here. Colours carry meaning (accent, warning, danger) or
they are neutral; contrast targets are checked in tests/test_theme.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources

from PySide6.QtGui import QColor, QFont, QFontDatabase, QPalette

UI_FAMILY = "IBM Plex Sans"
MONO_FAMILY = "IBM Plex Mono"

# Pixel sizes; Qt scales them for the display.
TYPE = {
    "caption": 12,
    "body": 14,
    "title": 18,
    "headline": 24,
    "hero": 36,
}

SPACE = {"xs": 4, "s": 8, "m": 12, "l": 16, "xl": 24, "xxl": 32}
RADIUS = {"control": 4, "card": 8}


@dataclass(frozen=True)
class Palette:
    name: str
    window: str          # the main content area
    sidebar: str         # the gallery panel, a step away from the content
    surface: str         # inputs and raised elements
    surface_hover: str
    text: str
    text_secondary: str
    text_tertiary: str   # placeholders, hints; still meets 4.5:1 on window
    divider: str         # hairlines between regions (decorative, no contrast target)
    border_strong: str   # input borders, meets 3:1
    accent: str          # primary action fill
    accent_hover: str
    accent_pressed: str
    on_accent: str
    accent_text: str     # accent used as text or icon on window
    success_text: str    # "verified", and nothing else
    warning_text: str
    warning_bg: str
    danger_text: str
    danger_bg: str
    selection: str       # selected row background
    focus: str


# "Ink": a monochrome palette. The accent is ink itself (near-black in light, near-white in
# dark), so the user's CC images carry the colour; the only hues left are semantic: green for
# verified, amber for warnings and conflicts, red for missing and failed.
LIGHT = Palette(
    name="light",
    window="#FFFFFF",
    sidebar="#F5F5F5",
    surface="#FFFFFF",
    surface_hover="#EDEDED",
    text="#171717",
    text_secondary="#4D4D4D",
    text_tertiary="#666666",
    divider="#E5E5E5",
    border_strong="#808080",
    accent="#171717",
    accent_hover="#333333",
    accent_pressed="#000000",
    on_accent="#FFFFFF",
    accent_text="#171717",
    success_text="#1B7034",
    warning_text="#7A4F00",
    warning_bg="#FDF3DC",
    danger_text="#A8231C",
    danger_bg="#FCE9E7",
    selection="#EBEBEB",
    focus="#171717",
)

DARK = Palette(
    name="dark",
    window="#1A1A1A",
    sidebar="#141414",
    surface="#242424",
    surface_hover="#2E2E2E",
    text="#EDEDED",
    text_secondary="#ABABAB",
    text_tertiary="#8F8F8F",
    divider="#2E2E2E",
    border_strong="#767676",
    accent="#EDEDED",
    accent_hover="#FFFFFF",
    accent_pressed="#D4D4D4",
    on_accent="#141414",
    accent_text="#EDEDED",
    success_text="#6CD088",
    warning_text="#E6B54A",
    warning_bg="#3A2F16",
    danger_text="#F28B82",
    danger_bg="#3D2220",
    selection="#303030",
    focus="#EDEDED",
)


def current() -> Palette:
    """The palette in use, for widgets that paint themselves."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    return DARK if app is not None and app.property("traytrim_palette") == "dark" else LIGHT


def load_fonts() -> None:
    """Register the bundled Plex fonts with Qt. Safe to call more than once."""
    fonts = resources.files("traytrim.gui") / "assets" / "fonts"
    for entry in fonts.iterdir():
        if entry.name.endswith(".ttf"):
            QFontDatabase.addApplicationFontFromData(entry.read_bytes())


def ui_font(size: str = "body", weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(UI_FAMILY)
    f.setPixelSize(TYPE[size])
    f.setWeight(weight)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def mono_font(size: str = "caption") -> QFont:
    f = QFont(MONO_FAMILY)
    f.setPixelSize(TYPE[size])
    f.setStyleHint(QFont.StyleHint.Monospace)
    return f


def qpalette(p: Palette) -> QPalette:
    """A QPalette so widgets we don't style by hand (menus, tooltips, dialogs) still match."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.window,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.surface,
        QPalette.ColorRole.AlternateBase: p.sidebar,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.PlaceholderText: p.text_tertiary,
        QPalette.ColorRole.Button: p.surface,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.Highlight: p.selection,
        QPalette.ColorRole.HighlightedText: p.text,
        QPalette.ColorRole.ToolTipBase: p.text,
        QPalette.ColorRole.ToolTipText: p.window,
        QPalette.ColorRole.Link: p.accent_text,
        QPalette.ColorRole.Mid: p.divider,
        QPalette.ColorRole.Dark: p.border_strong,
        QPalette.ColorRole.Accent: p.accent,
    }
    for role, colour in roles.items():
        pal.setColor(role, QColor(colour))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.text_tertiary))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.text_tertiary))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(p.text_tertiary))
    return pal


def stylesheet(p: Palette) -> str:
    from traytrim.gui.icons import file_for_stylesheet

    r, s = RADIUS, SPACE
    chevron_right = file_for_stylesheet("chevron-right", p.text_secondary, 16)
    chevron_down = file_for_stylesheet("chevron-down", p.text_secondary, 16)
    return f"""
    QWidget {{
        color: {p.text};
        font-family: "{UI_FAMILY}";
        font-size: {TYPE['body']}px;
    }}
    QMainWindow, #content {{ background: {p.window}; }}
    #sidebar {{ background: {p.sidebar}; }}
    #sidebarDivider {{ background: {p.divider}; }}

    QLabel[role="headline"] {{ font-size: {TYPE['headline']}px; font-weight: 600; }}
    QLabel[role="title"] {{ font-size: {TYPE['title']}px; font-weight: 600; }}
    QLabel[role="secondary"] {{ color: {p.text_secondary}; }}
    QLabel[role="caption"] {{ color: {p.text_secondary}; font-size: {TYPE['caption']}px; }}
    QLabel[role="hero"] {{ font-size: {TYPE['hero']}px; font-weight: 600; }}
    QLabel[role="heroPending"] {{ font-size: {TYPE['title']}px; color: {p.text_secondary}; }}
    QLabel[role="saving"] {{ color: {p.accent_text}; font-weight: 500; }}
    QLabel[role="noticeTitle"] {{ font-weight: 500; }}
    QLabel[role="detailItem"] {{ font-weight: 500; }}
    QFrame#divider {{ background: {p.divider}; border: none; }}
    QLabel[role="warning"] {{ color: {p.warning_text}; font-size: {TYPE['caption']}px; }}
    QLabel[role="danger"] {{ color: {p.danger_text}; font-size: {TYPE['caption']}px; }}
    QLabel[role="mono"] {{
        font-family: "{MONO_FAMILY}"; font-size: {TYPE['caption']}px; color: {p.text_secondary};
    }}

    QLineEdit {{
        background: {p.surface};
        border: 1px solid {p.border_strong};
        border-radius: {r['control']}px;
        padding: {s['s'] - 1}px {s['m'] - 1}px;
        selection-background-color: {p.selection};
        selection-color: {p.text};
    }}
    QLineEdit:disabled {{ background: transparent; border-color: {p.divider}; }}
    QLineEdit:focus {{ border: 2px solid {p.focus}; padding: {s['s'] - 2}px {s['m'] - 2}px; }}

    QPushButton {{
        background: {p.surface};
        border: 1px solid {p.border_strong};
        border-radius: {r['control']}px;
        padding: {s['s'] - 1}px {s['l'] - 1}px;
        font-weight: 500;
    }}
    QPushButton:hover {{ background: {p.surface_hover}; }}
    QPushButton:focus {{ border: 2px solid {p.focus}; padding: {s['s'] - 2}px {s['l'] - 2}px; }}
    QPushButton[kind="primary"] {{
        background: {p.accent}; color: {p.on_accent}; border: 1px solid {p.accent};
    }}
    QPushButton[kind="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton[kind="primary"]:pressed {{ background: {p.accent_pressed}; }}
    QPushButton[kind="primary"]:focus {{ border: 2px solid {p.text}; }}
    QPushButton[kind="link"] {{
        background: transparent; border: none; padding: 0; color: {p.accent_text};
        text-decoration: underline;
    }}
    QPushButton[kind="link"]:focus {{ border: none; padding: 0; outline: 2px solid {p.focus}; }}
    QPushButton:disabled {{ color: {p.text_tertiary}; border-color: {p.divider}; background: {p.surface}; }}

    QScrollArea, QScrollArea > QWidget > QWidget#content {{ background: {p.window}; }}
    QListView {{ background: transparent; border: none; outline: none; }}

    QFrame#ccCard {{
        background: {p.window}; border: 1px solid {p.divider}; border-radius: {r['card']}px;
    }}
    QFrame#ccCard:hover {{ border-color: {p.border_strong}; }}
    QFrame#ccCard[excluded="true"] {{ background: {p.sidebar}; border-style: dashed; }}
    QFrame#ccCard:focus {{ border: 2px solid {p.focus}; }}
    QFrame#ccCard QLabel {{ border: none; background: transparent; }}

    QFrame#dangerPanel {{ background: {p.danger_bg}; border-radius: {r['card']}px; }}
    QLabel[role="dangerTitle"] {{ color: {p.danger_text}; font-weight: 600; }}
    QLabel[role="dangerBody"] {{ color: {p.text}; }}
    QLabel[role="dangerItem"] {{ color: {p.text}; font-weight: 500; }}

    QPushButton[kind="segment"] {{
        background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: 0;
        padding: {s['xs'] + 1}px {s['l']}px; font-weight: 500; color: {p.text_secondary};
    }}
    QPushButton[kind="segment"][position="first"] {{
        border-top-left-radius: {r['control']}px; border-bottom-left-radius: {r['control']}px;
        border-right: none;
    }}
    QPushButton[kind="segment"][position="middle"] {{ border-right: none; }}
    QPushButton[kind="segment"][position="last"] {{
        border-top-right-radius: {r['control']}px; border-bottom-right-radius: {r['control']}px;
    }}
    QPushButton[kind="segment"]:checked {{
        background: {p.selection}; color: {p.text}; border-color: {p.border_strong};
    }}
    QPushButton[kind="segment"]:focus {{ border: 2px solid {p.focus}; }}

    QCheckBox {{ spacing: {s['s']}px; }}

    QTreeWidget {{
        background: {p.window}; border: none; outline: none;
        selection-background-color: {p.selection}; selection-color: {p.text};
    }}
    QTreeWidget::item {{ padding: {s['xs']}px 0; }}
    QTreeView::branch {{ background: transparent; }}
    QTreeView::branch:has-children:closed {{ image: url("{chevron_right}"); }}
    QTreeView::branch:has-children:open {{ image: url("{chevron_down}"); }}
    QHeaderView::section {{
        background: {p.window}; color: {p.text_secondary}; border: none;
        border-bottom: 1px solid {p.divider}; padding: {s['xs']}px {s['s']}px;
        font-size: {TYPE['caption']}px;
    }}

    QProgressBar {{ background: {p.divider}; border: none; border-radius: 2px; }}
    QProgressBar::chunk {{ background: {p.accent}; border-radius: 2px; }}

    QDialog {{ background: {p.window}; }}
    QPlainTextEdit {{
        font-family: "{MONO_FAMILY}"; font-size: {TYPE['caption']}px;
        background: {p.sidebar}; border: 1px solid {p.divider}; border-radius: {r['control']}px;
        padding: {s['s']}px; color: {p.text};
        selection-background-color: {p.selection}; selection-color: {p.text};
    }}

    QStatusBar {{
        background: {p.sidebar};
        border-top: 1px solid {p.divider};
        color: {p.text_secondary};
        font-size: {TYPE['caption']}px;
    }}
    QStatusBar::item {{ border: none; }}

    QToolTip {{
        background: {p.text}; color: {p.window}; border: none;
        padding: {s['xs']}px {s['s']}px; font-size: {TYPE['caption']}px;
    }}

    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {p.border_strong}; border-radius: 3px; min-height: 32px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    """
