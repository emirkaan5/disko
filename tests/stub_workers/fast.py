"""Stands in for live/workers/fast.py in tests: the same protocol (--jobs FILE or -, JSON lines out),
made-up results, no Essentia. Records each path it read and whether that file was there."""
import argparse
import json
import os
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--jobs", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--workers", type=int, default=2)
args = ap.parse_args()
say = lambda **m: print(json.dumps(m), flush=True)
jobs = (json.loads(l) for l in sys.stdin if l.strip()) if args.jobs == "-" else json.loads(Path(args.jobs).read_text())
say(event="started", total=None, workers=args.workers)
for job in jobs:
    there = os.path.exists(job["path"])
    with open(Path(args.out) / "seen.jsonl", "a") as f:
        f.write(json.dumps({"path": job["path"], "there": there, "id": job["id"]}) + "\n")
    if "fail" in job["rel"] or not there:
        say(event="failed", id=job["id"], rel=job["rel"], error="stub: can't read it")
        continue
    n = len(job["id"]) % 5
    data = {"version": 1, "path": job["rel"], "bytes": job["bytes"], "tags": {}, "duration": 180.0,
            "bpm": 123.0 + n, "beats": [i * 60 / (123.0 + n) for i in range(300)], "key": "A", "scale": "minor",
            "key_strength": 0.8, "loudness_short_term_mean": -9.0, "onset_rate": 3.0,
            "spectral_centroid_mean": 2000.0, "danceability": 1.5, "voice": 0.2, "valence": 5.0, "arousal": 6.0,
            "styles": {"Electronic---House": 0.9}}
    (Path(args.out) / f"{job['id']}.fast.json").write_text(json.dumps(data))
    say(event="done", id=job["id"], rel=job["rel"], title="", artist="", bpm=data["bpm"], key="A minor",
        arousal=6.0, seconds=0.0)
say(event="finished")
