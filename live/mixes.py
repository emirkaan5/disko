"""Five ways to mix two tracks: the automation each one writes, its settings, and which one fits."""
import math
from dataclasses import dataclass, field

import numpy as np

from .audio import SR, Deck, kick_onsets
from .dsp import render
from .library import MAX_STRETCH, Track, key_relation, tempo_match

LANE_DEFAULTS = {"fader": 1.0, "low": 1.0, "mid": 1.0, "high": 1.0,
                 "filter": 0.0, "echo": 0.0, "feedback": 0.0, "dry": 1.0}


@dataclass
class Automation:
    """Per-deck parameter curves across a transition, as (beat, value) breakpoints joined by lines.

    Beat 0 is the transition's first sample; deck B's audio starts at `b_start`. Lanes: fader, EQ
    low/mid/high (1 = 0 dB, 0 = kill), filter (-1 low-pass ... +1 high-pass), echo send, echo
    feedback, dry (0 cuts the deck but lets its echoes ring)."""
    beats: float
    b_start: float = 0.0
    echo_beats: float = 0.5
    resonance: float = 1.0
    lanes: dict = field(default_factory=dict)
    marks: list = field(default_factory=list)  # (beat, label)

    def set(self, deck: str, lane: str, *points):
        self.lanes[deck, lane] = sorted(points)

    def value(self, deck: str, lane: str, beat: float) -> float:
        pts = self.lanes.get((deck, lane))
        if not pts:
            return LANE_DEFAULTS[lane]
        xs, ys = zip(*pts)
        return float(np.interp(beat, xs, ys))

    def curve(self, deck: str, lane: str, n: int, spb: float) -> np.ndarray:
        pts = self.lanes.get((deck, lane))
        if not pts:
            return np.full(n, LANE_DEFAULTS[lane], np.float32)
        xs, ys = zip(*pts)
        return np.interp(np.arange(n), np.array(xs) * spb, ys).astype(np.float32)


# ---------------------------------------------------------------- the five techniques

def _bass_swap(p, bpm):
    L = p["bars"] * 4
    s = max(4, round(L * p["swap"] / 4) * 4)  # on a bar line
    dip = 10 ** (p["mid_dip"] / 20)
    a = Automation(L, marks=[(s, "bass swap")])
    a.set("B", "fader", (0, 0), (min(4, L / 4), 1))
    a.set("B", "high", (0, 0.2), (max(2, L * p["highs"]), 1))
    a.set("B", "mid", (0, dip), (s, dip), (min(L, s + 8), 1))
    a.set("B", "low", (0, 0), (s - 0.05, 0), (s + 0.05, 1))
    a.set("A", "low", (0, 1), (s - 0.05, 1), (s + 0.05, 0))
    a.set("A", "mid", (0, 1), (s, 1), (L, 0))
    a.set("A", "high", (0, 1), (s + (L - s) / 3, 1), (L, 0))
    a.set("A", "fader", (0, 1), (s + (L - s) / 2, 1), (L, 0))
    return a


def _filter_sweep(p, bpm):
    L, d = p["bars"] * 4, p["depth"]
    a = Automation(L, resonance=p["resonance"])
    a.set("A", "filter", (0, 0), (L * 0.5, d * 0.4), (L * 0.8, d * 0.8), (L, d))
    a.set("A", "fader", (0, 1), (L * 0.6, 1), (L, 0))
    a.set("B", "filter", (0, -d), (L * 0.5, -d * 0.45), (L * 0.85, -0.08), (L * 0.95, 0))
    a.set("B", "fader", (0, 0), (L * 0.25, 1))
    if p["bass_hold"]:
        h = round(L / 8) * 4
        a.set("B", "low", (0, 0), (h - 0.05, 0), (h + 0.05, 1))
        a.set("A", "low", (0, 1), (h - 0.05, 1), (h + 0.05, 0))
        a.marks.append((h, "bass swap"))
    return a


def _echo_out(p, bpm):
    lead, tail, fb = p["lead"], p["tail"] * 4, p["feedback"]
    cut, L = lead, lead + tail
    a = Automation(L, b_start=cut, echo_beats=p["time"], marks=[(cut, "cut")])
    a.set("A", "echo", (0, 0.3), (cut - 0.02, 1), (cut, 0))
    a.set("A", "feedback", (0, fb), (cut + tail * 0.5, fb), (L, fb * 0.5))
    a.set("A", "dry", (0, 1), (cut - 0.03, 1), (cut, 0))
    a.set("A", "filter", (0, 0), (cut, 0), (L, 0.55))
    a.set("B", "fader", (0, 0), (cut, 0), (cut + 0.03, 1))
    return a


def _drop_cut(p, bpm):
    pre = p["bass_kill"]
    edge = p["edge"] / 1000 * bpm / 60  # ms -> beats
    cut = max(pre, edge)
    L = cut + max(1.0, edge * 2)
    a = Automation(L, b_start=cut, marks=[(cut, "cut")])
    if pre:
        a.set("A", "low", (0, 1), (cut - pre, 1), (cut - pre + 0.05, 0))
    a.set("A", "fader", (0, 1), (cut - edge, 1), (cut, 0))
    a.set("B", "fader", (0, 0), (cut, 0), (cut + edge, 1))
    return a


def _smooth_fade(p, bpm):
    L = p["bars"] * 4
    x = np.linspace(0, 1, 17)
    if p["curve"] == "power":
        fa, fb = np.cos(x * math.pi / 2), np.sin(x * math.pi / 2)
    elif p["curve"] == "linear":
        fa, fb = 1 - x, x
    else:
        s = x * x * (3 - 2 * x)
        fa, fb = 1 - s, s
    a = Automation(L)
    a.set("A", "fader", *zip(x * L, fa))
    a.set("B", "fader", *zip(x * L, fb))
    if p["bass_swap"]:
        h = round(L / 8) * 4
        a.set("A", "low", (0, 1), (h - 0.05, 1), (h + 0.05, 0))
        a.set("B", "low", (0, 0), (h - 0.05, 0), (h + 0.05, 1))
        a.marks.append((h, "bass swap"))
    return a


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    options: tuple  # ((label, value), ...)
    default: object


@dataclass(frozen=True)
class Technique:
    key: str
    name: str
    blurb: str
    blend: bool     # both tracks play together for bars, so tempos must match
    params: tuple
    build: object   # (params, bpm) -> Automation

    def defaults(self) -> dict:
        return {p.key: p.default for p in self.params}


def _bars(*bars):
    return tuple((f"{b}", b) for b in bars)


TECHNIQUES = {t.key: t for t in (
    Technique("bass_swap", "Bass Swap", "Long EQ blend; the basslines trade places on a downbeat.", True, (
        Param("bars", "Length (bars)", _bars(8, 16, 32, 64), 16),
        Param("swap", "Swap point", (("¼", 0.25), ("½", 0.5), ("¾", 0.75)), 0.5),
        Param("mid_dip", "Incoming mids", (("0 dB", 0), ("−3", -3), ("−6", -6), ("−9", -9), ("−12", -12)), -6),
        Param("highs", "Highs come in", (("fast", 0.1), ("gradual", 0.25), ("slow", 0.5)), 0.25),
    ), _bass_swap),
    Technique("filter_sweep", "Filter Sweep", "High-pass the old track away while the new one opens up.", True, (
        Param("bars", "Length (bars)", _bars(4, 8, 16, 32), 8),
        Param("depth", "Sweep depth", (("50%", 0.5), ("70%", 0.7), ("85%", 0.85), ("100%", 1.0)), 0.85),
        Param("resonance", "Resonance", (("soft", 0.9), ("medium", 1.8), ("sharp", 3.5)), 1.8),
        Param("bass_hold", "Incoming bass", (("from start", False), ("swap halfway", True)), True),
    ), _filter_sweep),
    Technique("echo_out", "Echo Out", "Throw the old track into an echo, drop the new one on the beat.", False, (
        Param("time", "Echo time", (("¼ beat", 0.25), ("½ beat", 0.5), ("¾ beat", 0.75), ("1 beat", 1.0)), 0.5),
        Param("feedback", "Feedback", (("short", 0.35), ("medium", 0.55), ("long", 0.72)), 0.55),
        Param("lead", "Build-up", (("1 beat", 1), ("2 beats", 2), ("1 bar", 4)), 2),
        Param("tail", "Tail (bars)", _bars(1, 2, 4, 8), 4),
    ), _echo_out),
    Technique("drop_cut", "Drop Cut", "Hard switch on the downbeat, straight into the new track.", False, (
        Param("bass_kill", "Kill A's bass", (("no", 0), ("1 beat", 1), ("2 beats", 2), ("1 bar", 4)), 2),
        Param("edge", "Cut edge", (("5 ms", 5), ("15 ms", 15), ("40 ms", 40), ("120 ms", 120)), 15),
    ), _drop_cut),
    Technique("smooth_fade", "Smooth Fade", "Classic crossfade; gentle, works for breakdowns and ambient.", True, (
        Param("bars", "Length (bars)", _bars(4, 8, 16, 32), 8),
        Param("curve", "Curve", (("equal power", "power"), ("linear", "linear"), ("s-curve", "s")), "power"),
        Param("bass_swap", "Basses", (("both on", False), ("swap midway", True)), True),
    ), _smooth_fade),
)}


# ---------------------------------------------------------------- choosing one

@dataclass
class Suggestion:
    technique: str
    params: dict
    sync: bool
    reasons: list  # (tone, text), tone in good / warn / bad / info


def recommend(a: Track, b: Track, master_bpm: float) -> Suggestion:
    factor, dev = tempo_match(master_bpm, b.bpm)
    k, label = key_relation(a.camelot, b.camelot)
    lift = b.energy - a.energy
    vocal_clash = a.vocals > 0.1 and b.vocals > 0.1 and a.vocals_out_bars < 8 and b.vocals_in_bars < 8
    reasons = [
        ("good" if dev <= 0.04 else "warn" if dev <= MAX_STRETCH else "bad",
         f"{master_bpm:.1f} → {b.bpm:.1f} BPM ({(factor - 1) * 100:+.1f}%)"),
        ("good" if k >= 0.85 else "warn" if k >= 0.55 else "bad", f"{a.camelot} → {b.camelot}: {label}"),
        ("info", f"Energy {a.energy:.1f} → {b.energy:.1f} ({lift:+.1f})"),
        ("good" if b.bass_in_bars >= 8 else "info", f"B's bass enters after {b.bass_in_bars} bars"),
    ]

    def pick(key, tone, why, sync=True, **params):
        return Suggestion(key, {**TECHNIQUES[key].defaults(), **params}, sync, reasons + [(tone, why)])

    if dev > MAX_STRETCH:
        if lift > 0.3 and b.first_drop_bar:
            return pick("drop_cut", "info", "Too far apart to beatmatch, and B lifts the energy: cut into its drop", sync=False)
        return pick("echo_out", "info", "Too far apart to beatmatch: echo A out, start B at its own tempo", sync=False)
    if vocal_clash:
        return pick("filter_sweep", "warn", "Both have vocals at the joint: keep the overlap short and filtered", bars=8)
    if k < 0.55:
        return pick("filter_sweep", "warn", "The keys clash: a short filtered overlap hides it", bars=8)
    if lift > 0.8 and b.first_drop_bar:
        return pick("drop_cut", "info", f"Big energy lift: drop straight into B's first drop")
    if lift < -0.8:
        return pick("echo_out", "info", "Energy falls: an echo-out resets the room")
    if b.bass_in_bars >= 8:
        bars = 32 if b.bass_in_bars >= 32 else 16 if b.bass_in_bars >= 16 else 8
        return pick("bass_swap", "good", f"B has {b.bass_in_bars} bars before its bass: room for a {bars}-bar blend",
                    bars=bars)
    return pick("smooth_fade", "info", f"B's bass comes in early (bar {b.bass_in_bars + 1}): a short fade", bars=8)


# ---------------------------------------------------------------- where it happens

@dataclass
class Plan:
    technique: str
    params: dict
    sync: bool    # deck B time-stretched to the set tempo
    out_bar: int  # bar of deck A where the mix lands (the cut, or the start of a blend)
    in_bar: int   # bar of deck B that lands there

    @property
    def tech(self) -> Technique:
        return TECHNIQUES[self.technique]

    def automation(self, bpm: float) -> Automation:
        return self.tech.build(self.params, bpm)


def bars_after_out(technique: str, params: dict) -> int:
    """Bars of deck A the transition still needs after its mix-out bar."""
    return params["bars"] if TECHNIQUES[technique].blend else 1


def mix_window(a: Deck, now: int, technique: str, params: dict, lead_beats: float) -> tuple:
    """(first, last) bar of deck A where the mix can land: far enough ahead to render, early enough to fit."""
    db = a.downbeats
    ready = now / SR + 2.0 + lead_beats * a.spb / SR
    return int(np.searchsorted(db, ready)) + 1, len(db) - bars_after_out(technique, params) + 1


def mix_out_points(a: Deck, now: int, technique: str, params: dict, lead_beats: float) -> list:
    """[(label, bar)] where deck A can hand over, all far enough ahead to render and still fit."""
    db = a.downbeats
    first, last = mix_window(a, now, technique, params, lead_beats)
    if first > last:
        return []
    phrase = first + (-(first - 1)) % 8
    picks = [("Next bar", first), ("Next phrase", phrase), ("Phrase after", phrase + 8)]
    if a.track.outro_bars:
        picks.append(("Outro starts", len(db) - a.track.outro_bars + 1))
    if a.track.vocals > 0.05 and a.track.vocals_out_bars:
        picks.append(("Vocals end", len(db) - a.track.vocals_out_bars + 1))
    picks.append(("Latest", last))
    seen = {}
    for label, bar in picks:
        if first <= bar <= last:
            seen.setdefault(bar, label)
    return sorted(((label, bar) for bar, label in seen.items()), key=lambda lb: lb[1])


def default_out(points: list, now_bar: int) -> int:
    """The outro if it is close, else the next phrase."""
    bars = dict(points)
    if "Outro starts" in bars and bars["Outro starts"] - now_bar <= 32:
        return bars["Outro starts"]
    return bars.get("Next phrase", points[0][1])


def mix_in_points(b: Track, auto: Automation) -> list:
    """[(label, bar)] for where deck B can enter."""
    picks = [("Track start", 1)]
    swap = next((beat for beat, label in auto.marks if label == "bass swap"), None)
    if swap is not None and b.bass_in_bars:
        bar = b.bass_in_bars + 1 - int(round(swap / 4))
        if bar > 1:
            picks.append(("Bass lands on the swap", bar))
    picks += [("Second phrase", 9), ("Third phrase", 17)]
    if b.first_drop_bar > 1:
        picks.append(("First drop", b.first_drop_bar))
    last = b.n_bars - int(math.ceil(auto.beats / 4))
    seen = {}
    for label, bar in picks:
        if 1 <= bar <= last:
            seen.setdefault(bar, label)
    return sorted(((label, bar) for bar, label in seen.items()), key=lambda lb: lb[1])


def default_in(points: list, technique: str) -> int:
    bars = dict(points)
    if technique == "bass_swap" and "Bass lands on the swap" in bars:
        return bars["Bass lands on the swap"]
    if technique == "drop_cut" and "First drop" in bars:
        return bars["First drop"]
    return 1


# ---------------------------------------------------------------- rendering it

@dataclass(eq=False)
class Scheduled:
    """A rendered transition: from deck A's sample `start`, play `buffer`, then deck B onward."""
    start: int
    buffer: np.ndarray
    deck: Deck        # deck B
    b_offset: int     # deck B sample under the buffer's first sample (negative = not started yet)
    plan: Plan
    auto: Automation
    spb: float

    @property
    def end(self) -> int:
        return self.start + len(self.buffer)

    @property
    def b_after(self) -> int:
        return self.b_offset + len(self.buffer)


def layout(plan: Plan, a: Deck, b: Deck, auto: Automation) -> tuple:
    """(deck A start sample, deck B offset, length in samples, samples per beat)."""
    spb = a.spb
    lead = int(round(auto.b_start * spb))
    return a.bar_sample(plan.out_bar) - lead, b.bar_sample(plan.in_bar) - lead, int(round(auto.beats * spb)), spb


def _segment(audio: np.ndarray, start: int, n: int) -> np.ndarray:
    out = np.zeros((n, 2), np.float32)
    lo, hi = max(start, 0), min(start + n, len(audio))
    if hi > lo:
        out[lo - start:hi - start] = audio[lo:hi]
    return out


def align(a: Deck, start: int, b: Deck, b_offset: int, max_ms: int = 60) -> int:
    """Nudge deck B so its kicks land on deck A's (allin1's downbeats are only good to ~10-40 ms).

    Once both decks run at one measured tempo the offset is constant, so it is measured in 16-beat
    windows across up to 128 beats of both tracks (past the blend if need be) and the median of
    the windows that clearly lock is used; intros without a kick then can't mislead it."""
    win = int(16 * a.spb)
    span = min(a.length - start, b.length - b_offset, int(128 * a.spb))
    lags, quality = [], []
    for w in range(0, span - win + 1, win // 2):
        ea = kick_onsets(_segment(a.audio, start + w, win))
        eb = kick_onsets(_segment(b.audio, b_offset + w - max_ms * 44, win + 2 * max_ms * 44))
        if ea.std() < 1e-6 or eb.std() < 1e-6:
            continue
        corr = np.array([np.dot(ea, eb[k:k + len(ea)]) for k in range(2 * max_ms + 1)])
        k = int(np.argmax(corr))
        lags.append(k - max_ms)
        quality.append(corr[k] / (np.linalg.norm(ea) * np.linalg.norm(eb[k:k + len(ea)]) + 1e-9))
    locked = [lag for lag, q in zip(lags, quality) if q >= 0.35]
    if not locked:  # no steady kick to lock onto: trust allin1's grid rather than guess
        return b_offset
    return b_offset + int(np.median(locked)) * 44


def build(plan: Plan, a: Deck, b: Deck) -> Scheduled:
    auto = plan.automation(a.bpm)
    start, b_offset, n, spb = layout(plan, a, b, auto)
    if plan.tech.blend and plan.sync:
        b_offset = align(a, start, b, b_offset)
    seg_a = _segment(a.audio, start, n)
    seg_b = _segment(b.audio, b_offset, n)
    seg_b[:int(round(auto.b_start * spb))] = 0
    return Scheduled(start, render(seg_a, seg_b, auto, spb), b, b_offset, plan, auto, spb)
