"""Our own SVG icons (assets/icons), drawn in a theme colour at the size and scale needed."""

from __future__ import annotations

from functools import cache, lru_cache
from importlib import resources

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QImage, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

NAMES = ("search", "clear", "check", "chevron-right", "chevron-down", "alert", "info", "folder",
         "export", "copies", "excluded", "package")


@cache
def _svg(name: str) -> str:
    return (resources.files("traytrim.gui") / "assets" / "icons" / f"{name}.svg").read_text(
        encoding="utf-8")


@lru_cache(maxsize=512)
def image(name: str, colour: str, size: int = 20, scale: float = 1.0) -> QImage:
    """The icon as a transparent image, `size` logical pixels at device `scale`. Cached: list
    rows ask for the same icon on every repaint (QImage copies share their pixels)."""
    renderer = QSvgRenderer(QByteArray(_svg(name).replace("currentColor", colour).encode()))
    px = max(1, round(size * scale))
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    img.setDevicePixelRatio(scale)
    return img


def pixmap(name: str, colour: str, size: int = 20, scale: float = 1.0) -> QPixmap:
    return QPixmap.fromImage(image(name, colour, size, scale))


def icon(name: str, colour: str, size: int = 20) -> QIcon:
    """A QIcon with crisp versions for common display scales."""
    result = QIcon()
    for scale in (1.0, 1.5, 2.0):
        result.addPixmap(pixmap(name, colour, size, scale))
    return result


def file_for_stylesheet(name: str, colour: str, size: int, scale: float = 2.0) -> str:
    """Stylesheets (checkbox ticks, tree arrows) need an image file: write one to the cache."""
    from traytrim.paths import cache_dir

    folder = cache_dir() / "icons"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}-{colour.lstrip('#')}-{size}@{scale:g}x.png"
    if not path.exists():
        image(name, colour, size, scale).save(str(path))
    return path.as_posix()


def qsize(size: int) -> QSize:
    return QSize(size, size)
