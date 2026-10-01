"""Waveforms: a scrolling, beat-gridded deck view and a whole-track overview strip."""
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ..audio import SR, WAVE_RATE, Deck, Waveform
from . import theme as T


def _rgba(hex_: str, alpha: int = 255) -> tuple:
    c = QColor(hex_)
    return c.red(), c.green(), c.blue(), alpha


class WaveImage:
    """A track's waveform painted once into image tiles at PX_PER_S pixels per native second:
    lows in blue, mids in amber, highs in white, layered from the outside in."""

    PX_PER_S = 100
    TILE = 4096
    H = 128

    def __init__(self, wave: Waveform):
        cols = max(1, int(wave.seconds * self.PX_PER_S))
        edges = np.clip((np.arange(cols) * WAVE_RATE / self.PX_PER_S).astype(int), 0, len(wave.full) - 1)
        col = lambda env: np.maximum.reduceat(env, edges)
        half = self.H / 2 - 1
        dist = np.abs(np.arange(self.H) - (self.H - 1) / 2)[:, None]
        img = np.zeros((self.H, cols, 4), np.uint8)
        layers = [(col(wave.peak), 1.0, _rgba("#283043")), (col(wave.low), 1.0, _rgba(T.WAVE_LOW)),
                  (col(wave.mid), 0.8, _rgba(T.WAVE_MID)), (col(wave.high), 0.6, _rgba(T.WAVE_HIGH, 215))]
        for env, scale, color in layers:
            img[dist <= np.maximum(np.minimum(env * scale, 1.0) * half, 0.6)[None, :]] = color
        self.tiles = []
        for s in range(0, cols, self.TILE):
            part = np.ascontiguousarray(img[:, s:s + self.TILE])
            q = QImage(part.data, part.shape[1], part.shape[0], part.shape[1] * 4, QImage.Format.Format_RGBA8888)
            self.tiles.append(q.copy())

    def draw(self, p: QPainter, target: QRectF, t0: float, t1: float):
        """Draw native seconds [t0, t1] stretched over `target`."""
        if t1 <= t0:
            return
        c0, c1 = t0 * self.PX_PER_S, t1 * self.PX_PER_S
        scale = target.width() / (c1 - c0)
        for i, img in enumerate(self.tiles):
            s = i * self.TILE
            a, b = max(c0, s), min(c1, s + img.width())
            if b <= a:
                continue
            p.drawImage(QRectF(target.left() + (a - c0) * scale, target.top(), (b - a) * scale, target.height()),
                        img, QRectF(a - s, 0, b - a, self.H))


class WaveformView(QWidget):
    """A deck's waveform scrolling past a fixed playhead, with beat grid, sections and stem lanes."""

    def __init__(self, accent: str, span: float = 14.0):
        super().__init__()
        self.accent, self.span = accent, span
        self.deck: Deck | None = None
        self.image: WaveImage | None = None
        self.pos = 0
        self.region = None      # (start, end) playing seconds of a transition
        self.marks = []         # [(playing seconds, label)]
        self.enters_at = None   # playing seconds where this deck becomes audible
        self.setMinimumHeight(150)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_deck(self, deck: Deck | None, image: WaveImage | None):
        self.deck, self.image = deck, image
        self.region, self.marks, self.enters_at = None, [], None
        self.update()

    def set_position(self, samples: int):
        self.pos = samples
        self.update()

    def wheelEvent(self, e):
        self.span = float(np.clip(self.span * (0.85 if e.angleDelta().y() > 0 else 1.18), 4, 60))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.qc(T.BORDER), 1))
        p.setBrush(T.qc("#0b0e14"))
        p.drawRoundedRect(r, 10, 10)
        if self.deck is None or self.image is None:
            p.setPen(T.qc(T.FAINT))
            p.setFont(T.font(11))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "No track loaded")
            return
        p.setClipRect(r.adjusted(1, 1, -1, -1))
        d = self.deck
        t = self.pos / SR
        t0, t1 = t - self.span / 2, t + self.span / 2
        x = lambda s: r.left() + (s - t0) / self.span * r.width()
        sec_h, lane_h = 15.0, 5.0
        wave = QRectF(r.left(), r.top() + sec_h + 6, r.width(), r.height() - sec_h - 6 - 2 * lane_h - 12)

        # sections along the top
        p.setFont(T.font(7.5, QFont.Weight.DemiBold, spacing=0.9))
        for lab, a, b in d.sections:
            if b < t0 or a > t1:
                continue
            color = T.SECTION.get(lab, T.MUTED)
            box = QRectF(x(a) + 1, r.top() + 4, x(b) - x(a) - 2, sec_h - 2)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(color, 55))
            p.drawRoundedRect(box, 3, 3)
            p.setBrush(T.qc(color, 230))
            p.drawRect(QRectF(box.left(), box.top(), box.width(), 2))
            visible = box.intersected(r)
            if visible.width() > 40:  # the label sticks to the left edge while its section scrolls off
                p.setPen(T.qc(color))
                p.drawText(visible.adjusted(6, 2, -2, 0), Qt.AlignmentFlag.AlignVCenter, lab.upper())

        # transition window behind the wave
        if self.region:
            a, b = self.region
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(self.accent, 26))
            p.drawRect(QRectF(x(a), wave.top(), x(b) - x(a), wave.height()))

        self.image.draw(p, wave, t0 * d.ratio, t1 * d.ratio)

        # what has already played is dimmed; before this deck enters, nothing is heard
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc("#0b0e14", 120))
        p.drawRect(QRectF(r.left(), wave.top(), r.width() / 2, wave.height()))
        if self.enters_at is not None and self.enters_at > t0:
            p.setBrush(T.qc("#0b0e14", 190))
            p.drawRect(QRectF(r.left(), wave.top(), min(x(self.enters_at), r.right()) - r.left(), wave.height()))

        # beat grid: beats faint, bar lines brighter, phrase starts (every 8 bars) in the deck color
        lo, hi = np.searchsorted(d.beats, [t0, t1])
        p.setPen(QPen(T.qc("#ffffff", 22), 1))
        for bt in d.beats[lo:hi]:
            p.drawLine(QPointF(x(bt), wave.top()), QPointF(x(bt), wave.bottom()))
        lo, hi = np.searchsorted(d.downbeats, [t0, t1])
        p.setFont(T.font(7.5, QFont.Weight.Medium, mono=True))
        for i in range(lo, hi):
            xb, bar = x(d.downbeats[i]), i + 1
            phrase = (bar - 1) % 8 == 0
            p.setPen(QPen(T.qc(self.accent, 170) if phrase else T.qc("#ffffff", 60), 1.4 if phrase else 1))
            p.drawLine(QPointF(xb, wave.top()), QPointF(xb, wave.bottom()))
            if phrase or self.span < 9:
                p.setPen(T.qc(self.accent) if phrase else T.qc(T.MUTED))
                p.drawText(QPointF(xb + 3, wave.top() + 10), str(bar))

        # stem lanes: bass and vocals, one block per bar
        for row, (bits, color) in enumerate(((d.track.bass_bars, T.BASS_LANE), (d.track.vocal_bars, T.VOCAL_LANE))):
            y = wave.bottom() + 5 + row * (lane_h + 3)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(color, 200))
            for i in range(max(lo - 1, 0), min(hi + 1, len(bits), len(d.downbeats))):
                if bits[i] == "1":
                    end = d.downbeats[i + 1] if i + 1 < len(d.downbeats) else d.length / SR
                    p.drawRoundedRect(QRectF(x(d.downbeats[i]) + 0.5, y, x(end) - x(d.downbeats[i]) - 1, lane_h), 1.5, 1.5)

        # transition edges and marks
        p.setFont(T.font(7.5, QFont.Weight.DemiBold, spacing=0.9))
        if self.region:
            for edge, text in zip(self.region, ("MIX START", "MIX END")):
                xe = x(edge)
                if r.left() <= xe <= r.right():
                    p.setPen(QPen(T.qc(self.accent, 220), 1.5))
                    p.drawLine(QPointF(xe, wave.top()), QPointF(xe, wave.bottom()))
                    p.drawText(QPointF(xe + 4, wave.bottom() - 4), text)
        for when, text in self.marks:
            xm = x(when)
            p.setPen(QPen(T.qc(T.VIOLET), 1.3, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(xm, wave.top()), QPointF(xm, wave.bottom()))
            p.setPen(T.qc(T.VIOLET))
            p.drawText(QPointF(xm + 4, wave.top() + 22), text.upper())

        # playhead
        cx = r.center().x()
        p.setPen(QPen(T.qc("#ffffff", 235), 2))
        p.drawLine(QPointF(cx, wave.top() - 4), QPointF(cx, wave.bottom() + 2))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc("#ffffff"))
        p.drawPolygon([QPointF(cx - 5, wave.top() - 6), QPointF(cx + 5, wave.top() - 6), QPointF(cx, wave.top())])


class OverviewView(QWidget):
    """The whole track in one strip; click to jump."""

    seek = Signal(float)

    def __init__(self, accent: str):
        super().__init__()
        self.accent = accent
        self.deck: Deck | None = None
        self.frac = 0.0
        self.region = None  # (start, end) as fractions of the track
        self._cache = None
        self.setFixedHeight(46)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_deck(self, deck: Deck | None):
        self.deck, self.region, self._cache = deck, None, None
        self.update()

    def set_position(self, samples: int):
        if self.deck:
            self.frac = samples / self.deck.length
            self.update()

    def _images(self, w: int, h: int):
        key = (w, h, id(self.deck))
        if self._cache and self._cache[0] == key:
            return self._cache[1:]
        dpr = self.devicePixelRatioF()
        pw, ph = max(1, int(w * dpr)), max(1, int(h * dpr))
        env = self.deck.wave.full  # RMS, so the breakdowns read as dips
        edges = np.clip(np.linspace(0, len(env), pw, endpoint=False).astype(int), 0, len(env) - 1)
        heights = np.maximum.reduceat(env, edges) * (ph / 2 - 1)
        dist = np.abs(np.arange(ph) - (ph - 1) / 2)[:, None]
        mask = dist <= np.maximum(heights, 0.5)[None, :]
        out = []
        for color in (_rgba(T.FAINT, 200), _rgba(self.accent)):
            arr = np.zeros((ph, pw, 4), np.uint8)
            arr[mask] = color
            img = QImage(arr.data, pw, ph, pw * 4, QImage.Format.Format_RGBA8888).copy()
            img.setDevicePixelRatio(dpr)
            out.append(img)
        self._cache = (key, *out)
        return out

    def mousePressEvent(self, e):
        if self.deck:
            self.seek.emit(max(0.0, min(1.0, e.position().x() / self.width())))

    def paintEvent(self, _):
        p = QPainter(self)
        r = QRectF(self.rect())
        if self.deck is None:
            return
        wave_h = r.height() - 6
        dim, bright = self._images(int(r.width()), int(wave_h))
        cut = r.width() * self.frac
        p.drawImage(QPointF(0, 0), dim)
        p.save()
        p.setClipRect(QRectF(0, 0, cut, wave_h))
        p.setOpacity(0.55)
        p.drawImage(QPointF(0, 0), bright)
        p.restore()
        length = self.deck.length / SR
        for lab, a, b in self.deck.sections:
            p.fillRect(QRectF(a / length * r.width(), wave_h + 2, (b - a) / length * r.width() - 1, 3),
                       T.qc(T.SECTION.get(lab, T.MUTED), 200))
        if self.region:
            a, b = self.region
            p.fillRect(QRectF(a * r.width(), 0, (b - a) * r.width(), wave_h), T.qc(self.accent, 50))
        p.setPen(QPen(T.qc("#ffffff"), 2))
        p.drawLine(QPointF(cut, 0), QPointF(cut, wave_h))
