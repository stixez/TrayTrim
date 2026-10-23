"""Small building blocks shared by the window's parts."""

from __future__ import annotations

import html
import math
import re

from PySide6.QtCore import QElapsedTimer, QEvent, QObject, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtGui import Qt as GuiQt
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QGridLayout, QHBoxLayout,
                               QLabel, QProgressBar, QPushButton, QSizePolicy, QToolTip,
                               QVBoxLayout, QWidget)

from traytrim import SUPPORT_URL
from traytrim.gui import icons, system, theme
from traytrim.gui.theme import SPACE


_BREAK_AFTER = re.compile(r"([_\-./\\])(?=\S)")


def breakable(text: str) -> str:
    """Text that can wrap inside long names: a zero-width space after _ - . / and \\, the way
    package and file names are joined ("Soft_Bob_Mesh_v2.package"). Labels wrap only at spaces,
    so a name without any would push the page wider than the window."""
    return _BREAK_AFTER.sub("\\1\u200b", text)


class TextLabel(QLabel):
    """A label for text (never markup); when it wraps, long names can wrap too (breakable)."""

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        super().setText(breakable(text) if self.wordWrap() else text)


def label(text: str = "", role: str | None = None, *, wrap: bool = False) -> QLabel:
    w = TextLabel()
    # Always plain: names come from CC packages, tray files and received zips, and Qt would
    # render text that looks like HTML as HTML.
    w.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        w.setProperty("role", role)
    if wrap:
        w.setWordWrap(True)
    w.setText(text)
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
        self.setTextFormat(Qt.TextFormat.PlainText)  # paths can hold anything; see label()
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


PAGE_DETAILS = 200  # detail rows a notice shows at a time
DETAIL_ITEM_WIDTH = 320


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
        head.addWidget(label(title, "noticeTitle", wrap=True), 1)
        col.addLayout(head)
        col.addWidget(label(body, "secondary", wrap=True))
        outer.addLayout(col, 1)
        if details:
            box = QWidget()
            box_col = QVBoxLayout(box)
            box_col.setContentsMargins(0, SPACE["s"], 0, 0)
            box_col.setSpacing(SPACE["s"])
            grid = QGridLayout()
            grid.setHorizontalSpacing(SPACE["l"])
            grid.setVerticalSpacing(SPACE["xs"])
            grid.setColumnStretch(1, 1)
            box_col.addLayout(grid)
            self._details, self._grid, self._shown = details, grid, 0
            # Rows a page at a time: thousands of them (a zip of a whole Mods folder) take
            # seconds and hundreds of MB to lay out at once.
            self._more = ShowMore(len(details), self._add_rows, PAGE_DETAILS, "rows")
            box_col.addWidget(self._more)
            self._add_rows()
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

    def _add_rows(self) -> None:
        start = self._shown
        for i, (what, info, tip, *action) in enumerate(
                self._details[start:start + PAGE_DETAILS], start):
            a, b = label(what, "detailItem", wrap=True), label(info, "secondary", wrap=True)
            # As wide as it needs up to a limit; a longer package path wraps instead.
            font = QFont(a.font())
            font.setWeight(QFont.Weight.Medium)  # role "detailItem" in the stylesheet
            wanted = QFontMetrics(font).horizontalAdvance(what) + SPACE["s"]
            a.setMinimumWidth(min(wanted, DETAIL_ITEM_WIDTH))
            a.setMaximumWidth(DETAIL_ITEM_WIDTH)
            a.setToolTip(tip)
            b.setToolTip(tip)
            self._grid.addWidget(a, i, 0, Qt.AlignmentFlag.AlignTop)
            self._grid.addWidget(b, i, 1)
            if action:
                text, callback = action[0]
                link = link_button(text)
                link.setAccessibleName(f"{text}: {what}, {info}")
                link.clicked.connect(lambda _c=False, f=callback, w=link: f(w))
                self._grid.addWidget(link, i, 2, Qt.AlignmentFlag.AlignTop)
        self._shown = min(start + PAGE_DETAILS, len(self._details))
        self._more.set_shown(self._shown)


class ShowMore(QWidget):
    """The end of a long list shown a page at a time: "Showing 200 of 4,812 rows" and a link
    for the next page. Hidden once everything is shown."""

    def __init__(self, total: int, on_more, step: int, noun: str = "items", parent=None):
        super().__init__(parent)
        self._total, self._step, self._noun = total, step, noun
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["m"])
        self._text = label("", "secondary")
        row.addWidget(self._text)
        self._link = link_button("")
        self._link.clicked.connect(on_more)
        row.addWidget(self._link)
        row.addStretch(1)
        self.set_shown(0)

    def set_shown(self, shown: int) -> None:
        left = self._total - shown
        self.setVisible(left > 0)
        self._text.setText(f"Showing {shown:,} of {self._total:,} {self._noun}.")
        self._link.setText(f"Show {min(self._step, left):,} more")
        self._link.setAccessibleName(f"Show {min(self._step, left):,} more {self._noun}")


class DropOverlay(QWidget):
    """Covers its parent while a file is dragged over the window: what dropping it does."""

    def __init__(self, title: str, body: str, parent: QWidget):
        super().__init__(parent)
        self._title, self._body = title, body
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def show_over_parent(self) -> None:
        self.setGeometry(self.parentWidget().rect())
        self.raise_()
        self.show()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.current()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(p.window)
        fill.setAlpha(248)
        painter.fillRect(self.rect(), fill)
        box = QRectF(self.rect()).adjusted(SPACE["xl"], SPACE["xl"], -SPACE["xl"], -SPACE["xl"])
        pen = QPen(QColor(p.border_strong), 2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(box, 12, 12)
        painter.setPen(QColor(p.text))
        painter.setFont(theme.ui_font("headline", QFont.Weight.DemiBold))
        centre = box.center().y()
        painter.drawText(QRectF(box.left(), centre - 40, box.width(), 40),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom, self._title)
        painter.setPen(QColor(p.text_secondary))
        painter.setFont(theme.ui_font())
        painter.drawText(QRectF(box.left(), centre + 8, box.width(), 30),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, self._body)


class PlainToolTips(QObject):
    """Shows every tooltip as plain text, like the labels: the names and paths in them come
    from CC packages, tray files and received zips, and Qt renders text that looks like HTML
    as HTML. Installed on the application, so it covers item views' tooltips too."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() != QEvent.Type.ToolTip or not isinstance(obj, QWidget):
            return False
        view = obj.parent()
        if isinstance(view, QAbstractItemView) and obj is view.viewport():
            index = view.indexAt(event.pos())
            data = index.data(Qt.ItemDataRole.ToolTipRole) if index.isValid() else None
            text = str(data) if data else ""
        else:
            text = obj.toolTip()
        if not text or not GuiQt.mightBeRichText(text):
            return False  # Qt shows it as plain text anyway
        escaped = html.escape(text).replace("\n", "<br>")
        QToolTip.showText(event.globalPos(), f"<qt>{escaped}</qt>", obj)
        return True


class SupportLine(QWidget):
    """A quiet line after a successful export: TrayTrim is free, and a link to support it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE["s"])
        row.addWidget(label("TrayTrim is free. If it saved you time, you can", "secondary"))
        link = link_button("buy me a coffee")
        link.setToolTip(SUPPORT_URL)
        link.setAccessibleName("Buy me a coffee (opens buymeacoffee.com)")
        link.clicked.connect(lambda: system.open_url(SUPPORT_URL))
        row.addWidget(link)
        row.addStretch(1)


class Spinner(QWidget):
    """Something is being worked on: an arc that turns and grows and shrinks, on a faint ring.
    It only animates while it's shown, so hidden spinners cost nothing."""

    _FRAME_MS = 16          # about 60 frames a second
    _TURN_MS = 1100         # one turn of the ring
    _BREATH_MS = 1600       # the arc grows and shrinks once in this time

    def __init__(self, size: int = 32, parent=None):
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._clock = QElapsedTimer()
        self._timer = QTimer(self, interval=self._FRAME_MS)
        self._timer.timeout.connect(self.update)
        self.setAccessibleName("Working")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._clock.start()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = theme.current()
        t = self._clock.elapsed() if self._clock.isValid() else 0
        width = max(2.0, self._size / 10)
        box = QRectF(self.rect()).adjusted(width, width, -width, -width)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.divider), width))
        painter.drawEllipse(box)
        # The arc's length breathes between 30 and 270 degrees while the ring turns.
        breath = (1 - math.cos(2 * math.pi * (t % self._BREATH_MS) / self._BREATH_MS)) / 2
        span = 30 + 240 * breath
        start = -360 * (t % self._TURN_MS) / self._TURN_MS - 120 * breath
        pen = QPen(QColor(p.accent_text), width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(box, int(start * 16), int(-span * 16))


class LoadingPanel(QWidget):
    """The centred state of a view: a message, or, while something is worked out, a loader
    with a spinner, what's happening and how far along it is."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("content")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE["xxl"], SPACE["xxl"], SPACE["xxl"], SPACE["xxl"])
        outer.addStretch(2)
        box = QWidget()
        box.setFixedWidth(440)  # a fixed width lets wrapped labels report the right height
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(SPACE["s"])
        self.spinner = Spinner(48)
        col.addWidget(self.spinner, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(SPACE["m"])
        self.title = label("", "headline", wrap=True)
        self.body = label("", "secondary", wrap=True)
        col.addWidget(self.title)
        col.addWidget(self.body)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedSize(280, 4)
        self.bar.setAccessibleName("Progress")
        col.addSpacing(SPACE["xs"])
        col.addWidget(self.bar, 0, Qt.AlignmentFlag.AlignHCenter)
        centred = QHBoxLayout()
        centred.addStretch(1)
        centred.addWidget(box)
        centred.addStretch(1)
        outer.addLayout(centred)
        outer.addStretch(3)
        self.set_state("", "")

    def set_state(self, title: str, body: str = "", loading: bool = False, done: int = 0,
                  total: int = 0) -> None:
        """loading: show the spinner, and a bar when `total` says how far along it is. A loader
        is centred; a message reads from the left, like the pages."""
        side = Qt.AlignmentFlag.AlignHCenter if loading else Qt.AlignmentFlag.AlignLeft
        for text in (self.title, self.body):
            text.setAlignment(side)
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.spinner.setVisible(loading)
        self.bar.setVisible(loading and total > 0)
        if total > 0:
            self.bar.setRange(0, total)
            self.bar.setValue(min(done, total))
