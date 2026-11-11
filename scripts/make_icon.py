"""Draw disko's app icon: a record on a dark rounded square, its label split between the two
decks' colours (live/ui/theme.py). Writes a macOS .iconset folder; scripts/make_app.sh turns it
into disko.icns with iconutil.

    QT_QPA_PLATFORM=offscreen .venv-live/bin/python scripts/make_icon.py OUT.iconset
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (QBrush, QColor, QConicalGradient, QGuiApplication, QImage, QLinearGradient,  # noqa: E402
                           QPainter, QPainterPath, QPen, QRadialGradient)

from live.ui import theme  # noqa: E402

# Apple's grid for a 1024 canvas: an 824 px rounded square, centred, with a 185 px corner radius.
TILE, RADIUS = 824 / 1024, 185 / 1024
SIZES = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2), (256, 1), (256, 2), (512, 1), (512, 2)]


def c(hex_, alpha=255):
    q = QColor(hex_)
    q.setAlpha(alpha)
    return q


def draw(p: QPainter, n: int):
    """The icon at n x n pixels. Small sizes drop the fine grooves, which would only blur."""
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = n / 1024
    tile = QRectF((1 - TILE) / 2 * n, (1 - TILE) / 2 * n, TILE * n, TILE * n)
    path = QPainterPath()
    path.addRoundedRect(tile, RADIUS * n, RADIUS * n)

    # The tile: the app's background, lifted a little towards the top.
    bg = QLinearGradient(tile.topLeft(), tile.bottomLeft())
    bg.setColorAt(0, c(theme.RAISED))
    bg.setColorAt(1, c(theme.BG))
    p.fillPath(path, QBrush(bg))
    p.save()
    p.setClipPath(path)
    glow = QRadialGradient(QPointF(n / 2, n / 2), 0.46 * n)
    glow.setColorAt(0, c(theme.VIOLET, 46))
    glow.setColorAt(1, c(theme.VIOLET, 0))
    p.fillRect(tile, QBrush(glow))
    p.restore()
    if n >= 64:
        p.setPen(QPen(c(theme.BORDER), max(1.0, 3 * s)))
        p.drawPath(path)

    # The record.
    centre = QPointF(n / 2, n / 2)
    r = 0.335 * n
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c("#05060a"))
    p.drawEllipse(centre, r, r)
    if n >= 128:  # grooves
        rings = 9 if n >= 512 else 5
        for i in range(rings):
            rr = r * (0.93 - i * (0.93 - 0.47) / (rings - 1))
            p.setPen(QPen(c(theme.FAINT, 70 if i % 2 else 110), max(1.0, 2.2 * s)))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(centre, rr, rr)
    # Light catching the vinyl: two soft wedges across from each other, square to the label's split.
    sheen = QConicalGradient(centre, 135 - 0.06 * 360)
    for at, alpha in ((0, 0), (0.06, 70), (0.14, 0), (0.5, 0), (0.56, 45), (0.64, 0), (1, 0)):
        sheen.setColorAt(at, c(theme.TEXT, alpha))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(sheen))
    p.drawEllipse(centre, r, r)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(c(theme.MUTED, 90 if n >= 64 else 170), max(1.0, 3 * s)))
    p.drawEllipse(centre, r, r)

    # The label: deck A's cyan meets deck B's orange, the two tracks of a mix.
    lr = r * (0.40 if n >= 64 else 0.46)
    label = QRectF(centre.x() - lr, centre.y() - lr, 2 * lr, 2 * lr)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c(theme.DECK_A))
    p.drawPie(label, 45 * 16, 180 * 16)
    p.setBrush(c(theme.DECK_B))
    p.drawPie(label, 225 * 16, 180 * 16)
    # The spindle hole.
    p.setBrush(c(theme.BG))
    hole = max(0.8, lr * 0.16)
    p.drawEllipse(centre, hole, hole)


def render(n: int) -> QImage:
    img = QImage(n, n, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    draw(p, n)
    p.end()
    return img


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    _app = QGuiApplication(sys.argv[:1])
    for size, scale in SIZES:
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        if not render(size * scale).save(str(out / name)):
            sys.exit(f"could not write {out / name}")
    print("wrote", out)


if __name__ == "__main__":
    main()
