"""The fast pass over a playlist: what disko needs to shortlist tracks, a few seconds per track.

For each audio file: its tags, tempo and beat positions, key, loudness, onset rate and brightness
(Essentia, over the whole track), then energy (arousal), mood (valence), danceability, voice and
Discogs style from Essentia's pretrained models, over three 40-second windows of the track's body
(within ~0.2 of the full-track values on the house set, at a third of the cost). Bars, sections
and stems are left to allin1 (deep.py), which only runs on a track once it is picked. Writes <out>/<id>.fast.json per file and prints one
JSON line per file on stdout, then {"event": "finished"}.

Runs in the essentia-tensorflow venv, which clashes with the app's packages, one process per core:

    .venv-essentia-tf/bin/python live/workers/fast.py --jobs jobs.json --out DIR [--workers N]

jobs.json: [{"id": ..., "path": absolute path, "rel": path inside the playlist, "bytes": size}, ...]
"""
import argparse
import json
import multiprocessing as mp
import os
import queue
import sys
import time
import traceback
from pathlib import Path

import numpy as np

VERSION = 1
SR = 44100
MODELS = Path(__file__).resolve().parents[2] / "data" / "house" / "models" / "essentia"
EFFNET = "discogs-effnet-bs64-1"
MUSICNN = "msd-musicnn-1"
WINDOWS = (0.25, 0.5, 0.75)  # where the models listen, as a share of the track
WINDOW_S = 40
HEADS = {  # what we keep -> (embedding model, classifier head)
    "style": (EFFNET, "genre_discogs400-discogs-effnet-1"),
    "danceability": (EFFNET, "danceability-discogs-effnet-1"),
    "voice": (EFFNET, "voice_instrumental-discogs-effnet-1"),
    "emotion": (MUSICNN, "emomusic-msd-musicnn-2"),
}

_m = {}  # models, built once per worker process


def _meta(models: Path, name: str) -> dict:
    return json.loads((models / f"{name}.json").read_text())


def _output(meta: dict, purpose: str) -> str:
    outputs = meta["schema"]["outputs"]
    return next((o["name"] for o in outputs if o.get("output_purpose") == purpose), outputs[0]["name"])


def load(models: str):
    # Each worker is one of many: keep TensorFlow to a couple of threads, and quiet.
    os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "2")
    os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    import essentia
    essentia.log.infoActive = essentia.log.warningActive = False
    import essentia.standard as es

    models = Path(models)
    _m["es"] = es
    # Patches side by side rather than overlapping: half the work, the same averages.
    _m[EFFNET] = es.TensorflowPredictEffnetDiscogs(graphFilename=str(models / f"{EFFNET}.pb"), patchHopSize=128,
                                                   output=_output(_meta(models, EFFNET), "embeddings"))
    _m[MUSICNN] = es.TensorflowPredictMusiCNN(graphFilename=str(models / f"{MUSICNN}.pb"), patchHopSize=187,
                                              output=_output(_meta(models, MUSICNN), "embeddings"))
    for key, (_, name) in HEADS.items():
        meta = _meta(models, name)
        _m[key] = (es.TensorflowPredict2D(graphFilename=str(models / f"{name}.pb"),
                                          input=meta["schema"]["inputs"][0]["name"],
                                          output=_output(meta, "predictions")), meta["classes"])


def analyze(path: str) -> dict:
    es = _m["es"]
    title, artist, album, _, genre, _, date, _, _, bitrate, _, _ = es.MetadataReader(filename=path)()
    stereo, sr, channels, _, _, _ = es.AudioLoader(filename=path)()
    if sr != SR:
        stereo = np.stack([es.Resample(inputSampleRate=sr, outputSampleRate=SR)(np.ascontiguousarray(stereo[:, c]))
                           for c in range(2)], axis=1)
    mono = np.ascontiguousarray(stereo.mean(axis=1), dtype=np.float32)
    if len(mono) < SR * 10:
        raise ValueError("shorter than 10 seconds")

    # The same algorithms and settings MusicExtractor uses for these descriptors, minus the rest.
    _, short_term, integrated, _ = es.LoudnessEBUR128(sampleRate=SR)(stereo)
    bpm, beats, _, _, _ = es.RhythmExtractor2013(method="degara")(mono)
    _, onset_rate = es.OnsetRate()(mono)
    key, scale, strength = es.KeyExtractor(profileType="edma")(mono)
    window, spectrum, centroid = es.Windowing(type="blackmanharris62"), es.Spectrum(), es.Centroid(range=SR / 2)
    centroids = [centroid(spectrum(window(f)) ** 2)
                 for f in es.FrameGenerator(es.EqualLoudness()(mono), frameSize=2048, hopSize=1024, startFromZero=True)]

    audio16 = es.Resample(inputSampleRate=SR, outputSampleRate=16000, quality=4)(mono)
    n, half = len(audio16), WINDOW_S * 16000 // 2
    if n > 2 * half * len(WINDOWS):
        audio16 = np.concatenate([audio16[int(n * f) - half:int(n * f) + half] for f in WINDOWS])
    embeddings = {EFFNET: _m[EFFNET](audio16), MUSICNN: _m[MUSICNN](audio16)}
    pred = {}
    for name, (source, _) in HEADS.items():
        model, classes = _m[name]
        mean = np.asarray(model(embeddings[source])).mean(axis=0)
        pred[name] = dict(zip(classes, (float(v) for v in mean)))
    styles = sorted(pred["style"].items(), key=lambda kv: -kv[1])[:10]

    return {
        "version": VERSION,
        "tags": {"title": title, "artist": artist, "album": album, "genre": genre, "date": date},
        "duration": round(len(mono) / SR, 3), "channels": int(channels), "bitrate": int(bitrate),
        "bpm": round(float(bpm), 3), "beats": [round(float(t), 3) for t in beats],
        "key": key, "scale": scale, "key_strength": round(float(strength), 4),
        "loudness_short_term_mean": round(float(np.mean(short_term)), 3),
        "loudness_integrated": round(float(integrated), 3),
        "onset_rate": round(float(onset_rate), 4),
        "spectral_centroid_mean": round(float(np.mean(centroids)), 2),
        "danceability": round(pred["danceability"]["danceable"], 4),
        "voice": round(pred["voice"]["voice"], 4),
        "valence": round(pred["emotion"]["valence"], 4),
        "arousal": round(pred["emotion"]["arousal"], 4),
        "styles": {k: round(v, 4) for k, v in styles},
    }


def run(job: dict, out: str) -> dict:
    start = time.time()
    data = {"path": job["rel"], "bytes": job["bytes"], **analyze(job["path"])}
    dest = Path(out) / f"{job['id']}.fast.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, dest)  # never leave half a file behind if we're stopped mid-write
    return {"title": data["tags"]["title"], "artist": data["tags"]["artist"], "bpm": data["bpm"],
            "key": f"{data['key']} {data['scale']}", "arousal": data["arousal"], "seconds": round(time.time() - start, 2)}


def worker(models: str, out: str, tasks, results):
    """One of the processes: takes jobs until it gets None."""
    load(models)
    while (job := tasks.get()) is not None:
        results.put(("start", os.getpid(), job["id"]))
        try:
            results.put(("done", job["id"], run(job, out)))
        except Exception as e:
            traceback.print_exc()
            results.put(("failed", job["id"], f"{type(e).__name__}: {e}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jobs", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", default=str(MODELS))
    ap.add_argument("--workers", type=int, default=max(2, min((os.cpu_count() or 4) - 4, 12)))
    args = ap.parse_args()

    # Our stdout is the protocol; anything a library prints goes to stderr instead.
    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)

    def say(**msg):
        try:
            proto.write(json.dumps(msg) + "\n")
        except BrokenPipeError:  # the app is gone
            os._exit(0)

    missing = [n for n in [EFFNET, MUSICNN] + [h for _, h in HEADS.values()]
               if not (Path(args.models) / f"{n}.pb").exists()]
    if missing:
        say(event="fatal", error=f"Essentia models missing from {args.models}: {', '.join(missing)}")
        return
    jobs = sorted(json.loads(Path(args.jobs).read_text()), key=lambda j: -j["bytes"])  # long tracks first
    left = {j["id"]: j for j in jobs}
    n = min(args.workers, len(jobs))
    say(event="started", total=len(jobs), workers=n)

    # Our own pool rather than ProcessPoolExecutor: a file that crashes the decoder takes down its
    # process, and with an executor every other job with it. Here only that file fails.
    ctx = mp.get_context("spawn")
    tasks, results = ctx.Queue(), ctx.Queue()
    for job in jobs:
        tasks.put(job)
    procs, running, crashes = {}, {}, 0

    def spawn():
        p = ctx.Process(target=worker, args=(args.models, args.out, tasks, results), daemon=True)
        p.start()
        procs[p.pid] = p

    for _ in range(n):
        spawn()
    while left:
        try:
            kind, a, b = results.get(timeout=1)
        except queue.Empty:
            for pid, p in list(procs.items()):
                if p.is_alive():
                    continue
                del procs[pid]
                job = left.pop(running.pop(pid, None), None)
                if job is None:  # died before taking a job: models broken? don't spin forever
                    crashes += 1
                    if crashes > 2 * n:
                        say(event="fatal", error="the analysis processes keep crashing; see index.log")
                        return
                else:
                    say(event="failed", id=job["id"], rel=job["rel"], error="the decoder crashed on this file")
                if left:
                    spawn()
            continue
        if kind == "start":
            running[a] = b
        elif kind == "done" and a in left:
            job = left.pop(a)
            say(event="done", id=a, rel=job["rel"], **b)
        elif kind == "failed" and a in left:
            job = left.pop(a)
            say(event="failed", id=a, rel=job["rel"], error=b)
    for _ in procs:
        tasks.put(None)
    say(event="finished")


if __name__ == "__main__":
    main()
