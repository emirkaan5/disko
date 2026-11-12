"""The deep pass, one track at a time: allin1's beats, bars and sections, plus per-bar stem levels.

The app runs this as a long-lived helper and asks for a track when it is about to play (the opener
you pick, then each track Kev picks). allin1 splits the track into drums, bass, vocals and other
with Demucs and finds its beats, downbeats and labelled sections; before the stems are deleted,
each one's level in every bar is measured, so where the bass and vocals play can be worked out
later. Writes <out>/<id>.allin1.json and <out>/<id>.stems.json.

Reads one JSON request per line on stdin ({"id", "path", "out"}, and optionally "tmp": the folder to
keep the stems in while they're measured, such as the app's session temp folder), answers each with a JSON line on
stdout ({"event": "done" | "failed", "id", ...}), and exits when stdin closes, so it never outlives
the app. Runs in the audio venv:

    .venv-audio/bin/python live/workers/deep.py

Two backends give the same files. On an Apple Silicon Mac it is all-in-one-mlx on Apple's MLX; anywhere
else it is upstream allin1 on PyTorch (CUDA when there is a GPU, else the CPU, much slower), with
live/workers/natten_torch standing in for the NATTEN build allin1 would otherwise need. Its first
line says which it got ({"event": "ready", "device": "mlx" | "cuda" | "cpu"}), so the app knows how
long a track may take.
"""
import json
import os
import platform
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
NATTEN_TORCH = Path(__file__).resolve().parent / "natten_torch"


def backend() -> str:
    # by platform only: requirements/audio.txt installs each stack on its own platform
    return "mlx" if sys.platform == "darwin" and platform.machine() == "arm64" else "torch"


def load(name: str):
    """(allin1 module, extra analyze() arguments, what to run after each track, device) for the backend."""
    if name == "mlx":
        import allin1_mlx
        import mlx.core as mx
        mx.set_cache_limit(MLX_CACHE_GB << 30)
        return allin1_mlx, {}, mx.clear_cache, "mlx"
    sys.path.insert(0, str(NATTEN_TORCH))  # before any installed NATTEN, whose API allin1 can't use
    import allin1
    import torch
    device = "cuda" if torch.cuda.is_available() else "cpu"
    after = torch.cuda.empty_cache if device == "cuda" else (lambda: None)
    return allin1, {"device": device, "multiprocess": False}, after, device


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


def analyze(allin1, path: Path, out: Path, track_id: str, tmp_root: str | None = None, **options):
    with tempfile.TemporaryDirectory(prefix="disko-deep-", dir=tmp_root) as tmp:
        tmp = Path(tmp)
        # No out_dir: with keep_byproducts, all-in-one-mlx 1.0.14 saves its JSON into the stems
        # folder instead, so we write the results ourselves.
        r = allin1.analyze([path], demix_dir=tmp / "demix", spec_dir=tmp / "spec", keep_byproducts=True, **options)
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

    allin1, options, after_each, device = load(backend())
    say(event="ready", device=device)
    for line in sys.stdin:
        if not line.strip():
            continue
        req = json.loads(line)
        start = time.time()
        say(event="started", id=req["id"])
        try:
            bars = analyze(allin1, Path(req["path"]), Path(req["out"]), req["id"], req.get("tmp"), **options)
            say(event="done", id=req["id"], bars=bars, seconds=round(time.time() - start, 1))
        except Exception as e:
            traceback.print_exc()
            say(event="failed", id=req["id"], error=f"{type(e).__name__}: {e}")
        finally:
            after_each()


if __name__ == "__main__":
    main()
