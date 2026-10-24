"""Render the app icon (.ico, PNGs) and the installer's wizard images from
src/traytrim/gui/assets/app/icon.svg. Run after changing the icon:

    python packaging/build_assets.py

Needs PySide6 and Pillow (pip install -e ".[build]").
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "src" / "traytrim" / "gui" / "assets" / "app" / "icon.svg"
OUT = ROOT / "packaging" / "assets"
ICO_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]
INK = "#171717"


def render(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(SVG)).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def to_pil(image: QImage) -> Image.Image:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return Image.frombuffer("RGBA", (image.width(), image.height()),
                            bytes(image.constBits()), "raw", "RGBA", 0, 1)


def wizard_image(width: int, height: int) -> QImage:
    """The tall image on the installer's first and last page: ink, icon and name."""
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(INK))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    side = width * 0.5
    painter.drawImage(QRectF((width - side) / 2, height * 0.28, side, side), render(512))
    font = QFont("IBM Plex Sans")
    font.setPixelSize(round(width * 0.13))
    font.setWeight(QFont.Weight.DemiBold)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(QRectF(0, height * 0.28 + side + width * 0.08, width, width * 0.2),
                     Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop, "TrayTrim")
    painter.end()
    return image


def main() -> int:
    from traytrim.gui import theme
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    theme.load_fonts()
    OUT.mkdir(parents=True, exist_ok=True)

    images = [to_pil(render(s)) for s in ICO_SIZES]
    images[-1].save(OUT / "TrayTrim.ico", sizes=[(s, s) for s in ICO_SIZES],
                    append_images=images[:-1])
    for size in (256, 1024):
        render(size).save(str(OUT / f"TrayTrim-{size}.png"))

    # Inno Setup picks the variant closest to the display scale (100%, 150%, 200%).
    for scale, (w, h) in {100: (164, 314), 150: (246, 471), 200: (328, 628)}.items():
        to_pil(wizard_image(w, h)).convert("RGB").save(OUT / f"wizard-{scale}.bmp")
    for scale, size in {100: 55, 150: 83, 200: 110}.items():
        small = Image.new("RGB", (size, size), "#FFFFFF")
        icon = to_pil(render(size))
        small.paste(icon, (0, 0), icon)
        small.save(OUT / f"wizard-small-{scale}.bmp")
    del app
    print(f"Wrote icon and installer images to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
