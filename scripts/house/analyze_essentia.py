"""Run Essentia's MusicExtractor and a Chromaprint fingerprint on every downloaded house track.

Writes one JSON per track to data/house/analysis/essentia/<source>_<id>.json: every scalar
descriptor MusicExtractor computes (rhythm, tonal, loudness, spectral stats), a few short
per-band vectors unrolled, the beat positions, and the fingerprint of the first 120 s (the
length AcoustID looks up). Tracks with a JSON already are skipped.

    .venv-audio/bin/python scripts/house/analyze_essentia.py --workers 12
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
OUT = ROOT / "analysis" / "essentia"

# Short vectors worth unrolling into one column per element; bigger ones (bark/mel/ERB bands, HPCP,
# covariance matrices, histograms) stay out of the CSV.
UNROLL = {
    "lowlevel.mfcc.mean",
    "lowlevel.spectral_contrast_coeffs.mean",
    "lowlevel.spectral_contrast_valleys.mean",
    "rhythm.beats_loudness_band_ratio.mean",
}
SKIP_PREFIXES = ("metadata.version.", "metadata.tags.", "metadata.audio_properties.analysis.")


def analyze(path: Path) -> dict:
    import essentia
    essentia.log.infoActive = essentia.log.warningActive = False
    import essentia.standard as es

    feats, _ = es.MusicExtractor(lowlevelStats=["mean", "stdev"], rhythmStats=["mean", "stdev"],
                                 tonalStats=["mean", "stdev"])(str(path))
    out = {}
    for name in sorted(feats.descriptorNames()):
        if name.startswith(SKIP_PREFIXES):
            continue
        value = feats[name]
        if isinstance(value, str):
            out[name] = value
        elif np.ndim(value) == 0:
            out[name] = float(value)
        elif name in UNROLL:
            out.update({f"{name}_{i + 1}": float(v) for i, v in enumerate(value)})
    out["rhythm.beats_position"] = [round(float(t), 3) for t in feats["rhythm.beats_position"]]

    audio = es.MonoLoader(filename=str(path), sampleRate=44100)()
    out["chromaprint_120s"] = es.Chromaprinter(maxLength=120, sampleRate=44100)(audio)
    return out


def run(path: Path) -> str:
    dest = OUT / f"{path.parent.name}_{path.stem}.json"
    if not dest.exists():
        dest.write_text(json.dumps(analyze(path)))
    return dest.name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    paths = sorted((ROOT / "audio").glob("*/*.mp3"))
    print(f"{len(paths)} tracks")
    done = 0
    with ProcessPoolExecutor(args.workers) as pool:
        futs = {pool.submit(run, p): p for p in paths}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                print(f"  FAILED {futs[f]}: {e}", flush=True)
                continue
            done += 1
            if done % 50 == 0:
                print(f"  {done}/{len(paths)}", flush=True)
    print(f"done: {done}/{len(paths)}")


if __name__ == "__main__":
    main()
