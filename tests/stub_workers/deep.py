"""Stands in for live/workers/deep.py in tests: same protocol, a made-up four-bar-per-section map."""
import json
import os
import sys
from pathlib import Path

say = lambda **m: print(json.dumps(m), flush=True)
say(event="ready")
for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    say(event="started", id=req["id"])
    there = os.path.exists(req["path"])
    out = Path(req["out"])
    with open(out / "seen.jsonl", "a") as f:
        f.write(json.dumps({"path": req["path"], "there": there, "tmp": req.get("tmp")}) + "\n")
    if not there:
        say(event="failed", id=req["id"], error="stub: no file")
        continue
    period = 60 / 124
    beats = [i * period for i in range(256)]
    downbeats = beats[::4]
    n = len(downbeats)
    (out / f"{req['id']}.stems.json").write_text(json.dumps({
        "downbeats": downbeats, "duration": 124.0, "sample_rate": 44100,
        "db": {"mix": [-10.0] * n, "bass": [-12.0] * n, "vocals": [-40.0] * n, "drums": [-11.0] * n,
               "other": [-15.0] * n},
        "energy_share": {"drums": 0.4, "bass": 0.3, "vocals": 0.05, "other": 0.25}}))
    (out / f"{req['id']}.allin1.json").write_text(json.dumps({
        "bpm": 124, "beats": beats, "downbeats": downbeats, "beat_positions": [i % 4 + 1 for i in range(256)],
        "segments": [{"start": 0.0, "end": downbeats[16], "label": "intro"},
                     {"start": downbeats[16], "end": downbeats[48], "label": "chorus"},
                     {"start": downbeats[48], "end": beats[-1], "label": "outro"}]}))
    say(event="done", id=req["id"], bars=n, seconds=0.0)
