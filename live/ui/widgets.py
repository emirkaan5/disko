"""Small hand-painted controls: labels, badges, meters, knobs, faders, segmented choices."""
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QWidget

from . import theme as T


def caps(text: str, color: str = T.MUTED, size: float = 8.5) -> QLabel:
    label = QLabel(text.upper())
    label.setFont(T.font(size, QFont.Weight.DemiBold, spacing=1.1))
    label.setStyleSheet(f"color: {color};")
    return label


def label(text: str = "", size: float = 12, weight=QFont.Weight.Normal, color: str = T.TEXT, mono: bool = False) -> QLabel:
    lab = QLabel(text)
    lab.setFont(T.font(size, weight, mono=mono))
    lab.setStyleSheet(f"color: {color};")
    return lab


def clear(layout):
    """Empty a layout now: hide its widgets at once (deleteLater only runs on the next loop pass)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.hide()
            w.setParent(None)
            w.deleteLater()


def panel(name: str = "panel") -> QFrame:
    f = QFrame()
    f.setObjectName(name)
    return f


class Painted(QWidget):
    """Base for painted widgets: antialiased painter, fixed size hint."""

    def __init__(self, w: int, h: int, parent=None):
        super().__init__(parent)
        self._hint = QSize(w, h)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(h)

    def sizeHint(self):
        return self._hint

    def painter(self) -> QPainter:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        return p


class Badge(Painted):
    """A Camelot key code in its wheel color."""

    def __init__(self, size: float = 13):
        super().__init__(46, 26)
        self.code, self.size = "", size
        self.setFixedSize(48 if size > 11 else 40, 26 if size > 11 else 20)

    def set(self, code: str):
        self.code = code
        self.update()

    def paintEvent(self, _):
        if not self.code:
            return
        p = self.painter()
        c = T.camelot_color(self.code)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        fill = QColor(c)
        fill.setAlpha(50)
        p.setBrush(fill)
        p.setPen(QPen(c, 1.2))
        p.drawRoundedRect(r, 7, 7)
        p.setPen(c)
        p.setFont(T.font(self.size, QFont.Weight.Bold, mono=True))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, self.code)


class Meter(Painted):
    """Label, a thin bar at the value's library percentile, and the raw value."""

    def __init__(self, name: str, accent: str):
        super().__init__(160, 18)
        self.name, self.accent, self.frac, self.text = name, accent, 0.0, ""

    def set(self, frac: float, text: str):
        self.frac, self.text = max(0.0, min(1.0, frac)), text
        self.update()

    def paintEvent(self, _):
        p = self.painter()
        w, h = self.width(), self.height()
        p.setFont(T.font(8.5, QFont.Weight.DemiBold, spacing=1.0))
        p.setPen(T.qc(T.MUTED))
        p.drawText(QRectF(0, 0, 62, h), Qt.AlignmentFlag.AlignVCenter, self.name.upper())
        p.setFont(T.font(9.5, QFont.Weight.Medium, mono=True))
        p.setPen(T.qc(T.TEXT))
        p.drawText(QRectF(w - 40, 0, 40, h), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, self.text)
        track = QRectF(66, h / 2 - 2.5, w - 66 - 48, 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(track, 2.5, 2.5)
        if self.frac > 0:
            fill = QRectF(track.left(), track.top(), max(5.0, track.width() * self.frac), track.height())
            g = QLinearGradient(fill.topLeft(), fill.topRight())
            g.setColorAt(0, T.qc(self.accent, 110))
            g.setColorAt(1, T.qc(self.accent))
            p.setBrush(g)
            p.drawRoundedRect(fill, 2.5, 2.5)


class Knob(Painted):
    """A mixer knob. EQ: 1 = 0 dB at noon, 0 = kill. Bipolar: -1..1 with noon at 0."""

    START, SWEEP = 225.0, 270.0  # degrees, Qt angles (counter-clockwise from 3 o'clock)

    def __init__(self, name: str, accent: str, bipolar: bool = False, eq: bool = False):
        super().__init__(46, 58)
        self.setFixedSize(46, 58)
        self.name, self.accent, self.bipolar, self.eq, self.value = name, accent, bipolar, eq, 0.0

    def set(self, value: float):
        if abs(value - self.value) > 1e-3:
            self.value = value
            self.update()

    def _frac(self) -> float:
        if self.bipolar:
            return (self.value + 1) / 2
        if self.eq:
            return self.value / 2  # unity sits at noon
        return self.value

    def paintEvent(self, _):
        p = self.painter()
        d = 34
        r = QRectF((self.width() - d) / 2, 2, d, d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.RAISED))
        p.drawEllipse(r.adjusted(4, 4, -4, -4))
        p.setPen(T.pen(T.qc(T.BORDER), 3))
        p.drawArc(r, int(self.START * 16), int(-self.SWEEP * 16))
        frac = self._frac()
        origin = 0.5 if (self.bipolar or self.eq) else 0.0
        active = abs(frac - origin) > 0.01
        color = T.qc(T.BAD) if self.eq and self.value < 0.05 else T.qc(self.accent)
        if active:
            p.setPen(T.pen(color, 3))
            a0 = self.START - self.SWEEP * origin
            p.drawArc(r, int(a0 * 16), int(-self.SWEEP * (frac - origin) * 16))
        ang = math.radians(self.START - self.SWEEP * frac)
        c = r.center()
        tip = QPointF(c.x() + math.cos(ang) * 10, c.y() - math.sin(ang) * 10)
        p.setPen(T.pen(color if active else T.qc(T.MUTED), 2))
        p.drawLine(QPointF(c.x() + math.cos(ang) * 3, c.y() - math.sin(ang) * 3), tip)
        p.setFont(T.font(7.5, QFont.Weight.DemiBold, spacing=0.8))
        p.setPen(T.qc(T.MUTED))
        p.drawText(QRectF(0, d + 6, self.width(), 14), Qt.AlignmentFlag.AlignCenter, self.name)


class VFader(Painted):
    """A channel fader drawn as a filling bar with a cap."""

    def __init__(self, accent: str):
        super().__init__(16, 120)
        self.setFixedWidth(18)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.accent, self.value = accent, 0.0

    def set(self, value: float):
        if abs(value - self.value) > 1e-3:
            self.value = value
            self.update()

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(6, 4, -6, -4)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(r, 3, 3)
        h = r.height() * self.value
        fill = QRectF(r.left(), r.bottom() - h, r.width(), h)
        g = QLinearGradient(fill.bottomLeft(), fill.topLeft())
        g.setColorAt(0, T.qc(self.accent, 90))
        g.setColorAt(1, T.qc(self.accent))
        p.setBrush(g)
        p.drawRoundedRect(fill, 3, 3)
        cap = QRectF(1, r.bottom() - h - 3, self.width() - 2, 6)
        p.setBrush(T.qc(T.TEXT))
        p.drawRoundedRect(cap, 2, 2)


class LevelMeter(Painted):
    """Output level: a segmented bar, green to amber to red."""

    def __init__(self):
        super().__init__(10, 120)
        self.setFixedWidth(12)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.value = 0.0

    def set(self, peak: float):
        db = 20 * math.log10(max(peak, 1e-5))
        v = max(0.0, min(1.0, (db + 48) / 48))
        if abs(v - self.value) > 0.005:
            self.value = v
            self.update()

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(2, 4, -2, -4)
        n = 24
        seg = r.height() / n
        lit = int(round(self.value * n))
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(n):
            color = T.GOOD if i < n * 0.7 else T.WARN if i < n * 0.9 else T.BAD
            p.setBrush(T.qc(color, 230 if i < lit else 35))
            p.drawRoundedRect(QRectF(r.left(), r.bottom() - (i + 1) * seg + 1, r.width(), seg - 2), 1.5, 1.5)


class PhraseDots(Painted):
    """Eight dots: where we are inside the current 8-bar phrase."""

    def __init__(self, accent: str):
        super().__init__(96, 10)
        self.accent, self.bar = accent, 0

    def set(self, bar_in_phrase: int):
        if bar_in_phrase != self.bar:
            self.bar = bar_in_phrase
            self.update()

    def paintEvent(self, _):
        p = self.painter()
        p.setPen(Qt.PenStyle.NoPen)
        for i in range(8):
            on = i < self.bar
            p.setBrush(T.qc(self.accent if on else T.BORDER, 255 if i == self.bar - 1 else 150 if on else 255))
            p.drawRoundedRect(QRectF(i * 12, 2, 9, 6), 2, 2)


class Segmented(QWidget):
    """A row of chips, one checked; emits the chosen value."""

    changed = Signal(object)

    def __init__(self, options, value=None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.values = []
        for i, (text, v) in enumerate(options):
            b = QPushButton(text)
            b.setObjectName("chip")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            self.group.addButton(b, i)
            lay.addWidget(b)
            self.values.append(v)
        lay.addStretch(1)
        self.set(value if value is not None else self.values[0])
        self.group.idClicked.connect(lambda i: self.changed.emit(self.values[i]))

    def set(self, value):
        if value in self.values:
            self.group.button(self.values.index(value)).setChecked(True)

    def value(self):
        return self.values[self.group.checkedId()]


class Glyph(Painted):
    """Tiny pictogram for each mixing technique."""

    def __init__(self, kind: str, size: int = 34):
        super().__init__(size, size)
        self.setFixedSize(size, size)
        self.kind, self.color = kind, T.MUTED

    def set_color(self, color: str):
        self.color = color
        self.update()

    def paintEvent(self, _):
        p = self.painter()
        w, h = self.width(), self.height()
        a, b = T.qc(T.DECK_A), T.qc(T.DECK_B)
        pen = lambda c, wd=2.0: T.pen(c, wd)
        m = 5
        if self.kind == "bass_swap":
            p.setPen(pen(a))
            p.drawLine(QPointF(m, h * 0.35), QPointF(w / 2, h * 0.35))
            p.drawLine(QPointF(w / 2, h * 0.35), QPointF(w / 2, h * 0.7))
            p.setPen(pen(b))
            p.drawLine(QPointF(w / 2, h * 0.7), QPointF(w - m, h * 0.7))
            p.drawLine(QPointF(m, h * 0.7), QPointF(w / 2 - 3, h * 0.7))
        elif self.kind == "filter_sweep":
            path = QPainterPath(QPointF(m, h - m))
            path.cubicTo(QPointF(w * 0.45, h - m), QPointF(w * 0.55, m + 2), QPointF(w - m, m + 2))
            p.setPen(pen(a))
            p.drawPath(path)
            path2 = QPainterPath(QPointF(m, m + 2))
            path2.cubicTo(QPointF(w * 0.45, m + 2), QPointF(w * 0.55, h - m), QPointF(w - m, h - m))
            p.setPen(pen(b, 1.4))
            p.drawPath(path2)
        elif self.kind == "echo_out":
            p.setPen(Qt.PenStyle.NoPen)
            for i in range(5):
                c = QColor(a)
                c.setAlpha(255 - i * 45)
                p.setBrush(c)
                bh = (h - 2 * m) * (1 - i * 0.18)
                p.drawRoundedRect(QRectF(m + i * 5.5, (h - bh) / 2, 3.2, bh), 1.5, 1.5)
            p.setBrush(b)
            p.drawRoundedRect(QRectF(w - m - 4, m, 4, h - 2 * m), 1.5, 1.5)
        elif self.kind == "drop_cut":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(a)
            p.drawRoundedRect(QRectF(m, m + 4, w / 2 - m - 2, h - 2 * m - 8), 2, 2)
            p.setBrush(b)
            p.drawRoundedRect(QRectF(w / 2 + 2, m, w / 2 - m - 2, h - 2 * m), 2, 2)
        else:  # smooth_fade
            p.setPen(pen(a))
            path = QPainterPath(QPointF(m, m + 2))
            path.quadTo(QPointF(w * 0.62, m + 2), QPointF(w - m, h - m))
            p.drawPath(path)
            p.setPen(pen(b))
            path = QPainterPath(QPointF(m, h - m))
            path.quadTo(QPointF(w * 0.38, m + 2), QPointF(w - m, m + 2))
            p.drawPath(path)
