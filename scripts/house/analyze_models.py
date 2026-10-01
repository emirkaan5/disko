"""Run Essentia's pretrained models on every house track: style, mood, instruments, vibe.

Discogs-EffNet embeddings feed classifier heads for 400 Discogs styles (e.g. "Electronic---Deep
House"), 56 MTG-Jamendo moods/themes, 40 instruments, five binary moods, danceability,
voice vs. instrumental, approachability and engagement. MusiCNN embeddings feed the emoMusic
head for valence and arousal (1-9 scale). Every prediction is averaged over the track and saved
to data/house/analysis/models/<source>_<id>.json. Needs essentia-tensorflow, which clashes with
plain essentia, so it runs from its own venv:

    .venv-essentia-tf/bin/python scripts/house/analyze_models.py --workers 6

Models: https://essentia.upf.edu/models (CC BY-NC-SA 4.0, non-commercial; UPF sells commercial
licenses), downloaded to data/house/models/essentia.
"""
import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
MODELS = ROOT / "models" / "essentia"
OUT = ROOT / "analysis" / "models"

EFFNET = "discogs-effnet-bs64-1"
MUSICNN = "msd-musicnn-1"
EFFNET_HEADS = {
    "style": "genre_discogs400-discogs-effnet-1",
    "moodtheme": "mtg_jamendo_moodtheme-discogs-effnet-1",
    "instrument": "mtg_jamendo_instrument-discogs-effnet-1",
    "mood_happy": "mood_happy-discogs-effnet-1",
    "mood_sad": "mood_sad-discogs-effnet-1",
    "mood_party": "mood_party-discogs-effnet-1",
    "mood_relaxed": "mood_relaxed-discogs-effnet-1",
    "mood_aggressive": "mood_aggressive-discogs-effnet-1",
    "danceability": "danceability-discogs-effnet-1",
    "voice_instrumental": "voice_instrumental-discogs-effnet-1",
    "approachability": "approachability_regression-discogs-effnet-1",
    "engagement": "engagement_regression-discogs-effnet-1",
}
MUSICNN_HEADS = {"emomusic": "emomusic-msd-musicnn-2"}

_models = {}


def meta(name: str) -> dict:
    return json.loads((MODELS / f"{name}.json").read_text())


def output_node(m: dict, purpose: str) -> str:
    outputs = m["schema"]["outputs"]
    return next((o["name"] for o in outputs if o.get("output_purpose") == purpose), outputs[0]["name"])


def load():
    """Build every model once per worker process."""
    import essentia
    essentia.log.infoActive = essentia.log.warningActive = False
    import essentia.standard as es

    _models["effnet"] = es.TensorflowPredictEffnetDiscogs(
        graphFilename=str(MODELS / f"{EFFNET}.pb"), output=output_node(meta(EFFNET), "embeddings"))
    _models["musicnn"] = es.TensorflowPredictMusiCNN(
        graphFilename=str(MODELS / f"{MUSICNN}.pb"), output=output_node(meta(MUSICNN), "embeddings"))
    for key, name in {**EFFNET_HEADS, **MUSICNN_HEADS}.items():
        m = meta(name)
        _models[key] = (es.TensorflowPredict2D(graphFilename=str(MODELS / f"{name}.pb"),
                                               input=m["schema"]["inputs"][0]["name"],
                                               output=output_node(m, "predictions")),
                        m["classes"])
    _models["loader"] = es.MonoLoader


def analyze(path: Path) -> dict:
    if not _models:
        load()
    audio = _models["loader"](filename=str(path), sampleRate=16000, resampleQuality=4)()
    embeddings = {"effnet": _models["effnet"](audio), "musicnn": _models["musicnn"](audio)}
    out = {}
    for key in {**EFFNET_HEADS, **MUSICNN_HEADS}:
        model, classes = _models[key]
        pred = np.asarray(model(embeddings["musicnn" if key in MUSICNN_HEADS else "effnet"])).mean(axis=0)
        out[key] = {c: round(float(p), 5) for c, p in zip(classes, pred)}
    return out


def run(path: Path) -> str:
    dest = OUT / f"{path.parent.name}_{path.stem}.json"
    if not dest.exists():
        dest.write_text(json.dumps(analyze(path)))
    return dest.name


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, help="analyze at most this many tracks (for a quick check)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    paths = sorted((ROOT / "audio").glob("*/*.mp3"))[:args.limit]
    print(f"{len(paths)} tracks", flush=True)
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
