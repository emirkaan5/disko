"""The mixer column: both channels' EQ, filter, echo and faders, moved by the transition's automation."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QVBoxLayout, QWidget

from . import theme as T
from .widgets import Knob, LevelMeter, Painted, VFader, caps, label

KNOBS = (("high", "HI", True, False), ("mid", "MID", True, False), ("low", "LOW", True, False),
         ("filter", "FILTER", False, True), ("echo", "ECHO", False, False))


class XFader(Painted):
    """Where the blend sits between deck A and deck B."""

    def __init__(self):
        super().__init__(180, 30)
        self.value = 0.0

    def set(self, value: float):
        if abs(value - self.value) > 1e-3:
            self.value = value
            self.update()

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(14, 0, -14, 0)
        y = r.center().y()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(QRectF(r.left(), y - 2, r.width(), 4), 2, 2)
        x = r.left() + r.width() * self.value
        p.setBrush(T.qc(T.DECK_A, 160))
        p.drawRoundedRect(QRectF(r.left(), y - 2, x - r.left(), 4), 2, 2)
        p.setBrush(T.qc(T.TEXT))
        p.drawRoundedRect(QRectF(x - 7, y - 9, 14, 18), 3, 3)
        p.setPen(QPen(T.qc(T.BG), 1.5))
        p.drawLine(int(x), int(y - 5), int(x), int(y + 5))
        p.setFont(T.font(8, QFont.Weight.Bold))
        p.setPen(T.qc(T.DECK_A))
        p.drawText(QRectF(0, 0, 12, self.height()), Qt.AlignmentFlag.AlignVCenter, "A")
        p.setPen(T.qc(T.DECK_B))
        p.drawText(QRectF(self.width() - 10, 0, 10, self.height()), Qt.AlignmentFlag.AlignVCenter, "B")


class Progress(Painted):
    def __init__(self):
        super().__init__(180, 6)
        self.value, self.marks = 0.0, []

    def set(self, value: float, marks=()):
        self.value, self.marks = value, list(marks)
        self.update()

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.BORDER))
        p.drawRoundedRect(r, 3, 3)
        p.setBrush(T.qc(T.VIOLET))
        p.drawRoundedRect(QRectF(0, 0, r.width() * self.value, r.height()), 3, 3)
        p.setBrush(T.qc(T.TEXT))
        for m in self.marks:
            p.drawRect(QRectF(r.width() * m - 1, 0, 2, r.height()))


class MixerStrip(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        self.setFixedWidth(232)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 16, 14, 16)
        lay.setSpacing(10)
        head = QHBoxLayout()
        head.addWidget(caps("Mixer", T.TEXT, 9))
        head.addStretch(1)
        self.mode = caps("idle", T.MUTED, 8.5)
        head.addWidget(self.mode)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setHorizontalSpacing(4)
        grid.setVerticalSpacing(2)
        grid.addWidget(caps("A", T.DECK_A, 9), 0, 0, Qt.AlignmentFlag.AlignHCenter)
        grid.addWidget(caps("B", T.DECK_B, 9), 0, 2, Qt.AlignmentFlag.AlignHCenter)
        self.knobs = {"A": {}, "B": {}}
        for row, (key, name, eq, bipolar) in enumerate(KNOBS, start=1):
            for col, deck, accent in ((0, "A", T.DECK_A), (2, "B", T.DECK_B)):
                k = Knob(name, accent, bipolar=bipolar, eq=eq)
                self.knobs[deck][key] = k
                grid.addWidget(k, row, col, Qt.AlignmentFlag.AlignHCenter)
        self.level = LevelMeter()
        grid.addWidget(self.level, 1, 1, len(KNOBS), 1, Qt.AlignmentFlag.AlignHCenter)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)

        faders = QHBoxLayout()
        self.fader = {"A": VFader(T.DECK_A), "B": VFader(T.DECK_B)}
        faders.addStretch(1)
        faders.addWidget(self.fader["A"])
        faders.addSpacing(54)
        faders.addWidget(self.fader["B"])
        faders.addStretch(1)
        lay.addLayout(faders, 1)
        self.xfader = XFader()
        lay.addWidget(self.xfader)

        box = QFrame()
        box.setObjectName("card")
        bl = QVBoxLayout(box)
        bl.setContentsMargins(12, 10, 12, 12)
        bl.setSpacing(6)
        self.title = label("No transition", 11.5, QFont.Weight.DemiBold)
        self.detail = label("Pick a track on the right", 9.5, color=T.MUTED)
        self.detail.setWordWrap(True)
        self.detail.setFixedHeight(30)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.progress = Progress()
        bl.addWidget(self.title)
        bl.addWidget(self.detail)
        bl.addWidget(self.progress)
        lay.addWidget(box)

    def set_channels(self, a: dict, b: dict):
        for deck, values in (("A", a), ("B", b)):
            for key, knob in self.knobs[deck].items():
                knob.set(values.get(key, 0.0))
            self.fader[deck].set(values.get("fader", 0.0))
        fa, fb = a.get("fader", 0.0), b.get("fader", 0.0)
        self.xfader.set(fb / (fa + fb) if fa + fb > 1e-6 else 0.0)

    def set_info(self, mode: str, title: str, detail: str, progress: float = 0.0, marks=()):
        self.mode.setText(mode.upper())
        self.mode.setStyleSheet(f"color: {T.VIOLET if mode == 'mixing' else T.MUTED};")
        self.title.setText(title)
        self.detail.setText(detail)
        self.progress.set(progress, marks)
