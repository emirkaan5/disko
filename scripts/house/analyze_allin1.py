"""Run allin1 (its Apple Silicon port, all-in-one-mlx) on every house track, plus per-bar stem levels.

allin1 splits each track into drums, bass, vocals and other with Demucs, then finds the tempo,
beats, downbeats and labelled sections (intro, verse, chorus, break, inst, solo, bridge, outro).
allin1 writes its own JSON to data/house/analysis/allin1/<source>/<id>.json. Before the ~200 MB
of stems per track are deleted, we measure each stem's level in every bar (downbeat to
downbeat) and save the raw levels to data/house/analysis/stems/<source>_<id>.json, so whether a
stem "is playing" can be decided later without re-running Demucs. Tracks go in batches so
stems never pile up on disk; tracks with a stems JSON already are skipped.

    .venv-audio/bin/python scripts/house/analyze_allin1.py --source fma
"""
import argparse
import json
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
STEMS = ("drums", "bass", "vocals", "other")
# MLX keeps freed GPU buffers cached up to its memory limit (~40 GB on a 52 GB Mac), starving
# everything else; cap the cache and empty it after each batch.
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


def run_batch(allin1_mlx, paths: list, out_dir: Path, tmp_root: Path):
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        tmp = Path(tmp)
        # No out_dir: with keep_byproducts, all-in-one-mlx 1.0.14 saves its JSON into the stems
        # folder instead of out_dir, so we write the results ourselves.
        results = allin1_mlx.analyze(paths, demix_dir=tmp / "demix", spec_dir=tmp / "spec", keep_byproducts=True)
        for r in results if isinstance(results, list) else [results]:
            source = r.path.parent.name
            (out_dir / f"{r.path.stem}.json").write_text(json.dumps({
                "bpm": r.bpm, "beats": r.beats, "downbeats": r.downbeats, "beat_positions": r.beat_positions,
                "segments": [{"start": s.start, "end": s.end, "label": s.label} for s in r.segments]}))
            duration = float(sf.info(str(r.path)).duration)
            stats = {"downbeats": r.downbeats, "duration": round(duration, 3)}
            if len(r.downbeats) >= 2:
                stats.update(bar_levels(tmp / "demix" / "htdemucs" / r.path.stem, r.downbeats, duration))
            (ROOT / "analysis" / "stems" / f"{source}_{r.path.stem}.json").write_text(json.dumps(stats))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", choices=["fma", "jamendo"], required=True)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--limit", type=int, help="analyze at most this many tracks (for a quick check)")
    args = ap.parse_args()

    import allin1_mlx
    import mlx.core as mx
    mx.set_cache_limit(MLX_CACHE_GB << 30)

    out_dir = ROOT / "analysis" / "allin1" / args.source
    out_dir.mkdir(parents=True, exist_ok=True)
    (ROOT / "analysis" / "stems").mkdir(parents=True, exist_ok=True)
    tmp_root = ROOT / "tmp"
    tmp_root.mkdir(exist_ok=True)

    paths = sorted((ROOT / "audio" / args.source).glob("*.mp3"))
    todo = [p for p in paths if not (ROOT / "analysis" / "stems" / f"{args.source}_{p.stem}.json").exists()]
    todo = todo[:args.limit]
    print(f"{args.source}: {len(paths)} tracks, {len(todo)} to analyze", flush=True)
    start = time.time()
    for i in range(0, len(todo), args.batch):
        batch = todo[i:i + args.batch]
        try:
            run_batch(allin1_mlx, batch, out_dir, tmp_root)
        except Exception as e:
            print(f"  FAILED batch starting {batch[0].name}: {e}", flush=True)
            continue
        finally:
            mx.clear_cache()
        done = i + len(batch)
        rate = (time.time() - start) / done
        print(f"  {done}/{len(todo)}  {rate:.1f} s/track  ~{rate * (len(todo) - done) / 60:.0f} min left", flush=True)
    print(f"done: {args.source}", flush=True)


if __name__ == "__main__":
    main()
