"""Small building blocks shared by the window's parts."""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QAbstractButton, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QSizePolicy, QVBoxLayout, QWidget)

from traytrim.gui import icons, theme
from traytrim.gui.theme import SPACE


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


class IconLabel(QLabel):
    """One of our icons in a theme colour token, redrawn when the theme changes."""

    def __init__(self, name: str, token: str, size: int = 20, parent=None):
        super().__init__(parent)
        self._name, self._token, self._size = name, token, size
        self.setFixedSize(size, size)
        self.refresh()

    def refresh(self) -> None:
        colour = getattr(theme.current(), self._token)
        self.setPixmap(icons.pixmap(self._name, colour, self._size, self.devicePixelRatioF()))


class SimChip(QAbstractButton):
    """A Sim the user can include or leave out: a rounded chip with a tick when included."""

    HEIGHT = 32

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(f"Include {text}")
        self._hover = False

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        return QSize(SPACE["m"] + 16 + SPACE["s"] + fm.horizontalAdvance(self.text())
                     + SPACE["l"], self.HEIGHT)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        radius = r.height() / 2
        checked = self.isChecked()
        fill = p.selection if checked else (p.surface_hover if self._hover else p.surface)
        border = p.accent if checked else p.border_strong
        painter.setBrush(QColor(fill))
        painter.setPen(QPen(QColor(border), 1))
        painter.drawRoundedRect(r, radius, radius)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(p.focus), 2))
            painter.drawRoundedRect(r.adjusted(1, 1, -1, -1), radius - 1, radius - 1)
        x = SPACE["m"]
        if checked:
            img = icons.image("check", p.accent_text, 16, self.devicePixelRatioF())
            painter.drawImage(QRectF(x, (self.height() - 16) / 2, 16, 16), img)
        else:
            painter.setPen(QPen(QColor(p.border_strong), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QRectF(x + 2.5, (self.height() - 11) / 2, 11, 11))
        painter.setPen(QColor(p.text if checked else p.text_secondary))
        painter.setFont(self.font())
        painter.drawText(QRectF(x + 16 + SPACE["s"], 0, self.width(), self.height()),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.text())


class Notice(QWidget):
    """A calm, one-line notice with an icon, and details that open on request.

    A detail row is (item, info, tooltip) or (item, info, tooltip, (link text, callback));
    `actions` are links next to Show, like Copy list. `expanded` and `on_expand` let the view
    keep the details open when the notice is rebuilt."""

    def __init__(self, icon_name: str, token: str, title: str, body: str,
                 details: list[tuple] | None = None,
                 actions: list[tuple[str, object]] | None = None, expanded: bool = False,
                 on_expand=None, parent=None):
        super().__init__(parent)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, SPACE["m"], 0, SPACE["m"])
        outer.setSpacing(SPACE["m"])
        outer.addWidget(IconLabel(icon_name, token), 0, Qt.AlignmentFlag.AlignTop)
        col = QVBoxLayout()
        col.setSpacing(SPACE["xs"])
        head = QHBoxLayout()
        head.setSpacing(SPACE["m"])
        head.addWidget(label(title, "noticeTitle"), 1)
        col.addLayout(head)
        col.addWidget(label(body, "secondary", wrap=True))
        outer.addLayout(col, 1)
        if details:
            box = QWidget()
            grid = QGridLayout(box)
            grid.setContentsMargins(0, SPACE["s"], 0, 0)
            grid.setHorizontalSpacing(SPACE["l"])
            grid.setVerticalSpacing(SPACE["xs"])
            grid.setColumnStretch(1, 1)
            for i, (what, info, tip, *action) in enumerate(details):
                a, b = label(what, "detailItem"), label(info, "secondary", wrap=True)
                a.setToolTip(tip)
                b.setToolTip(tip)
                grid.addWidget(a, i, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(b, i, 1)
                if action:
                    text, callback = action[0]
                    link = link_button(text)
                    link.setAccessibleName(f"{text}: {what}, {info}")
                    link.clicked.connect(lambda _c=False, f=callback, w=link: f(w))
                    grid.addWidget(link, i, 2, Qt.AlignmentFlag.AlignTop)
            box.setVisible(expanded)
            col.addWidget(box)
        for text, callback in actions or []:
            link = link_button(text)
            link.setAccessibleName(f"{text}: {title}")
            link.clicked.connect(lambda _c=False, f=callback, w=link: f(w))
            head.addWidget(link, 0, Qt.AlignmentFlag.AlignTop)
        if details:
            toggle = link_button("Hide" if expanded else "Show")
            toggle.setAccessibleName(f"Show details: {title}")

            def flip() -> None:
                box.setVisible(not box.isVisible())
                toggle.setText("Hide" if box.isVisible() else "Show")
                if on_expand is not None:
                    on_expand(box.isVisible())

            toggle.clicked.connect(flip)
            head.addWidget(toggle, 0, Qt.AlignmentFlag.AlignTop)
