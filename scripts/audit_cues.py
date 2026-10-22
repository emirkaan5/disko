"""How many of the cue points Kev is offered land on a phrase start, old grid vs new.

    .venv-live/bin/python scripts/audit_cues.py

A phrase start here is a bar where one of allin1's sections begins (snapped to the nearest
downbeat), or a multiple of 8 bars into that section. The old grid counted phrases as bars 1, 9,
17... from the top of the track; the new one (Track.phrase_grid) starts them on the sections.
Every track plays deck A with a 16-bar bass swap planned from 30% in, and deck B for the mix-in
points.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.dj import Cards, KevDJ  # noqa: E402
from live.library import Library  # noqa: E402
from live.mixes import TECHNIQUES  # noqa: E402

BARS = 16


def section_starts(t) -> set:
    return {int(np.argmin(np.abs(t.downbeats - s.start))) + 1 for s in t.sections}


def old_outs(t, first, last) -> list:
    floor = max(first, int(t.n_bars * 0.55))
    floor = first if floor > last else floor
    bars = {bar for bar in range(1, last + 1, 8) if bar >= floor}
    if t.outro_bars and floor <= t.n_bars - t.outro_bars + 1 <= last:
        bars.add(t.n_bars - t.outro_bars + 1)
    if t.vocals > 0.05 and t.vocals_out_bars and floor <= t.n_bars - t.vocals_out_bars + 1 <= last:
        bars.add(t.n_bars - t.vocals_out_bars + 1)
    bars.add(last)
    return sorted(bars)


def old_ins(t, swap_beat) -> list:
    picks = [1, 9, 17]
    if t.bass_in_bars and t.bass_in_bars + 1 - int(round(swap_beat / 4)) > 1:
        picks.append(t.bass_in_bars + 1 - int(round(swap_beat / 4)))
    if t.first_drop_bar > 1:
        picks.append(t.first_drop_bar)
    return [b for b in picks if b <= t.n_bars - BARS]


def main():
    lib = Library()
    dj = KevDJ.__new__(KevDJ)
    dj.cards = Cards(lib)
    tech = TECHNIQUES["bass_swap"]
    auto = tech.build({**tech.defaults(), "bars": BARS}, 125.0)
    swap = next(beat for beat, label in auto.marks if label == "bass swap")
    from live.mixes import mix_in_points
    counts = {k: [0, 0] for k in ("old out", "new out", "old in", "new in")}

    def tally(key, bars, phrases):
        counts[key][0] += sum(b in phrases for b in bars)
        counts[key][1] += len(bars)

    for t in lib.tracks:
        if not t.sections or t.n_bars < 48:
            continue
        starts = sorted(section_starts(t) | {1})  # a phrase: a section start, or 8k bars into that section
        strict = {b for s, e in zip(starts, starts[1:] + [t.n_bars + 1]) for b in range(s, e, 8)}
        first, last = int(t.n_bars * 0.3), t.n_bars - BARS + 1
        tally("old out", old_outs(t, first, last), strict)
        tally("new out", [bar for _, bar, _ in dj._out_options(t, first, last)], strict)
        tally("old in", old_ins(t, swap), strict)
        tally("new in", [bar for _, bar in mix_in_points(t, auto)], strict)

    print("cue points on a phrase start (a section start, or 8k bars into that section):")
    for key, (on, total) in counts.items():
        print(f"  {key:8s} {on:5d}/{total:<5d} {on / max(total, 1):6.1%}")


if __name__ == "__main__":
    main()
