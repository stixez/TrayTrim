"""Small building blocks shared by the window's parts."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QLabel, QPushButton, QSizePolicy, QWidget


def label(text: str = "", role: str | None = None, *, wrap: bool = False) -> QLabel:
    w = QLabel(text)
    if role:
        w.setProperty("role", role)
    if wrap:
        w.setWordWrap(True)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
    return w


def link_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("kind", "link")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return b


def set_role(widget, role: str | None) -> None:
    """Change a styled role at runtime; Qt only re-reads the stylesheet when asked."""
    if widget.property("role") == role:
        return
    widget.setProperty("role", role)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class ElidedLabel(QLabel):
    """A one-line label that shortens its text in the middle when there isn't room, so both the
    start and the end of a long path stay visible. The full text is in the tooltip."""

    def __init__(self, text: str = "", role: str | None = None, max_width: int = 360):
        super().__init__()
        if role:
            self.setProperty("role", role)
        self._full = ""
        self.setMaximumWidth(max_width)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._full = text
        self.setToolTip(text)
        super().setText(text)
        self.updateGeometry()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), 80), hint.height())

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideMiddle,
                                             rect.width())
        painter.drawText(rect, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter), text)


def wrap_and_elide(text: str, metrics, width: int, max_lines: int) -> list[str]:
    """Break text into at most max_lines lines that fit `width`, breaking long words if they
    must, and ending the last line with an ellipsis when text is left over."""
    lines: list[str] = []
    rest = text.strip()
    while rest and len(lines) < max_lines:
        if len(lines) == max_lines - 1:
            lines.append(metrics.elidedText(rest, Qt.TextElideMode.ElideRight, width))
            break
        if metrics.horizontalAdvance(rest) <= width:
            lines.append(rest)
            break
        # Longest prefix that fits, preferring to break after a space.
        cut = len(rest)
        while cut > 1 and metrics.horizontalAdvance(rest[:cut]) > width:
            cut -= 1
        space = rest.rfind(" ", 0, cut + 1)
        if space > 0:
            cut = space
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return lines


class ClampedText(QWidget):
    """Text in a fixed number of lines (so cards line up), shortened with an ellipsis."""

    def __init__(self, text: str, lines: int = 2, parent=None):
        super().__init__(parent)
        self._text = text
        self._lines = lines
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(self.fontMetrics().lineSpacing() * lines)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        fm = self.fontMetrics()
        y = fm.ascent()
        for line in wrap_and_elide(self._text, fm, self.width(), self._lines):
            painter.drawText(0, y, line)
            y += fm.lineSpacing()
