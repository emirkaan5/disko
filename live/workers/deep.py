"""The deep pass, one track at a time: allin1's beats, bars and sections, plus per-bar stem levels.

The app runs this as a long-lived helper and asks for a track when it is about to play (the opener
you pick, then each track Kev picks). allin1 splits the track into drums, bass, vocals and other
with Demucs and finds its beats, downbeats and labelled sections; before the stems are deleted,
each one's level in every bar is measured, so where the bass and vocals play can be worked out
later. Writes <out>/<id>.allin1.json and <out>/<id>.stems.json.

Reads one JSON request per line on stdin ({"id", "path", "out"}), answers each with a JSON line on
stdout ({"event": "done" | "failed", "id", ...}), and exits when stdin closes, so it never outlives
the app. Runs in the audio venv (allin1 on Apple's MLX):

    .venv-audio/bin/python live/workers/deep.py
"""
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf

STEMS = ("drums", "bass", "vocals", "other")
# MLX keeps freed GPU buffers cached up to its memory limit, starving everything else (Kev runs on
# the same GPU): cap the cache and empty it after each track.
MLX_CACHE_GB = 4


def bar_levels(stem_dir: Path, downbeats: list, duration: float) -> dict:
    """Mean power per bar, in dBFS, for each stem and for their sum (the mix)."""
    signals = {}
    for stem in STEMS:
        audio, sr = sf.read(stem_dir / f"{stem}.wav", dtype="float32")
        signals[stem] = audio.mean(axis=1) if audio.ndim == 2 else audio
    signals["mix"] = sum(signals[s] for s in STEMS)

    edges = np.round(np.array(list(downbeats) + [duration]) * sr).astype(int)
    edges = edges.clip(0, len(signals["mix"]))
    widths = np.maximum(np.diff(edges), 1)
    out = {"sample_rate": sr, "db": {}, "energy_share": {}}
    total = sum(float(np.sum(signals[s].astype(np.float64) ** 2)) for s in STEMS)
    for name, x in signals.items():
        power = x.astype(np.float64) ** 2
        cum = np.concatenate([[0.0], np.cumsum(power)])
        per_bar = (cum[edges[1:]] - cum[edges[:-1]]) / widths
        out["db"][name] = [round(float(v), 1) for v in 10 * np.log10(per_bar + 1e-12)]
        if name != "mix":
            out["energy_share"][name] = round(float(power.sum()) / total, 4) if total else None
    return out


def write(dest: Path, data: dict):
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, dest)


def analyze(allin1_mlx, path: Path, out: Path, track_id: str):
    with tempfile.TemporaryDirectory(prefix="disko-deep-") as tmp:
        tmp = Path(tmp)
        # No out_dir: with keep_byproducts, all-in-one-mlx 1.0.14 saves its JSON into the stems
        # folder instead, so we write the results ourselves.
        r = allin1_mlx.analyze([path], demix_dir=tmp / "demix", spec_dir=tmp / "spec", keep_byproducts=True)
        r = r[0] if isinstance(r, list) else r
        stem_dir = tmp / "demix" / "htdemucs" / Path(r.path).stem  # allin1 resolves links, so not path.stem
        duration = float(sf.info(str(stem_dir / "drums.wav")).duration)  # the stems are WAVs whatever the input
        stems = {"downbeats": r.downbeats, "duration": round(duration, 3)}
        if len(r.downbeats) >= 2:
            stems.update(bar_levels(stem_dir, r.downbeats, duration))
    # stems first: the app treats the allin1 file as the sign that both are complete
    write(out / f"{track_id}.stems.json", stems)
    write(out / f"{track_id}.allin1.json", {
        "bpm": r.bpm, "beats": r.beats, "downbeats": r.downbeats, "beat_positions": r.beat_positions,
        "segments": [{"start": s.start, "end": s.end, "label": s.label} for s in r.segments]})
    return len(r.downbeats)


def main():
    # Our stdout is the protocol; anything a library prints (allin1 does) goes to stderr instead.
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)

    def say(**msg):
        try:
            proto.write(json.dumps(msg) + "\n")
        except BrokenPipeError:  # the app is gone
            os._exit(0)

    import allin1_mlx
    import mlx.core as mx
    mx.set_cache_limit(MLX_CACHE_GB << 30)
    say(event="ready")
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        start = time.time()
        say(event="started", id=req["id"])
        try:
            bars = analyze(allin1_mlx, Path(req["path"]), Path(req["out"]), req["id"])
            say(event="done", id=req["id"], bars=bars, seconds=round(time.time() - start, 1))
        except Exception as e:
            traceback.print_exc()
            say(event="failed", id=req["id"], error=f"{type(e).__name__}: {e}")
        finally:
            mx.clear_cache()


if __name__ == "__main__":
    main()
