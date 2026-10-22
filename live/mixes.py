"""Ways to mix two tracks: the automation each technique writes, its settings, which one fits,
and the effects that can go on top of it."""
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audio import SR, Deck, kick_onsets
from .dsp import render
from .library import MAX_STRETCH, Track, key_relation, tempo_match

LOG = Path(__file__).resolve().parent.parent / "results" / "live" / "transitions.jsonl"
LANE_DEFAULTS = {"fader": 1.0, "low": 1.0, "mid": 1.0, "high": 1.0, "filter": 0.0, "resonance": 1.0,
                 "echo": 0.0, "feedback": 0.0, "reverb": 0.0, "dry": 1.0}
SPLICE = 132  # samples (3 ms) crossfaded ahead of each loop jump


@dataclass
class Automation:
    """Per-deck parameter curves across a transition, as (beat, value) breakpoints joined by lines.

    Beat 0 is the transition's first sample; deck B's audio starts at `b_start`. Lanes: fader, EQ
    low/mid/high (1 = 0 dB, 0 = kill, 2 = +6 dB), filter (-1 low-pass ... +1 high-pass) and its
    resonance (Q), echo send and feedback, reverb send, dry (0 cuts the deck but lets its echoes
    and reverb ring).

    Each deck also has a timeline: [(beat, source beat)] jumps, from which on the deck plays its
    own audio from that source beat (beats counted from where it sits at the transition's start).
    No jumps means it simply plays on; jumping back repeats a stretch, which is how loops work.

    `exit` is the beat where deck A gives way (the bass swap, or the cut): effects build up to it."""
    beats: float
    b_start: float = 0.0
    exit: float = 0.0
    echo_beats: float = 0.5
    reverb_size: float = 0.7
    lanes: dict = field(default_factory=dict)
    marks: list = field(default_factory=list)  # (beat, label)
    timeline: dict = field(default_factory=dict)  # deck -> sorted [(beat, source beat)]
    loops: list = field(default_factory=list)  # (deck, first beat, last beat, loop length): for drawing

    def set(self, deck: str, lane: str, *points):
        self.lanes[deck, lane] = sorted(points)

    def overlay(self, deck: str, lane: str, *points):
        """Write `points` over a lane between the first and the last of them; outside that span the
        lane keeps its old values (with a step back to them right after the span, if they differ)."""
        lo, hi = points[0][0], points[-1][0]
        old = self.lanes.get((deck, lane))
        if not old:
            self.lanes[deck, lane] = sorted(points)
            return
        at_lo, at_hi = self.value(deck, lane, lo - 1e-3), self.value(deck, lane, hi + 1e-3)
        before = [(b, v) for b, v in old if b < lo - 1e-3] + [(lo - 1e-3, at_lo)]
        after = [(hi + 1e-3, at_hi)] + [(b, v) for b, v in old if b > hi + 1e-3] if hi < self.beats else []
        self.lanes[deck, lane] = before + sorted(points) + after

    def prepend(self, beats: float):
        """Start the transition `beats` earlier: everything in it moves that much later, and the
        decks play on as they were before it."""
        d = beats
        self.beats, self.b_start, self.exit = self.beats + d, self.b_start + d, self.exit + d
        self.lanes = {k: [(b + d, v) for b, v in pts] for k, pts in self.lanes.items()}
        self.marks = [(b + d, text) for b, text in self.marks]
        self.timeline = {k: [(b + d, src + d) for b, src in pts] for k, pts in self.timeline.items()}
        self.loops = [(deck, at + d, until + d, length) for deck, at, until, length in self.loops]

    def jump(self, deck: str, beat: float, source: float):
        """From `beat` on, play the deck from `source` (replacing any jump at the same beat)."""
        pts = {b: s for b, s in self.timeline.get(deck, [])}
        pts[beat] = source
        self.timeline[deck] = sorted(pts.items())

    def source_beat(self, deck: str, beat: float) -> float:
        """Which beat of the deck's own audio plays at transition beat `beat`."""
        b0, s0 = 0.0, 0.0
        for b, s in self.timeline.get(deck, []):
            if b > beat + 1e-9:
                break
            b0, s0 = b, s
        return s0 + beat - b0

    def loop(self, deck: str, at: float, length: float, until: float, resume: bool = True,
             source: float | None = None):
        """From `at` until `until`, repeat `length` beats starting at `source` (default: whatever
        plays at `at`); then, with `resume`, play on from the end of the loop."""
        src = self.source_beat(deck, at) if source is None else source
        if source is not None:
            self.jump(deck, at, src)
        t = at + length
        while t < until - 1e-6:
            self.jump(deck, t, src)
            t += length
        if resume:
            self.jump(deck, until, src + length)
        self.loops.append((deck, at, until, length))

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


# ---------------------------------------------------------------- techniques

def _bass_swap(p, bpm):
    L = p["bars"] * 4
    s = p.get("swap_at") or max(4, round(L * p["swap"] / 4) * 4)  # on a bar line
    dip = 10 ** (p["mid_dip"] / 20)
    a = Automation(L, exit=s, marks=[(s, "bass swap")])
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
    a = Automation(L, exit=round(L * 0.75 / 4) * 4)
    a.set("A", "resonance", (0, p["resonance"]))
    a.set("B", "resonance", (0, p["resonance"]))
    a.set("A", "filter", (0, 0), (L * 0.5, d * 0.4), (L * 0.8, d * 0.8), (L, d))
    a.set("A", "fader", (0, 1), (L * 0.6, 1), (L, 0))
    a.set("B", "filter", (0, -d), (L * 0.5, -d * 0.45), (L * 0.85, -0.08), (L * 0.95, 0))
    a.set("B", "fader", (0, 0), (L * 0.25, 1))
    if p["bass_hold"]:
        h = p.get("swap_at") or round(L / 8) * 4
        a.set("B", "low", (0, 0), (h - 0.05, 0), (h + 0.05, 1))
        a.set("A", "low", (0, 1), (h - 0.05, 1), (h + 0.05, 0))
        a.marks.append((h, "bass swap"))
        a.exit = h
    return a


def _echo_out(p, bpm):
    lead, tail, fb = p["lead"], p["tail"] * 4, p["feedback"]
    cut, L = lead, lead + tail
    a = Automation(L, b_start=cut, exit=cut, echo_beats=p["time"], marks=[(cut, "cut")])
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
    a = Automation(L, b_start=cut, exit=cut, marks=[(cut, "cut")])
    if pre:
        a.set("A", "low", (0, 1), (cut - pre, 1), (cut - pre + 0.05, 0))
    a.set("A", "dry", (0, 1), (cut - edge, 1), (cut, 0))  # dry, not the fader: an effect's tail may ring on
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
    a = Automation(L, exit=round(L / 8) * 4)
    a.set("A", "fader", *zip(x * L, fa))
    a.set("B", "fader", *zip(x * L, fb))
    if p["bass_swap"]:
        h = p.get("swap_at") or round(L / 8) * 4
        a.set("A", "low", (0, 1), (h - 0.05, 1), (h + 0.05, 0))
        a.set("B", "low", (0, 0), (h - 0.05, 0), (h + 0.05, 1))
        a.marks.append((h, "bass swap"))
        a.exit = h
    return a


def _reverb_wash(p, bpm):
    lead, tail = p["build"] * 4, p["tail"] * 4
    cut, L = lead, lead + tail
    a = Automation(L, b_start=cut, exit=cut, reverb_size=p["size"], marks=[(cut, "cut")])
    a.set("A", "reverb", (0, 0.1), (cut * 0.6, 0.45), (cut - 0.02, 1), (cut, 0))
    a.set("A", "dry", (0, 1), (cut - 0.03, 1), (cut, 0))
    a.set("A", "filter", (0, 0), (cut, 0.25), (L, 0.45))  # thins the swell, then keeps the wash off B's bass
    a.set("B", "fader", (0, 0), (cut, 0), (cut + 0.03, 1))
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
    Technique("reverb_wash", "Reverb Wash", "Swell the old track into a reverb and land the new one in its tail.", False, (
        Param("build", "Swell", (("1 bar", 1), ("2 bars", 2)), 1),
        Param("size", "Room", (("room", 0.6), ("hall", 0.82), ("cathedral", 0.94)), 0.82),
        Param("tail", "Tail (bars)", _bars(2, 4, 8), 4),
    ), _reverb_wash),
)}


# ---------------------------------------------------------------- effects on top

PREROLL = 16  # beats the longest effect needs before A leaves: reserved when planning a mix with effects
MAX_EFFECTS = 2


def _need(auto: Automation, beats: float):
    """Make room for `beats` before A leaves, starting the transition earlier if need be."""
    if auto.exit < beats:
        auto.prepend(beats - auto.exit)


def _tail(auto: Automation, beats: float):
    """Let the transition run at least `beats` past A's exit, for an echo or reverb to ring out."""
    auto.beats = max(auto.beats, auto.exit + beats)


def _a_leaves(auto: Automation, at: float):
    """A's dry signal stops at `at` and its sends close, so only its echo and reverb ring on; B,
    which ends at full anyway, gets there now."""
    L = auto.beats
    v = auto.value("A", "fader", at - 0.05)
    auto.overlay("A", "fader", (at - 0.05, v), (L, v))
    auto.overlay("A", "dry", (at - 0.03, auto.value("A", "dry", at - 0.03)), (at, 0.0), (L, 0.0))
    for lane in ("echo", "reverb"):
        if ("A", lane) in auto.lanes:
            auto.overlay("A", lane, (at, auto.value("A", lane, at)), (at + 0.02, 0.0), (L, 0.0))
    for lane, full in (("fader", 1.0), ("low", 1.0), ("mid", 1.0), ("high", 1.0), ("filter", 0.0)):
        if ("B", lane) in auto.lanes and abs(auto.value("B", lane, L) - full) < 1e-6:
            auto.overlay("B", lane, (at - 0.05, auto.value("B", lane, at - 0.05)), (at + 0.05, full), (L, full))


def _riser(auto: Automation, plan):
    """A rising resonant high-pass with the treble cranked and a little reverb, up to A's exit."""
    n = 16 if auto.exit >= 16 else 8
    _need(auto, n)
    x, t0 = auto.exit, auto.exit - n
    high_after = auto.value("A", "high", x + 1)
    auto.overlay("A", "filter", (t0, auto.value("A", "filter", t0)), (t0 + n * 0.5, 0.3), (x - 0.5, 0.7), (x, 0.78))
    auto.overlay("A", "resonance", (t0, 1.0), (x, 4.0))
    auto.overlay("A", "high", (t0, auto.value("A", "high", t0)), (x, 1.7))
    auto.overlay("A", "reverb", (t0, 0.0), (x, 0.4))
    auto.marks.append((t0, "riser"))
    if plan.riser_end == "cut":
        _a_leaves(auto, x)
    else:  # release: the filter drops straight back and A plays on
        auto.overlay("A", "filter", (x, 0.78), (x + 1, 0.0))
        auto.overlay("A", "resonance", (x, 4.0), (x + 1, 1.0))
        auto.overlay("A", "high", (x, 1.7), (x + 1, high_after))
        auto.overlay("A", "reverb", (x, 0.4), (x + 0.5, 0.0))


def _loop_roll(auto: Automation, plan):
    """A's last bar before its exit plays, then repeats in halves: ½, ¼, ⅛, ⅛ of a bar. Its bass
    goes once the loop is shorter than a bar, and A gives way on the exit."""
    _need(auto, 8)
    x = auto.exit
    src = auto.source_beat("A", x - 8)
    auto.loop("A", x - 4, 2, x - 2, resume=False, source=src)
    auto.loop("A", x - 2, 1, x - 1, resume=False, source=src)
    auto.loop("A", x - 1, 0.5, x, resume=False, source=src)
    auto.overlay("A", "low", (x - 4.05, auto.value("A", "low", x - 4.05)), (x - 3.95, 0.0), (x, 0.0))
    auto.marks.append((x - 4, "roll"))
    _a_leaves(auto, x)


def _echo_throw(auto: Automation, plan):
    """A's last beat thrown into a dotted (¾-beat) echo as it leaves."""
    _need(auto, 1)
    x = auto.exit
    auto.echo_beats = 0.75
    auto.overlay("A", "echo", (x - 1, 0.0), (x - 0.02, 1.0), (x, 0.0))
    auto.overlay("A", "feedback", (x - 1, 0.6), (x + 8, 0.6), (x + 16, 0.3))
    _tail(auto, 16)
    _a_leaves(auto, x)
    auto.marks.append((x - 1, "echo"))


def _reverb_throw(auto: Automation, plan):
    """A's last two beats thrown into a big reverb as it leaves."""
    _need(auto, 2)
    x = auto.exit
    auto.reverb_size = max(auto.reverb_size, 0.88)
    auto.overlay("A", "reverb", (x - 2, auto.value("A", "reverb", x - 2)), (x - 0.02, 1.0), (x, 0.0))
    _tail(auto, 16)
    _a_leaves(auto, x)
    auto.marks.append((x - 2, "reverb"))


@dataclass(frozen=True)
class Effect:
    key: str
    name: str
    blurb: str
    kev: str        # the yes/no question Kev answers
    exclude: tuple  # techniques it doesn't go with
    apply: object   # (automation, plan) -> None, in place
    group: str = ""  # effects in one group don't stack


EFFECTS = {e.key: e for e in (  # in the order they are applied
    Effect("loop_roll", "Loop Roll", "A repeats its last bar in halves (½, ¼, ⅛) as it leaves.",
           "Should the outgoing track roll a shrinking loop of its last bar (one bar, half, quarter, eighth) as it leaves?",
           (), _loop_roll),
    Effect("riser", "Riser", "A rising, resonant high-pass with the treble cranked, up to the change.",
           "Should the outgoing track rise through a resonant high-pass filter, treble cranked, to build tension before the change?",
           ("filter_sweep",), _riser),
    Effect("echo_throw", "Echo Throw", "A's last beat thrown into a dotted echo as it leaves.",
           "Should the outgoing track's last beat be thrown into an echo as it leaves?",
           ("echo_out",), _echo_throw, "throw"),
    Effect("reverb_throw", "Reverb Throw", "A's last two beats thrown into a big reverb as it leaves.",
           "Should the outgoing track's last beats be thrown into a big reverb as it leaves?",
           ("reverb_wash",), _reverb_throw, "throw"),
)}


def effect_options(technique: str) -> list:
    """The effects that go with a technique."""
    return [k for k, e in EFFECTS.items() if technique not in e.exclude]


def stack(technique: str, keys) -> list:
    """The effects that can actually go on together, best first: ones that fit the technique, one
    per group, at most MAX_EFFECTS."""
    out, groups = [], set()
    for k in keys:
        e = EFFECTS.get(k)
        if e is None or technique in e.exclude or k in out or (e.group and e.group in groups):
            continue
        out.append(k)
        groups.add(e.group)
        if len(out) == MAX_EFFECTS:
            break
    return out


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

    sung_out = a.vocals > 0.1 and a.vocals_out_bars < 4  # an echo would repeat A's last words; a wash blurs them
    if dev > MAX_STRETCH:
        if lift > 0.3 and b.first_drop_bar:
            return pick("drop_cut", "info", "Too far apart to beatmatch, and B lifts the energy: cut into its drop", sync=False)
        if sung_out:
            return pick("reverb_wash", "info", "Too far apart to beatmatch, and A sings to the end: wash it out in reverb",
                        sync=False)
        return pick("echo_out", "info", "Too far apart to beatmatch: echo A out, start B at its own tempo", sync=False)
    if vocal_clash:
        return pick("filter_sweep", "warn", "Both have vocals at the joint: keep the overlap short and filtered", bars=8)
    if k < 0.55:
        return pick("filter_sweep", "warn", "The keys clash: a short filtered overlap hides it", bars=8)
    if lift > 0.8 and b.first_drop_bar:
        return pick("drop_cut", "info", f"Big energy lift: drop straight into B's first drop")
    if lift < -0.8:
        if sung_out:
            return pick("reverb_wash", "info", "Energy falls and A sings to the end: a reverb wash resets the room")
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
    effects: list = field(default_factory=list)  # effect keys layered on, in order (see EFFECTS)
    riser_end: str = "cut"  # how a riser ends: "cut" or "release"

    @property
    def tech(self) -> Technique:
        return TECHNIQUES[self.technique]

    def automation(self, bpm: float, b: Track | None = None, a: Track | None = None) -> Automation:
        """The technique's automation, then:
        - given deck B's track, a bass swap moves onto the bar where B's bass comes in, when that
          falls in the middle half of the blend;
        - given deck A's track, a blend that runs past A's end loops A's last clean phrase;
        - the effects go on top."""
        auto = self.tech.build(self.params, bpm)
        if b is not None and any(label == "bass swap" for _, label in auto.marks):
            landing = bass_landing(b, self.in_bar, auto.beats)
            if landing is not None:
                auto = self.tech.build({**self.params, "swap_at": landing}, bpm)
        if a is not None and self.tech.blend:
            ext = extension(a, self.out_bar, self.params["bars"])
            if ext is not None:
                bar, length, repeats = ext
                at = (bar - self.out_bar) * 4.0
                auto.loop("A", at, length * 4.0, at + length * 4.0 * (repeats + 1))
                auto.marks.append((at, f"loop ×{repeats}"))
        for key in sorted(stack(self.technique, self.effects), key=list(EFFECTS).index):
            EFFECTS[key].apply(auto, self)
        return auto


def bass_landing(b: Track, in_bar: int, beats: float) -> float | None:
    """Beat of a blend where B's bass first plays, if that is in the blend's middle half."""
    if not b.bass_in_bars:
        return None
    beat = (b.bass_in_bars + 1 - in_bar) * 4.0
    return beat if beats / 4 <= beat <= beats * 3 / 4 else None


MAX_REPEATS = 2  # a looped phrase plays at most three times in all; more starts to show


def extension(t: Track, out_bar: int, bars: int) -> tuple | None:
    """(bar, length, repeats) looping A's last vocal-free 4- or 8-bar phrase inside the blend so A
    lasts as long as a blend of `bars` from `out_bar` needs; None if it fits anyway or can't."""
    short = out_bar + bars - 1 - t.n_bars
    if short <= 0:
        return None
    for length in (8, 4):
        repeats = -(-short // length)
        if repeats > MAX_REPEATS:
            continue
        clean = [p for p in t.phrase_grid[1] if out_bar <= p <= t.n_bars - length + 1
                 and "1" not in t.vocal_bars[p - 1:p - 1 + length]]
        if clean:
            return clean[-1], length, repeats
    return None


def bars_after_out(technique: str, params: dict) -> int:
    """Bars of deck A the transition still needs after its mix-out bar."""
    return params["bars"] if TECHNIQUES[technique].blend else 1


def mix_window(a: Deck, now: int, technique: str, params: dict, lead_beats: float) -> tuple:
    """(first, last) bar of deck A where the mix can land: far enough ahead to render, early enough to fit."""
    db = a.downbeats
    ready = now / SR + 2.0 + lead_beats * a.spb / SR
    last = len(db) - bars_after_out(technique, params) + 1
    if TECHNIQUES[technique].blend:  # later still if A can loop a phrase to make room
        while last < len(db) and extension(a.track, last + 1, params["bars"]) is not None:
            last += 1
    return int(np.searchsorted(db, ready)) + 1, last


def mix_out_points(a: Deck, now: int, technique: str, params: dict, lead_beats: float) -> list:
    """[(label, bar)] where deck A can hand over, all far enough ahead to render and still fit."""
    t, n = a.track, len(a.downbeats)
    first, last = mix_window(a, now, technique, params, lead_beats)
    if first > last:
        return []
    picks = [("Next bar", first)]
    phrase = t.phrase_after(first, last)
    if phrase is not None:
        picks += [("Next phrase", phrase), ("Phrase after", t.phrase_after(phrase + 1, last))]
    if t.outro_bars:
        picks.append(("Outro starts", t.phrase_near(n - t.outro_bars + 1, first, last)))
    if t.vocals > 0.05 and t.vocals_out_bars:
        picks.append(("Vocals end", t.phrase_after(n - t.vocals_out_bars + 1, last)))
    picks.append(("Latest", t.phrase_before(last, first) or last))
    seen = {}
    for label, bar in picks:
        if bar is not None and first <= bar <= last:
            seen.setdefault(bar, label)
    if TECHNIQUES[technique].blend:
        seen = {bar: label + (" · A loops to fit" if extension(t, bar, params["bars"]) else "")
                for bar, label in seen.items()}
    return sorted(((label, bar) for bar, label in seen.items()), key=lambda lb: lb[1])


def default_out(points: list, now_bar: int) -> int:
    """The outro if it is close, else the next phrase."""
    bars = {label.split(" · ")[0]: bar for label, bar in points}
    if "Outro starts" in bars and bars["Outro starts"] - now_bar <= 32:
        return bars["Outro starts"]
    return bars.get("Next phrase", points[0][1])


def mix_in_points(b: Track, auto: Automation) -> list:
    """[(label, bar)] for where deck B can enter."""
    picks = [("Track start", 1)]
    swap = next((beat for beat, label in auto.marks if label == "bass swap"), None)
    if swap is not None and b.bass_in_bars:  # a phrase start that puts B's bass mid-blend: the swap moves onto it
        fits = [p for p in b.phrases if 1 < p and bass_landing(b, p, auto.beats) is not None]
        if fits:
            picks.append(("Bass lands on the swap", min(fits, key=lambda p: abs(bass_landing(b, p, auto.beats) - swap))))
    picks += [("Second phrase", b.phrases[1] if len(b.phrases) > 1 else None),
              ("Third phrase", b.phrases[2] if len(b.phrases) > 2 else None)]
    if b.first_drop_bar > 1:
        picks.append(("First drop", b.phrase_near(b.first_drop_bar, 2, b.n_bars) or b.first_drop_bar))
    last = b.n_bars - int(math.ceil(auto.beats / 4))
    seen = {}
    for label, bar in picks:
        if bar is not None and 1 <= bar <= last:
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
    report: dict = field(default_factory=dict)  # where it lands and how well the kicks locked (see `report`)

    @property
    def end(self) -> int:
        return self.start + len(self.buffer)

    def a_at(self, tpos: int) -> int:
        """Deck A's sample heard at `tpos` into the buffer (loops jump it back)."""
        return self.start + int(round(self.auto.source_beat("A", tpos / self.spb) * self.spb))

    def b_at(self, tpos: int) -> int:
        return self.b_offset + int(round(self.auto.source_beat("B", tpos / self.spb) * self.spb))

    @property
    def b_after(self) -> int:
        """Deck B's sample right after the buffer, where it plays on by itself."""
        return self.b_at(len(self.buffer))


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


def _play(audio: np.ndarray, start: int, n: int, spb: float, jumps: list) -> np.ndarray:
    """n samples of a deck from `start`, following its timeline jumps. Each jump is crossfaded over
    the SPLICE samples just before it, so the downbeat it lands on keeps its full attack."""
    if not jumps:
        return _segment(audio, start, n)
    pts = [(0.0, 0.0)] + [(b, s) for b, s in jumps if b > 0]
    out = np.zeros((n, 2), np.float32)
    for i, (beat, src) in enumerate(pts):
        at = int(round(beat * spb))
        to = int(round(pts[i + 1][0] * spb)) if i + 1 < len(pts) else n
        lead = SPLICE if i else 0  # this piece fades in over the samples before its jump
        lo = max(at - lead, 0)
        piece = _segment(audio, start + int(round(src * spb)) - (at - lo), min(to, n) - lo)
        if lead:
            ramp = np.linspace(0, 1, at - lo, dtype=np.float32)[:, None]
            piece[:at - lo] *= ramp
            out[lo:at] *= 1 - ramp  # the piece before fades out under it
        out[lo:lo + len(piece)] += piece
    return out


def align(a: Deck, start: int, b: Deck, b_offset: int, max_ms: int = 60) -> tuple:
    """Nudge deck B so its kicks land on deck A's (allin1's downbeats are only good to ~10-40 ms).

    Once both decks run at one measured tempo the offset is constant, so it is measured in 16-beat
    windows across up to 128 beats of both tracks (past the blend if need be) and the median of
    the windows that clearly lock is used; intros without a kick then can't mislead it.
    Returns (offset, {"lag_ms", "locked": windows that locked, "windows"})."""
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
    info = {"locked": len(locked), "windows": len(lags), "lag_ms": None}
    if not locked:  # no steady kick to lock onto: trust allin1's grid rather than guess
        return b_offset, info
    info["lag_ms"] = int(np.median(locked))
    return b_offset + info["lag_ms"] * 44, info


def build(plan: Plan, a: Deck, b: Deck) -> Scheduled:
    auto = plan.automation(a.bpm, b.track, a.track)
    start, b_offset, n, spb = layout(plan, a, b, auto)
    lock = None
    if plan.tech.blend and plan.sync:
        b_offset, lock = align(a, start, b, b_offset)
    seg_a = _play(a.audio, start, n, spb, auto.timeline.get("A", []))
    seg_b = _play(b.audio, b_offset, n, spb, auto.timeline.get("B", []))
    seg_b[:int(round(auto.b_start * spb))] = 0
    s = Scheduled(start, render(seg_a, seg_b, auto, spb), b, b_offset, plan, auto, spb)
    s.report = report(s, a, lock)
    return s


def report(s: Scheduled, a: Deck, lock: dict | None) -> dict:
    """What a transition does, for the log: its bars, the sections there, whether each is a phrase
    start, where the bass swaps, and how the kicks locked."""
    p, b = s.plan, s.deck
    section = lambda deck, bar: deck.where(deck.bar_sample(bar) + int(deck.spb))[2] or "-"  # a beat in: section edges wobble
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "a": a.track.display, "b": b.track.display,
            "technique": p.technique, "params": p.params, "sync": p.sync,
            "out_bar": p.out_bar, "out_section": section(a, p.out_bar), "out_on_phrase": p.out_bar in a.track.phrases,
            "in_bar": p.in_bar, "in_section": section(b, p.in_bar), "in_on_phrase": p.in_bar in b.track.phrases,
            "swap_beat": next((beat for beat, label in s.auto.marks if label == "bass swap"), None),
            "loops": [(deck, at, until, length) for deck, at, until, length in s.auto.loops],
            "effects": list(p.effects), "lock": lock}


def describe(r: dict) -> str:
    """One line from a report, for the booth."""
    phrase = lambda on: "phrase" if on else "OFF-PHRASE"
    text = (f"A bar {r['out_bar']} ({r['out_section']}, {phrase(r['out_on_phrase'])}) → "
            f"B bar {r['in_bar']} ({r['in_section']}, {phrase(r['in_on_phrase'])})")
    if r["swap_beat"] is not None:
        text += f" · swap at bar {r['swap_beat'] / 4 + 1:g} of the mix"
    runs = []  # loops back to back on one deck (a roll) read as one
    for deck, at, until, length in r.get("loops", []):
        if runs and runs[-1][0] == deck and abs(runs[-1][2] - at) < 1e-6:
            runs[-1][2], runs[-1][3] = until, runs[-1][3] + [length]
        else:
            runs.append([deck, at, until, [length]])
    for deck, at, until, lengths in runs:
        size = " → ".join(f"{n / 4:g} bar{'s' * (n != 4)}" if n >= 4 else f"{n:g} beat{'s' * (n != 1)}" for n in lengths)
        text += f" · {deck} loops {size} from bar {at / 4 + 1:g} to {until / 4 + 1:g}"
    if r.get("effects"):
        text += " · effects: " + ", ".join(r["effects"])
    lock = r["lock"]
    if lock is not None:
        text += (f" · kicks locked {lock['locked']}/{lock['windows']}, nudged {lock['lag_ms']:+d} ms"
                 if lock["lag_ms"] is not None else " · no kick lock: on allin1's grid")
    return text


def log(r: dict):
    """Append a transition's report to results/live/transitions.jsonl."""
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps(r, default=str) + "\n")
    except OSError:
        pass
