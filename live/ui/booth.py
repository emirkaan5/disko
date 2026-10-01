"""Kev's booth: the AI DJ's decisions as they happen — the tracks it weighed, the pick, and the mix plan."""
import time

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QFont, QFontMetrics
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget

from ..library import KEY_NAMES
from ..mixes import TECHNIQUES
from . import theme as T
from .widgets import Painted, Segmented, caps, clear, label


class CandidateRow(Painted):
    """One shortlisted track: Kev's flow and energy ratings, and its chance of being picked."""

    def __init__(self, rank: int, candidate):
        super().__init__(300, 34)
        self.rank, self.c, self.picked = rank, candidate, False

    def paintEvent(self, _):
        p = self.painter()
        r = QRectF(self.rect()).adjusted(0.5, 1, -0.5, -1)
        c = self.c
        p.setPen(T.pen(T.qc(T.VIOLET if self.picked else T.BORDER), 1.4 if self.picked else 1))
        p.setBrush(T.qc(T.VIOLET, 38) if self.picked else T.qc(T.PANEL_HI))
        p.drawRoundedRect(r, 8, 8)
        p.setFont(T.font(9.5, QFont.Weight.Bold, mono=True))
        p.setPen(T.qc(T.VIOLET if self.picked else T.FAINT))
        p.drawText(QRectF(r.left() + 8, r.top(), 18, r.height()), Qt.AlignmentFlag.AlignVCenter,
                   "★" if self.picked else str(self.rank))

        right = r.right() - 10
        p.setFont(T.font(11, QFont.Weight.DemiBold, mono=True))
        if c.chance is not None:
            p.setPen(T.qc(T.VIOLET if self.picked else T.TEXT))
            p.drawText(QRectF(right - 44, r.top(), 44, r.height()), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{c.chance:.0%}")
        right -= 54
        if c.score is None:
            p.setFont(T.font(9))
            p.setPen(T.qc(T.FAINT))
            p.drawText(QRectF(right - 120, r.top(), 120, r.height()),
                       Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, "Kev is listening…")
            right -= 128
        else:
            for name, value, color in (("rep", c.repeat, T.BAD), ("energy", c.energy, T.GOOD), ("flow", c.flow, T.DECK_A)):
                box = QRectF(right - 40, r.top() + 6, 40, r.height() - 12)
                p.setFont(T.font(6.5, QFont.Weight.DemiBold, spacing=0.6))
                p.setPen(T.qc(T.FAINT))
                p.drawText(QRectF(box.left(), box.top(), box.width(), 9), Qt.AlignmentFlag.AlignLeft, name.upper())
                bar = QRectF(box.left(), box.bottom() - 5, box.width(), 4)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(T.qc(T.BORDER))
                p.drawRoundedRect(bar, 2, 2)
                p.setBrush(T.qc(color))
                p.drawRoundedRect(QRectF(bar.left(), bar.top(), max(3.0, bar.width() * value), bar.height()), 2, 2)
                right -= 48
        left = r.left() + 30
        width = right - left - 6
        t = c.track
        p.setFont(T.font(10.5, QFont.Weight.DemiBold))
        p.setPen(T.qc(T.TEXT))
        fm = QFontMetrics(p.font())
        p.drawText(QRectF(left, r.top() + 2, width, 16), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(t.title, Qt.TextElideMode.ElideRight, int(width)))
        p.setFont(T.font(8.5))
        p.setPen(T.qc(T.MUTED))
        fm = QFontMetrics(p.font())
        meta = f"{t.artist} · {t.camelot} · {c.fit.replace('tempo ', '').replace('; ', ' · ')}"
        p.drawText(QRectF(left, r.top() + 17, width, 14), Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(meta, Qt.TextElideMode.ElideRight, int(width)))


class ProbBars(Painted):
    """Kev's probability for each option, the chosen one in color."""

    ROW = 19

    def __init__(self):
        super().__init__(300, 0)
        self.rows = []  # (label, prob, chosen, note)

    def set(self, rows: list):
        self.rows = rows
        self.setFixedHeight(len(rows) * self.ROW)
        self.update()

    def paintEvent(self, _):
        if not self.rows:
            return
        p = self.painter()
        w = self.width()
        top = max(r[1] for r in self.rows) or 1
        for i, (name, prob, chosen, note) in enumerate(self.rows):
            y = i * self.ROW
            p.setFont(T.font(9.5, QFont.Weight.DemiBold if chosen else QFont.Weight.Normal))
            p.setPen(T.qc(T.TEXT if chosen else T.MUTED))
            p.drawText(QRectF(0, y, 110, self.ROW), Qt.AlignmentFlag.AlignVCenter, name)
            track = QRectF(116, y + self.ROW / 2 - 3, w - 116 - 100, 6)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(T.BORDER))
            p.drawRoundedRect(track, 3, 3)
            p.setBrush(T.qc(T.VIOLET if chosen else T.FAINT))
            p.drawRoundedRect(QRectF(track.left(), track.top(), max(4.0, track.width() * prob / top), 6), 3, 3)
            p.setFont(T.font(9, QFont.Weight.Medium, mono=True))
            p.setPen(T.qc(T.TEXT if chosen else T.MUTED))
            p.drawText(QRectF(w - 92, y, 38, self.ROW), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                       f"{prob:.0%}")
            if note:
                p.setFont(T.font(7.5, QFont.Weight.DemiBold, spacing=0.5))
                p.setPen(T.qc(T.VIOLET if chosen else T.WARN))
                p.drawText(QRectF(w - 50, y, 50, self.ROW), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                           note)


class Booth(QFrame):
    repick = Signal()
    lift = Signal(int)
    skip = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        self.rows = []
        self.candidates = []
        self._started = 0.0
        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 14, 16, 12)
        outer.setSpacing(8)

        head = QHBoxLayout()
        dot = label("●", 9, color=T.VIOLET)
        head.addWidget(dot)
        head.addWidget(caps("Kev · DJ", T.TEXT, 9))
        self.model = caps("", T.FAINT, 8)
        head.addSpacing(6)
        head.addWidget(self.model)
        head.addStretch(1)
        self.status = QLabel("")
        self.status.setFont(T.font(9, QFont.Weight.DemiBold))
        head.addWidget(self.status)
        outer.addLayout(head)

        controls = QHBoxLayout()
        self.energy = Segmented((("▼ Cool down", -1), ("● Hold", 0), ("▲ Lift", 1)), 0)
        self.energy.changed.connect(lambda v: self.lift.emit(v))
        controls.addWidget(self.energy)
        controls.addStretch(1)
        self.skip_button = QPushButton("⏩  Skip to the mix")
        self.skip_button.setObjectName("chip")
        self.skip_button.setToolTip("Jump to 8 bars before Kev's queued transition")
        self.skip_button.clicked.connect(lambda: self.skip.emit())
        self.skip_button.setEnabled(False)
        controls.addWidget(self.skip_button)
        self.repick_button = QPushButton("↻  Pick again")
        self.repick_button.setObjectName("chip")
        self.repick_button.clicked.connect(lambda: self.repick.emit())
        controls.addWidget(self.repick_button)
        outer.addLayout(controls)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("body")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(caps("Next track"))
        row.addStretch(1)
        self.pick_note = caps("", T.FAINT)
        row.addWidget(self.pick_note)
        lay.addLayout(row)
        self.list = QVBoxLayout()
        self.list.setSpacing(4)
        lay.addLayout(self.list)

        lay.addSpacing(4)
        row = QHBoxLayout()
        row.addWidget(caps("How to mix"))
        row.addStretch(1)
        self.mix_note = caps("", T.FAINT)
        row.addWidget(self.mix_note)
        lay.addLayout(row)
        self.techniques = ProbBars()
        lay.addWidget(self.techniques)
        self.plan = label("", 9.5, color=T.TEXT)
        self.plan.setTextFormat(Qt.TextFormat.RichText)
        self.plan.setWordWrap(True)
        lay.addWidget(self.plan)

        lay.addSpacing(4)
        lay.addWidget(caps("Kev's log"))
        self.log_box = QVBoxLayout()
        self.log_box.setSpacing(2)
        lay.addLayout(self.log_box)
        lay.addStretch(1)
        self.set_status("Pick an opening track — Kev takes it from there", "info")

    # ------------------------------------------------------------ state

    def set_status(self, text: str, tone: str = "info"):
        color = T.TONE.get(tone, T.VIOLET)
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def begin(self):
        """A new decision starts: clear the pick and plan, keep the log."""
        self._started = time.time()
        clear(self.list)
        self.rows, self.candidates = [], []
        self.pick_note.setText("")
        self.mix_note.setText("")
        self.techniques.set([])
        self.plan.setText("")
        self._lines = []
        self.set_status("Kev is thinking…", "info")

    def log(self, text: str, tone: str = "info"):
        lab = QLabel(f"<span style='color:{T.FAINT}'>{time.strftime('%H:%M:%S')}</span>&nbsp;&nbsp;{text}")
        lab.setFont(T.font(9))
        lab.setStyleSheet(f"color: {T.TONE.get(tone, T.MUTED) if tone != 'info' else T.MUTED};")
        lab.setWordWrap(True)
        self.log_box.insertWidget(0, lab)
        while self.log_box.count() > 6:
            item = self.log_box.takeAt(self.log_box.count() - 1)
            item.widget().hide()
            item.widget().deleteLater()

    def _plan_line(self, html: str):
        self._lines.append(html)
        self.plan.setText("<br>".join(self._lines))

    # ------------------------------------------------------------ events from the DJ

    def handle(self, kind: str, data: dict):
        if kind == "status":
            self.set_status(data["text"], data.get("tone", "info"))
        elif kind == "model":
            self.model.setText(data["name"].upper())
        elif kind == "shortlist":
            self._started = time.time()  # the clock for "rated all 8 in …" (the opener may have been mapped first)
            self.candidates = data["candidates"]
            clear(self.list)
            self.rows = []
            for i, c in enumerate(self.candidates, 1):
                row = CandidateRow(i, c)
                self.rows.append(row)
                self.list.addWidget(row)
            self.pick_note.setText(f"Kev rates {len(self.candidates)} candidates")
        elif kind == "rated":
            self.rows[data["index"]].update()
            done = sum(c.score is not None for c in self.candidates)
            self.pick_note.setText(f"rated {done} of {len(self.candidates)}")
        elif kind == "picked":
            # keep the pick and the three it beat most narrowly; the rest fold away to make room for the plan
            top = sorted(range(len(self.rows)), key=lambda i: -self.candidates[i].chance)[:4]
            for i, row in enumerate(self.rows):
                row.picked = i == data["index"]
                row.setVisible(i in top or row.picked)
                row.update()
            self.pick_note.setText(f"rated all {len(self.candidates)} in {time.time() - self._started:.1f} s · top 4 shown")
        elif kind == "technique":
            probs, choice = data["probs"], data["choice"]
            rows = [(TECHNIQUES[k].name, p, k == choice, "✓" if k == choice else
                     "KEV ★" if k == data["overruled"] else "") for k, p in probs.items()]
            self.techniques.set(rows)
            self.mix_note.setText("averaged over 4 option orders")
            if data["overruled"]:
                self._plan_line(f"<span style='color:{T.WARN}'>Kev was unsure ({max(probs.values()):.0%}), "
                                f"so the rules chose {TECHNIQUES[choice].name}</span>")
        elif kind == "length":
            lp = data["probs"]
            parts = [f"<b>{k}</b> {v:.0%}" if k == data["choice"] else f"{k} {v:.0%}" for k, v in lp.items()]
            self._plan_line(f"<span style='color:{T.MUTED}'>Length</span>&nbsp;&nbsp;" + " · ".join(parts))
        elif kind == "cues":
            out = next(o for o in data["outs"] if o[0] == data["out_choice"])
            inn = next(o for o in data["ins"] if o[0] == data["in_choice"])
            self._plan_line(f"<span style='color:{T.MUTED}'>Mix out</span>&nbsp;&nbsp;bar {out[1]} — {out[2]} "
                            f"<b>{data['out_probs'][out[0]]:.0%}</b>")
            self._plan_line(f"<span style='color:{T.MUTED}'>B enters</span>&nbsp;&nbsp;bar {inn[1]} — {inn[2]} "
                            f"<b>{data['in_probs'][inn[0]]:.0%}</b>")
            self._plan_line(f"<span style='color:{T.MUTED}'>Setting</span>&nbsp;&nbsp;{data['setting']}")
        elif kind == "log":
            self.log(data["text"], data.get("tone", "info"))
        elif kind == "exhausted":
            self.set_status("Every track has been played", "warn")
            self.log("No tracks left to pick — this one closes the set", "warn")
        elif kind == "error":
            self.set_status("Kev hit a problem", "bad")
            self.log(data["text"], "bad")
