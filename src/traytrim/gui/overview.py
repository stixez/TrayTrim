"""The right side when nothing is picked yet: what's in the Tray and Mods, and where to start."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QGridLayout, QHBoxLayout, QSizePolicy,
                               QVBoxLayout, QWidget)

from traytrim.gui import icons, theme
from traytrim.gui.theme import RADIUS, SPACE, TYPE
from traytrim.gui.widgets import label


class StatTile(QWidget):
    """A number in large type over what it counts: "42 / households"."""

    def __init__(self, number: str, what: str, parent=None):
        super().__init__(parent)
        self.setObjectName("statTile")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        col = QVBoxLayout(self)
        col.setContentsMargins(SPACE["l"], SPACE["m"], SPACE["l"], SPACE["m"])
        col.setSpacing(0)
        self.number = label(number, "headline")
        col.addWidget(self.number)
        col.addWidget(label(what, "secondary"))
        self.setAccessibleName(f"{number} {what}")


class ActionCard(QAbstractButton):
    """A way to start: an icon, what it does and a line about it, on a card that lights up
    under the pointer."""

    def __init__(self, icon_name: str, title: str, text: str, on_click: Callable[[], None],
                 parent=None):
        super().__init__(parent)
        self._icon, self._text = icon_name, text
        self.setText(title)
        self.setAccessibleName(title)
        self.setAccessibleDescription(text)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)  # repaints on hover
        self.clicked.connect(on_click)

    def sizeHint(self) -> QSize:  # noqa: N802
        body = QFontMetrics(theme.ui_font("caption"))
        return QSize(220, SPACE["l"] + 24 + SPACE["s"] + TYPE["body"] + 8 + 2 * body.lineSpacing()
                     + SPACE["l"])

    def paintEvent(self, _event) -> None:  # noqa: N802
        from traytrim.gui.widgets import wrap_and_elide

        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        enabled = self.isEnabled()
        pen = QPen(QColor(p.focus if self.hasFocus() else
                          p.border_strong if self.underMouse() and enabled else p.divider))
        pen.setWidthF(2 if self.hasFocus() else 1)
        painter.setPen(pen)
        painter.setBrush(QColor(p.surface_hover if self.underMouse() and enabled else p.window))
        painter.drawRoundedRect(r, RADIUS["card"], RADIUS["card"])
        colour = p.text if enabled else p.text_tertiary
        x, y = SPACE["l"], SPACE["l"]
        painter.drawImage(QRectF(x, y, 24, 24),
                          icons.image(self._icon, colour, 24, self.devicePixelRatioF()))
        y += 24 + SPACE["s"]
        width = self.width() - 2 * SPACE["l"]
        painter.setFont(theme.ui_font("body", QFont.Weight.DemiBold))
        painter.setPen(QColor(colour))
        painter.drawText(QRectF(x, y, width, TYPE["body"] + 8),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self.text())
        y += TYPE["body"] + 8
        font = theme.ui_font("caption")
        painter.setFont(font)
        painter.setPen(QColor(p.text_secondary if enabled else p.text_tertiary))
        metrics = QFontMetrics(font)
        for line in wrap_and_elide(self._text, metrics, int(width), 2):
            painter.drawText(QRectF(x, y, width, metrics.lineSpacing()),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, line)
            y += metrics.lineSpacing()


class Overview(QWidget):
    """Counts of what TrayTrim found, and cards for the ways to start."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xxl"], SPACE["xxl"], SPACE["xxl"], SPACE["xxl"])
        outer.addStretch(2)
        self.box = QWidget()
        self.box.setMaximumWidth(760)
        self.col = QVBoxLayout(self.box)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(SPACE["s"])
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(self.box, 4)
        centred.addStretch(1)
        outer.addLayout(centred)
        outer.addStretch(3)
        self.cards: list[ActionCard] = []

    def fill(self, title: str, line: str, stats: list[tuple[str, str]],
             actions: list[tuple[str, str, str, Callable[[], None], bool]], hint: str) -> None:
        """actions: (icon, title, text, on_click, enabled)."""
        _clear(self.col)
        self.col.addWidget(label(title, "headline", wrap=True))
        self.col.addWidget(label(line, "secondary", wrap=True))
        if stats:
            self.col.addSpacing(SPACE["l"])
            row = QHBoxLayout()
            row.setSpacing(SPACE["m"])
            for number, what in stats:
                row.addWidget(StatTile(number, what), 1)
            self.col.addLayout(row)
        self.col.addSpacing(SPACE["xl"])
        self.col.addWidget(label("Or start with", "noticeTitle"))
        self.col.addSpacing(SPACE["xs"])
        grid = QGridLayout()
        grid.setHorizontalSpacing(SPACE["m"])
        grid.setVerticalSpacing(SPACE["m"])
        self.cards = []
        for i, (icon, name, text, on_click, enabled) in enumerate(actions):
            card = ActionCard(icon, name, text, on_click)
            card.setEnabled(enabled)
            self.cards.append(card)
            grid.addWidget(card, 0, i)
        self.col.addLayout(grid)
        if hint:
            self.col.addSpacing(SPACE["m"])
            self.col.addWidget(label(hint, "secondary", wrap=True))


def _clear(layout) -> None:
    """Empty a layout. Its widgets are hidden at once: deleted later, they'd show through
    what replaces them until then."""
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().hide()
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())
