"""Render one transition to a WAV, with some of each track around it, for listening.

    .venv-live/bin/python scripts/render_mix.py --a "Phoenix Lights" --b Neurotic --technique bass_swap
    .venv-live/bin/python scripts/render_mix.py --a fma_107426 --technique echo_out --effects loop_roll,riser
    .venv-live/bin/python scripts/render_mix.py --a ... --b ... --params '{"bars": 32}' --out-bar 97 --in-bar 17

Tracks come from the house test set, by uid or part of the title; B defaults to the top-ranked
next track. Mix-out defaults to the first phrase start past 60% of A, mix-in to the planner's
default for the technique. Prints the transition's report.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from live.audio import SR, load_deck, sync_ratio  # noqa: E402
from live.library import MAX_STRETCH, Library, tempo_match  # noqa: E402
from live.mixes import TECHNIQUES, Plan, build, default_in, describe, mix_in_points  # noqa: E402


def find(lib, key):
    if key in lib.by_uid:
        return lib.by_uid[key]
    hit = next((t for t in lib.tracks if key.lower() in t.title.lower()), None)
    if hit is None:
        sys.exit(f"no track matches {key!r}")
    return hit


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a", required=True)
    ap.add_argument("--b")
    ap.add_argument("--technique", default="bass_swap", choices=list(TECHNIQUES))
    ap.add_argument("--params", default="{}", help="JSON overriding the technique's defaults")
    ap.add_argument("--effects", default="", help="comma-separated effects to layer on")
    ap.add_argument("--riser-end", default="cut", choices=["cut", "release"], help="how a riser ends (blends only)")
    ap.add_argument("--out-bar", type=int)
    ap.add_argument("--in-bar", type=int)
    ap.add_argument("--context", type=int, default=8, help="bars of each track around the transition")
    ap.add_argument("--out", default="mix.wav")
    args = ap.parse_args()

    lib = Library()
    ta = find(lib, args.a)
    tb = find(lib, args.b) if args.b else lib.rank(ta, ta.bpm, set())[0].track
    a = load_deck(ta)
    tech = TECHNIQUES[args.technique]
    sync = tech.blend or tempo_match(a.bpm, tb.bpm)[1] <= MAX_STRETCH
    b = load_deck(tb, sync_ratio(tb, a.period) if sync else 1.0)
    params = {**tech.defaults(), **json.loads(args.params)}
    effects = [e for e in args.effects.split(",") if e]
    auto = tech.build(params, a.bpm)
    out_bar = args.out_bar or ta.phrase_after(int(ta.n_bars * 0.6), ta.n_bars) or int(ta.n_bars * 0.6)
    in_bar = args.in_bar or default_in(mix_in_points(tb, auto), args.technique)
    plan = Plan(args.technique, params, sync, out_bar, in_bar, effects=effects, riser_end=args.riser_end)
    s = build(plan, a, b)

    pad = int(args.context * 4 * a.spb)
    before = a.audio[max(s.start - pad, 0):s.start]
    after = b.audio[s.b_after:s.b_after + pad]
    mix = np.concatenate([before, s.buffer, after])
    sf.write(args.out, mix, SR)
    print(f"{ta.display}  →  {tb.display}")
    print(f"{tech.name}{' + ' + ', '.join(effects) if effects else ''} · {params}")
    print(describe(s.report))
    print(f"wrote {args.out}: {len(mix) / SR:.0f} s, transition from {len(before) / SR:.1f} s to "
          f"{(len(before) + len(s.buffer)) / SR:.1f} s")


if __name__ == "__main__":
    main()
