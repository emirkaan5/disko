"""The mix planner: suggested technique, the techniques to choose from, their settings, effects on top,
timing and a preview."""
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QVBoxLayout, QWidget)

from ..audio import SR, Deck
from ..library import MAX_STRETCH, Track, tempo_match
from ..mixes import (EFFECTS, MAX_EFFECTS, TECHNIQUES, Automation, Plan, default_in, default_out, effect_options, layout,
                     mix_in_points, mix_out_points, recommend)
from . import theme as T
from .waveform import WaveImage
from .widgets import Glyph, Segmented, caps, clear, label


class TechniqueCard(QPushButton):
    def __init__(self, key: str):
        super().__init__()
        self.key = key
        self.setCheckable(True)
        self.setObjectName("tech")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 8, 7)
        lay.setSpacing(2)
        top = QHBoxLayout()
        self.glyph = Glyph(key, 26)
        top.addWidget(self.glyph)
        top.addStretch(1)
        self.star = label("", 10, color=T.VIOLET)
        top.addWidget(self.star, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(top)
        self.name = label(TECHNIQUES[key].name, 10.5, QFont.Weight.DemiBold)
        lay.addWidget(self.name)
        for w in (self.glyph, self.star, self.name):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setStyleSheet(f"""
            QPushButton#tech {{ background: {T.PANEL_HI}; border: 1px solid {T.BORDER}; border-radius: 11px; padding: 0; }}
            QPushButton#tech:hover {{ border-color: {T.FAINT}; }}
            QPushButton#tech:checked {{ background: {T.rgba(T.VIOLET, 0.14)}; border: 1.5px solid {T.VIOLET}; }}
            QPushButton#tech:disabled {{ background: transparent; border-style: dashed; }}""")

    def set_suggested(self, on: bool):
        self.star.setText("★" if on else "")
        self.setToolTip("Suggested for this pair" if on else "")

    def setEnabled(self, on: bool):
        super().setEnabled(on)
        self.name.setStyleSheet(f"color: {T.TEXT if on else T.FAINT};")


class TransitionPreview(QWidget):
    """Both decks across the transition window, with the automation drawn over them."""

    PRE, POST = 8.0, 8.0  # beats of context either side

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(150)
        self.data = None

    def set(self, data):
        self.data = data
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(T.qc(T.BORDER), 1))
        p.setBrush(T.qc("#0b0e14"))
        p.drawRoundedRect(r, 10, 10)
        if not self.data:
            p.setPen(T.qc(T.FAINT))
            p.setFont(T.font(10))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, "Loading the track…")
            return
        a, a_img, b, b_img, auto, start, b_offset, spb = self.data
        L = auto.beats
        b0, b1 = -self.PRE, L + self.POST
        inner = r.adjusted(26, 24, -10, -22)
        x = lambda beat: inner.left() + (beat - b0) / (b1 - b0) * inner.width()
        lane_h = (inner.height() - 10) / 2
        lanes = {"A": QRectF(inner.left(), inner.top(), inner.width(), lane_h),
                 "B": QRectF(inner.left(), inner.top() + lane_h + 10, inner.width(), lane_h)}

        # the transition window and the grid
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(T.qc(T.VIOLET, 22))
        p.drawRect(QRectF(x(0), inner.top() - 4, x(L) - x(0), inner.height() + 8))
        p.setFont(T.font(7.5, QFont.Weight.Medium, mono=True))
        step = 4 if L <= 32 else 8 if L <= 128 else 16  # label every 1, 2 or 4 bars
        for beat in range(int(np.ceil(b0 / 4)) * 4, int(b1) + 1, 4):
            phrase = beat % 32 == 0
            p.setPen(QPen(T.qc("#ffffff", 70 if phrase else 22), 1))
            p.drawLine(QPointF(x(beat), inner.top()), QPointF(x(beat), inner.bottom()))
            if beat % (step * 4) == 0:
                p.setPen(T.qc(T.MUTED))
                p.drawText(QPointF(x(beat) + 3, r.top() + 15), f"{beat // 4:+d}" if beat else "bar 0")

        # waveforms: deck A until the transition ends, deck B from where it enters, each piece of
        # audio where its timeline plays it (a loop shows its repeats)
        for key, deck, img, offset, lane, accent in (("A", a, a_img, start, lanes["A"], T.DECK_A),
                                                     ("B", b, b_img, b_offset, lanes["B"], T.DECK_B)):
            if img is not None:
                cuts = [b0] + [beat for beat, _ in auto.timeline.get(key, []) if b0 < beat < b1] + [b1]
                for lo, hi in zip(cuts, cuts[1:]):
                    src = auto.source_beat(key, lo)
                    t0 = (offset + src * spb) / SR * deck.ratio
                    t1 = (offset + (src + hi - lo) * spb) / SR * deck.ratio
                    img.draw(p, QRectF(x(lo), lane.top(), x(hi) - x(lo), lane.height()), t0, t1)
            for deck_key, at, until, length in auto.loops:
                if deck_key != key:
                    continue
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(T.qc(T.WARN, 34))
                p.drawRect(QRectF(x(at), lane.top(), x(until) - x(at), lane.height()))
                p.setPen(QPen(T.qc(T.WARN, 150), 1))
                beat = at + length
                while beat < until - 1e-6:
                    p.drawLine(QPointF(x(beat), lane.top() + 2), QPointF(x(beat), lane.top() + 8))
                    beat += length
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc("#0b0e14", 185))
            if lane is lanes["A"]:
                p.drawRect(QRectF(x(L), lane.top(), lane.right() - x(L), lane.height()))
            else:
                p.drawRect(QRectF(lane.left(), lane.top(), x(auto.b_start) - lane.left(), lane.height()))
            p.setFont(T.font(9, QFont.Weight.Bold))
            p.setPen(T.qc(accent))
            p.drawText(QRectF(r.left() + 8, lane.top(), 16, lane.height()), Qt.AlignmentFlag.AlignVCenter,
                       "A" if lane is lanes["A"] else "B")

        # automation
        def curve(deck, names, color, width, style, bipolar=False, top=1.0):
            """A lane (or the product of several, e.g. fader x dry) drawn from 0 to `top` up its deck's lane."""
            if not any((deck, n) in auto.lanes for n in names):
                return
            lane = lanes[deck]
            path = QPainterPath()
            for i, beat in enumerate(np.linspace(b0, b1, 240)):
                v = float(np.prod([auto.value(deck, n, min(max(beat, 0), L)) for n in names]))
                y = (lane.center().y() - v * lane.height() / 2 if bipolar else
                     lane.bottom() - min(v / top, 1.0) * (lane.height() - 4) - 2)
                (path.moveTo if i == 0 else path.lineTo)(QPointF(x(beat), y))
            p.setPen(QPen(T.qc(color), width, style, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        boosted = any(v > 1.0 for (_, name), pts in auto.lanes.items() if name == "high" for _, v in pts)
        for deck, accent in (("A", T.DECK_A), ("B", T.DECK_B)):
            curve(deck, ["low"], T.BASS_LANE, 1.6, Qt.PenStyle.DashLine)
            if boosted:  # treble on a 0-2 scale: halfway up is 0 dB
                curve(deck, ["high"], T.WAVE_HIGH, 1.3, Qt.PenStyle.DashDotLine, top=2.0)
            curve(deck, ["filter"], T.VIOLET, 1.6, Qt.PenStyle.SolidLine, bipolar=True)
            curve(deck, ["echo"], T.GOOD, 1.6, Qt.PenStyle.DotLine)
            curve(deck, ["reverb"], T.VOCAL_LANE, 1.6, Qt.PenStyle.DotLine)
            curve(deck, ["fader", "dry"], accent, 2.4, Qt.PenStyle.SolidLine)

        # marks (bass swap, cut) as pills on their line, between the lanes
        p.setFont(T.font(7.5, QFont.Weight.DemiBold, spacing=0.9))
        mid_y = lanes["A"].bottom() + 5
        placed = []  # a pill that would cover another moves to the top, then the bottom, of the lanes
        for beat, text in sorted(auto.marks):
            p.setPen(QPen(T.qc(T.TEXT, 200), 1.2, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x(beat), inner.top()), QPointF(x(beat), inner.bottom()))
            w = p.fontMetrics().horizontalAdvance(text.upper()) + 12
            left = min(x(beat) - w / 2, inner.right() - w)
            for y in (mid_y - 8, inner.top() + 2, inner.bottom() - 18):
                pill = QRectF(left, y, w, 16)
                if not any(pill.adjusted(-3, 0, 3, 0).intersects(q) for q in placed):
                    break
            placed.append(pill)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(T.qc(T.TEXT))
            p.drawRoundedRect(pill, 8, 8)
            p.setPen(T.qc(T.BG))
            p.drawText(pill, Qt.AlignmentFlag.AlignCenter, text.upper())

        # legend along the bottom
        items = [("level", T.TEXT, Qt.PenStyle.SolidLine), ("bass EQ", T.BASS_LANE, Qt.PenStyle.DashLine),
                 ("filter", T.VIOLET, Qt.PenStyle.SolidLine), ("echo", T.GOOD, Qt.PenStyle.DotLine),
                 ("reverb", T.VOCAL_LANE, Qt.PenStyle.DotLine)]
        if boosted:
            items.insert(2, ("treble", T.WAVE_HIGH, Qt.PenStyle.DashDotLine))
        lx = r.right() - 10
        p.setFont(T.font(8))
        for name, color, style in reversed(items):
            lx -= p.fontMetrics().horizontalAdvance(name)
            p.setPen(T.qc(T.MUTED))
            p.drawText(QPointF(lx, r.bottom() - 6), name)
            p.setPen(QPen(T.qc(color), 2, style))
            p.drawLine(QPointF(lx - 18, r.bottom() - 10), QPointF(lx - 5, r.bottom() - 10))
            lx -= 32


class Planner(QFrame):
    back = Signal()
    start = Signal(object)  # Plan

    def __init__(self):
        super().__init__()
        self.setObjectName("panel")
        self.a: Deck | None = None
        self.a_image = None
        self.b: Track | None = None
        self.b_native = self.b_synced = None
        self.b_image = None
        self.a_pos = 0
        self.master_bpm = 120.0
        self.technique, self.params, self.sync = "bass_swap", {}, True
        self.effects, self.riser_end = [], "cut"
        self.out_bar = self.in_bar = 1

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("body")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(18, 16, 16, 6)
        lay.setSpacing(9)

        head = QHBoxLayout()
        dot = label("●", 9, color=T.VIOLET)
        head.addWidget(dot)
        head.addWidget(caps("Mix plan", T.TEXT, 9))
        head.addStretch(1)
        lay.addLayout(head)
        self.title = label("", 17, QFont.Weight.DemiBold)
        self.title.setMinimumWidth(10)
        self.subtitle = label("", 10.5, color=T.MUTED)
        lay.addWidget(self.title)
        lay.addWidget(self.subtitle)

        self.suggest = QFrame()
        self.suggest.setObjectName("card")
        sl = QVBoxLayout(self.suggest)
        sl.setContentsMargins(14, 10, 12, 11)
        sl.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(caps("Suggested", T.VIOLET))
        self.suggest_name = label("", 12.5, QFont.Weight.DemiBold)
        row.addSpacing(6)
        row.addWidget(self.suggest_name)
        row.addStretch(1)
        self.use_suggestion = QPushButton("Use it")
        self.use_suggestion.setObjectName("chip")
        self.use_suggestion.clicked.connect(self._apply_suggestion)
        row.addWidget(self.use_suggestion)
        sl.addLayout(row)
        self.facts = QGridLayout()
        self.facts.setHorizontalSpacing(16)
        self.facts.setVerticalSpacing(3)
        sl.addLayout(self.facts)
        self.why = label("", 10, QFont.Weight.Medium)
        self.why.setWordWrap(True)
        sl.addWidget(self.why)
        lay.addWidget(self.suggest)

        lay.addWidget(caps("Technique"))
        cards = QGridLayout()
        cards.setSpacing(8)
        self.cards = {}
        for i, key in enumerate(TECHNIQUES):  # three to a row
            c = TechniqueCard(key)
            c.clicked.connect(lambda _=False, k=key: self._select(k))
            self.cards[key] = c
            cards.addWidget(c, i // 3, i % 3)
        lay.addLayout(cards)
        self.blurb = label("", 10, color=T.MUTED)
        self.blurb.setWordWrap(True)
        lay.addWidget(self.blurb)

        self.param_grid = QGridLayout()
        self.param_grid.setHorizontalSpacing(14)
        self.param_grid.setVerticalSpacing(5)
        self.param_grid.setColumnMinimumWidth(0, 110)
        lay.addLayout(self.param_grid)

        fx = QHBoxLayout()
        fx.setSpacing(6)
        fx_label = caps("Effects")
        fx_label.setFixedWidth(110)
        fx_label.setToolTip(f"Up to {MAX_EFFECTS} on top of the technique; one throw at a time")
        fx.addWidget(fx_label)
        self.fx_buttons = {}
        for key, effect in EFFECTS.items():
            btn = QPushButton(effect.name)
            btn.setObjectName("chip")
            btn.setCheckable(True)
            btn.setToolTip(effect.blurb)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda on, k=key: self._effect(k, on))
            self.fx_buttons[key] = btn
            fx.addWidget(btn)
        fx.addStretch(1)
        lay.addLayout(fx)
        riser = QHBoxLayout()
        self.riser_label = caps("Riser ends")
        self.riser_label.setFixedWidth(110)
        riser.addWidget(self.riser_label)
        self.riser_seg = Segmented((("cut at the top", "cut"), ("filter drops back", "release")), "cut")
        self.riser_seg.changed.connect(self._riser_end)
        riser.addWidget(self.riser_seg, 1)
        lay.addLayout(riser)

        timing = QGridLayout()
        timing.setHorizontalSpacing(10)
        timing.setVerticalSpacing(6)
        timing.addWidget(caps("Mix out at"), 0, 0)
        timing.addWidget(caps("B enters at"), 0, 1)
        self.out_combo = QComboBox()
        self.out_combo.activated.connect(self._out_chosen)
        timing.addWidget(self.out_combo, 1, 0)
        self.in_combo = QComboBox()
        self.in_combo.activated.connect(self._in_chosen)
        timing.addWidget(self.in_combo, 1, 1)
        timing.setColumnStretch(0, 1)
        timing.setColumnStretch(1, 1)
        lay.addLayout(timing)
        tempo_row = QHBoxLayout()
        self.tempo_label = caps("Tempo")
        self.tempo_label.setFixedWidth(110)
        tempo_row.addWidget(self.tempo_label)
        self.tempo = Segmented((("Match set tempo", True), ("B's own tempo", False)), True)
        self.tempo.changed.connect(self._tempo_chosen)
        tempo_row.addWidget(self.tempo, 1)
        lay.addLayout(tempo_row)

        lay.addStretch(1)

        # the preview stays in view below the scrolling settings
        self.preview = TransitionPreview()
        self.preview.setFixedHeight(160)
        holder = QHBoxLayout()
        holder.setContentsMargins(18, 6, 16, 0)
        holder.addWidget(self.preview)
        outer.addLayout(holder)

        foot = QHBoxLayout()
        foot.setContentsMargins(18, 8, 16, 14)
        self.status = label("", 9.5, color=T.MUTED)
        foot.addWidget(self.status, 1)
        back = QPushButton("Back")
        back.setObjectName("ghost")
        back.clicked.connect(self.back)
        foot.addWidget(back)
        self.go = QPushButton("Start mix  ▶")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self._start)
        foot.addWidget(self.go)
        outer.addLayout(foot)

    # ------------------------------------------------------------ opening a plan

    def open(self, a: Deck, a_image: WaveImage, b: Track, a_pos: int):
        self.a, self.a_image, self.b, self.a_pos = a, a_image, b, a_pos
        self.master_bpm = a.bpm
        self.b_native = self.b_synced = self.b_image = None
        self.effects = []
        self.title.setText(f"Into “{b.title}”")
        self.subtitle.setText(b.artist + (f"  ·  {', '.join(b.genre_list[:3])}" if b.genre_list else ""))
        dev = tempo_match(self.master_bpm, b.bpm)[1]

        self.suggestion = recommend(a.track, b, self.master_bpm)
        self.suggest_name.setText(self._describe(self.suggestion.technique, self.suggestion.params))
        clear(self.facts)
        *facts, (tone, why) = self.suggestion.reasons
        for i, (fact_tone, text) in enumerate(facts):
            lab = QLabel(f"<span style='color:{T.TONE[fact_tone]}'>●</span>&nbsp;&nbsp;{text}")
            lab.setFont(T.font(9.5))
            lab.setStyleSheet(f"color: {T.TEXT};")
            self.facts.addWidget(lab, i // 2, i % 2)
        self.why.setText(why)
        self.why.setStyleSheet(f"color: {T.TONE[tone]};")
        syncable = bool(dev <= MAX_STRETCH)
        for key, card in self.cards.items():
            ok = not TECHNIQUES[key].blend or syncable
            card.setEnabled(ok)
            card.setToolTip("" if ok else f"Tempos are {dev:.0%} apart: too far to blend")
            card.set_suggested(key == self.suggestion.technique)
        self.tempo.group.button(0).setEnabled(syncable)
        self._apply_suggestion()

    def set_b(self, native: Deck | None, synced: Deck | None, image: WaveImage | None):
        self.b_native, self.b_synced, self.b_image = native, synced, image
        self._refresh()

    def tick(self, a_pos: int):
        self.a_pos = a_pos
        self._refresh(keep=True)

    # ------------------------------------------------------------ choices

    def _describe(self, key: str, params: dict) -> str:
        t = TECHNIQUES[key]
        return f"{t.name} · {params['bars']} bars" if "bars" in params else t.name

    def _apply_suggestion(self):
        s = self.suggestion
        self.sync = s.sync
        self._select(s.technique, dict(s.params), reset_timing=True)

    def _select(self, key: str, params: dict | None = None, reset_timing: bool = True):
        self.technique = key
        self.params = params if params is not None else TECHNIQUES[key].defaults()
        tech = TECHNIQUES[key]
        if tech.blend:
            self.sync = True
        for k, card in self.cards.items():
            card.setChecked(k == key)
        self.blurb.setText(tech.blurb)
        clear(self.param_grid)
        for row, prm in enumerate(tech.params):
            self.param_grid.addWidget(caps(prm.label), row, 0)
            seg = Segmented(prm.options, self.params[prm.key])
            seg.changed.connect(lambda v, k=prm.key: self._param(k, v))
            self.param_grid.addWidget(seg, row, 1)
        self.param_grid.setColumnStretch(1, 1)
        fits = effect_options(key)
        self.effects = [k for k in self.effects if k in fits]
        for k, btn in self.fx_buttons.items():
            btn.setVisible(k in fits)
        self._sync_effects()
        self.tempo.setVisible(not tech.blend)
        self.tempo_label.setVisible(not tech.blend)
        self.tempo.set(self.sync)
        self._refresh(keep=not reset_timing)

    def _effect(self, key: str, on: bool):
        """Toggle an effect: one throw at a time, and the oldest drops off past MAX_EFFECTS."""
        group = EFFECTS[key].group
        self.effects = [k for k in self.effects if k != key and not (on and group and EFFECTS[k].group == group)]
        if on:
            self.effects = (self.effects + [key])[-MAX_EFFECTS:]
        self._sync_effects()
        self._refresh(keep=True)

    def _riser_end(self, value):
        self.riser_end = value
        self._refresh(keep=True)

    def _sync_effects(self):
        for k, btn in self.fx_buttons.items():
            btn.setChecked(k in self.effects)
        show = "riser" in self.effects and TECHNIQUES[self.technique].blend  # a cut drops A at the top anyway
        self.riser_label.setVisible(show)
        self.riser_seg.setVisible(show)

    def _param(self, key, value):
        self.params[key] = value
        self._refresh(keep=True)

    def _tempo_chosen(self, value):
        self.sync = value
        self._refresh(keep=True)

    def _out_chosen(self, i):
        self.out_bar = self.out_combo.itemData(i)
        self._refresh(keep=True)

    def _in_chosen(self, i):
        self.in_bar = self.in_combo.itemData(i)
        self._refresh(keep=True)

    def b_deck(self) -> Deck | None:
        return self.b_synced if self.sync else self.b_native

    def plan(self) -> Plan:
        return Plan(self.technique, dict(self.params), self.sync, self.out_bar, self.in_bar,
                    effects=list(self.effects), riser_end=self.riser_end)

    # ------------------------------------------------------------ keeping it consistent

    def _refresh(self, keep: bool = True):
        if self.a is None or self.b is None:
            return
        tech = TECHNIQUES[self.technique]
        auto = tech.build(self.params, self.master_bpm)
        now_bar = self.a.where(self.a_pos)[0]
        lead = self.plan().automation(self.master_bpm).b_start  # effects may start the transition earlier
        outs = mix_out_points(self.a, self.a_pos, self.technique, self.params, lead)
        ins = mix_in_points(self.b, auto)
        if not (keep and self.out_bar in {bar for _, bar in outs}):
            self.out_bar = default_out(outs, now_bar) if outs else 0
        if not (keep and self.in_bar in {bar for _, bar in ins}):
            self.in_bar = default_in(ins, self.technique)
        self._fill(self.out_combo, outs, self.out_bar, lambda bar: f"in {bar - now_bar} bars")
        self._fill(self.in_combo, ins, self.in_bar, lambda bar: "")

        auto = self.plan().automation(self.master_bpm, self.b, self.a.track)  # swap on B's bass, A's loop, effects
        deck = self.b_deck()
        ratio = deck.ratio if deck else 1.0
        virtual_b = deck or Deck(self.b, np.zeros((0, 2), np.float32), ratio)
        if outs:
            start, b_offset, n, spb = layout(self.plan(), self.a, virtual_b, auto)
            self.preview.set((self.a, self.a_image, virtual_b, self.b_image, auto, start, b_offset, spb))
            seconds_away = (start - self.a_pos) / SR
            when = f"starts in {max(seconds_away, 0):.0f} s" if seconds_away < 120 else f"starts in {seconds_away / 60:.1f} min"
            self.status.setText(f"{self._describe(self.technique, self.params)} · {n / SR:.0f} s · {when}")
        else:
            self.preview.set(None)
            self.status.setText("Not enough of this track left for that — try a shorter mix or a cut")
        ready = deck is not None and bool(outs)
        self.go.setEnabled(ready)
        if deck is None:
            self.status.setText(self.status.text() + ("  ·  syncing B to the set tempo…" if self.sync else "  ·  loading B…"))

    def _fill(self, combo: QComboBox, points, current, note):
        items = [(f"{name} — bar {bar}" + (f"  ({note(bar)})" if note(bar) else ""), bar) for name, bar in points]
        if [combo.itemData(i) for i in range(combo.count())] != [b for _, b in items] or \
                [combo.itemText(i) for i in range(combo.count())] != [t for t, _ in items]:
            combo.blockSignals(True)
            combo.clear()
            for text, bar in items:
                combo.addItem(text, bar)
            combo.blockSignals(False)
        i = combo.findData(current)
        if i >= 0 and combo.currentIndex() != i:
            combo.setCurrentIndex(i)

    def _start(self):
        if self.go.isEnabled():
            self.start.emit(self.plan())
