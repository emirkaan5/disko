"""Turn Raveform's real DJ mixes into labelled transitions for the timing test.

A transition is two consecutive tracks in a mix, A (outgoing) then B (incoming), where both
tracks have human-annotated sections. The labels are what the DJ actually did, read off the
beat-level mix-to-track alignments:

  incoming_start     section of B where the DJ started it
  outgoing_at_start  section of A that was playing when B came in
  outgoing_end       section of A where the DJ took it out
  incoming_at_end    section of B that was playing when A was gone
  overlap            how long both played together, in bars (bucketed by the test)
"""
import csv
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "data" / "raveform"

# Sections offered as options: the first WINDOW of the incoming track, the last WINDOW of the
# outgoing one. ~98% of real mix points fall inside; Laya's calibration only covers <=10 options.
WINDOW = 7

# Overlaps outside this range are alignment failures or an unaligned track in between.
MIN_OVERLAP_BARS, MAX_OVERLAP_BARS = -2, 96


@dataclass
class Section:
    name: str       # intro, buildup, drop, breakdown, cooldown, outro, altintro, altoutro, bridge
    nth: int        # 1 for the first drop, 2 for the second, ...
    start: float    # seconds
    end: float
    bar_start: int  # 1-based
    bars: int

    @property
    def key(self) -> str:
        return f"{self.name}_{self.nth}"

    @property
    def label(self) -> str:
        name = {"altintro": "alt intro", "altoutro": "alt outro"}.get(self.name, self.name)
        return f"{name} {self.nth}" if self.nth > 1 else name


@dataclass
class Track:
    id: str
    title: str
    genre: str
    bpm: float
    sections: list  # [Section]

    def section_at(self, t: float) -> int:
        """Index of the section playing at time t, clamped to the first/last section."""
        for i, s in enumerate(self.sections):
            if t < s.end:
                return i
        return len(self.sections) - 1


@dataclass
class Transition:
    mix_id: str
    a: Track        # outgoing
    b: Track        # incoming
    overlap_bars: float
    labels: dict    # question id -> section index into a/b.sections, or overlap in bars


def _jsonl(path: Path):
    with open(path) as f:
        return [json.loads(line) for line in f]


def _sections(seg: dict) -> list:
    """Sections with bar counts, the trailing 'end' marker folded into the section before it."""
    raw = [dict(s) for s in seg["sections"]]
    merged = []
    for s in raw:
        if s["name"] == "end" and merged:
            merged[-1]["end"] = max(merged[-1]["end"], s["end"])
        elif s["name"] != "end":
            merged.append(s)
    with open(ROOT / "structures" / "beats" / f"{seg['key']}.beat.csv") as f:
        downbeats = [float(r["time"]) for r in csv.DictReader(f) if r["downbeat"] == "1"]

    out, seen, bar = [], {}, 1
    for s in merged:
        seen[s["name"]] = seen.get(s["name"], 0) + 1
        bars = max(1, sum(1 for t in downbeats if s["start"] <= t < s["end"]))
        out.append(Section(s["name"], seen[s["name"]], s["start"], s["end"], bar, bars))
        bar += bars
    return out


@lru_cache(maxsize=None)
def _track(track_id: str) -> Track:
    seg = _annotated()[track_id]
    return Track(track_id, seg["title"], seg["genre"], seg["average_bpm"], _sections(seg))


@lru_cache(maxsize=None)
def _annotated() -> dict:
    return {s["id"]: s for s in json.loads((ROOT / "structures" / "segments.json").read_text())}


def _track_time_at(align: dict, mix_beat: int) -> float:
    """Where in the track the mix was at `mix_beat`, via the DTW warping path ([track_beat, mix_beat]).

    The path's beat indices don't match the released beat files for ~1 in 6 tracks, so the beat
    is placed in time by interpolating between the alignment's own mix-in and mix-out anchors.
    """
    track_beat, _ = min(align["wp"], key=lambda p: abs(p[1] - mix_beat))
    b0, b1 = align["mixin_beat_track"], align["mixout_beat_track"]
    t0, t1 = align["mixin_time_track"], align["mixout_time_track"]
    return t0 + (track_beat - b0) * (t1 - t0) / max(b1 - b0, 1)


def load_transitions():
    """All usable transitions, plus a count of why the rest were dropped."""
    annotated = _annotated()
    transitions, dropped = [], {"one_side_unannotated": 0, "implausible_overlap": 0, "outside_window": 0}
    for mix in _jsonl(ROOT / "mixes.jsonl"):
        path = ROOT / "alignments" / f"{mix['id']}.align.jsonl"
        if not path.exists():
            continue
        aligns = {al["track_id"]: al for al in _jsonl(path)}
        ids = [t.get("id") for t in mix["tracklist"]]
        for a_id, b_id in zip(ids, ids[1:]):
            if a_id not in aligns or b_id not in aligns:
                continue
            if a_id not in annotated or b_id not in annotated:
                dropped["one_side_unannotated"] += 1
                continue
            al_a, al_b = aligns[a_id], aligns[b_id]
            overlap = (al_a["mixout_beat_mix"] - al_b["mixin_beat_mix"]) / 4
            if not MIN_OVERLAP_BARS <= overlap <= MAX_OVERLAP_BARS:
                dropped["implausible_overlap"] += 1
                continue
            a, b = _track(a_id), _track(b_id)
            labels = {
                "incoming_start": b.section_at(al_b["mixin_time_track"]),
                "outgoing_at_start": a.section_at(_track_time_at(al_a, al_b["mixin_beat_mix"])),
                "outgoing_end": a.section_at(al_a["mixout_time_track"]),
                "incoming_at_end": b.section_at(_track_time_at(al_b, al_a["mixout_beat_mix"])),
                "overlap": overlap,
            }
            in_window = (
                labels["incoming_start"] < WINDOW and labels["incoming_at_end"] < WINDOW
                and labels["outgoing_at_start"] >= len(a.sections) - WINDOW
                and labels["outgoing_end"] >= len(a.sections) - WINDOW
            )
            if not in_window:
                dropped["outside_window"] += 1
                continue
            transitions.append(Transition(mix["id"], a, b, overlap, labels))
    return transitions, dropped
