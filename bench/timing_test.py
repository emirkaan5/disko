"""Timing test: from track structure alone, can a System One model predict where real DJs mixed?

    .venv/bin/python -m bench.timing_test [--backend laya|kev] [--state NAME|all] [--limit N] [--no-reverse]

Each Raveform transition becomes one request with five choice questions, scored against what the DJ
actually did and against three baselines:

  random  pick uniformly among the options (expected score)
  rule    textbook: start the incoming track at bar 1 and overlap its intro with the end of the
          outgoing track
  prior   the answer DJs pick most often (section position counted from the track start or end),
          counted on the other folds, split by mix

Scores are reported on the dev folds (0-2, for choosing a state wording) and the test folds (3-4, for
the final read) separately. `--no-reverse` skips the second pass that re-asks every question with the
options reversed, which measures how much the answers depend on option order.

Backends: `laya` runs Laya's Router in-process, as in jev_test.py; `kev` calls a Kev server on
`--kev-url` (start it from external/kev with `uv run --extra serve python -m kev.serve --run
jaredpalmer/kev-4b --port 8009`).
"""
import argparse
import json
import math
import time
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from statistics import mean

from bench.raveform import WINDOW, load_transitions

RESULTS = Path(__file__).resolve().parent.parent / "results" / "timing"

INSTRUCTIONS = {
    "incoming_start": "Where in the incoming track should the DJ start playing it?",
    "outgoing_at_start": "Which part of the outgoing track should be playing when the DJ brings in the incoming track?",
    "outgoing_end": "In which part of the outgoing track should the DJ finish taking it out?",
    "incoming_at_end": "Which part of the incoming track should be playing once the outgoing track is gone?",
    "overlap": "How long should both tracks play together?",
}
QUESTIONS = list(INSTRUCTIONS)

OVERLAP = {
    "cut": "under 1 bar, a straight cut",
    "short": "1 to 8 bars",
    "medium": "9 to 16 bars",
    "long": "17 to 32 bars",
    "very_long": "more than 32 bars",
}
FOLDS = 5
DEV_FOLDS = {0, 1, 2}

# ---------------------------------------------------------------------------------- state wordings
PERSONA = ("You are supposed to be an excellent dj, you are given two tracks and you need to decide on the best way "
           "to mix them. You can mix them at any point in the tracks.")
SITUATION = ("Consecutive tracks in a club DJ mix: the DJ blends from the outgoing track into the incoming one, "
             "beatmatched and phrase-aligned.")
SECTION_GUIDE = ("intro/outro: beat-led, made for mixing; buildup: rising tension; drop: full energy; breakdown: calm, "
                 "no kick; cooldown: energy easing off")
TIPS = ("Club DJs usually start the incoming track at its 1st or 2nd section, bring it in during the outgoing track's "
        "last drop or cooldown rather than its outro, overlap 8 to 32 bars, and remove the outgoing track once the "
        "incoming one is past its intro.")


def overlap_bucket(bars: float) -> str:
    for key, upper in (("cut", 1), ("short", 8.5), ("medium", 16.5), ("long", 32.5)):
        if bars < upper:
            return key
    return "very_long"


def tempo_relation(a, b) -> str:
    pct = (b.bpm / a.bpm - 1) * 100
    if abs(pct) <= 0.5:
        return "same tempo"
    word = "faster" if pct > 0 else "slower"
    size = "slightly" if abs(pct) <= 3 else "noticeably" if abs(pct) <= 8 else "much"
    return f"incoming track is {size} {word}"


def describe(track) -> str:
    sections = ", ".join(f"{s.label} {s.bars}" for s in track.sections)
    return f"{track.genre}, {round(track.bpm)} BPM. Sections in order, with length in bars: {sections}"


def ordinal(n: int) -> str:
    return {1: "1st", 2: "2nd", 3: "3rd"}.get(n, f"{n}th")


def plain_options(track, i: int, outgoing: bool) -> str:
    s = track.sections[i]
    return f"{s.label}, bars {s.bar_start} to {s.bar_start + s.bars - 1}"


def positional_options(track, i: int, outgoing: bool) -> str:
    """Spell out where a section sits, so answering needs no bar arithmetic."""
    s = track.sections[i]
    if not outgoing:
        return f"{s.label}, {ordinal(i + 1)} section, {s.bars} bars, from bar {s.bar_start}"
    from_end = len(track.sections) - i
    end_bar = track.sections[-1].bar_start + track.sections[-1].bars
    where = "last section" if from_end == 1 else f"{ordinal(from_end)} from end"
    return f"{s.label}, {where}, {s.bars} bars, {end_bar - s.bar_start} bars left"


def facts(t) -> dict:
    return {"outgoing_track": describe(t.a), "incoming_track": describe(t.b), "tempo": tempo_relation(t.a, t.b)}


# name -> (state builder, option text builder). The track facts come first: Laya cuts an over-long state
# from the end, and the general guidance is what can best be lost.
STATES = {
    "plain": (facts, plain_options),
    "persona": (lambda t: {"state_prompt": PERSONA, **facts(t)}, plain_options),
    "structured": (lambda t: {**facts(t), "situation": SITUATION, "section_types": SECTION_GUIDE}, positional_options),
    "tips": (lambda t: {**facts(t), "situation": SITUATION, "section_types": SECTION_GUIDE, "dj_practice": TIPS},
             positional_options),
}


def windows(t):
    """Indices of the sections offered as options: the last WINDOW of A, the first WINDOW of B."""
    return list(range(max(0, len(t.a.sections) - WINDOW), len(t.a.sections))), list(range(min(WINDOW, len(t.b.sections))))


def build_questions(t, variant: str = "plain", reverse: bool = False) -> dict:
    option_text = STATES[variant][1]
    a_idx, b_idx = windows(t)
    a_opts = {t.a.sections[i].key: option_text(t.a, i, True) for i in a_idx}
    b_opts = {t.b.sections[i].key: option_text(t.b, i, False) for i in b_idx}
    criteria = {"incoming_start": b_opts, "outgoing_at_start": a_opts, "outgoing_end": a_opts,
                "incoming_at_end": b_opts, "overlap": dict(OVERLAP)}
    if reverse:
        criteria = {q: dict(reversed(list(c.items()))) for q, c in criteria.items()}
    return {q: {"type": "choice", "instructions": INSTRUCTIONS[q], "criteria": dict(criteria[q])} for q in QUESTIONS}


def gold(t) -> dict:
    return {
        "incoming_start": t.b.sections[t.labels["incoming_start"]].key,
        "outgoing_at_start": t.a.sections[t.labels["outgoing_at_start"]].key,
        "outgoing_end": t.a.sections[t.labels["outgoing_end"]].key,
        "incoming_at_end": t.b.sections[t.labels["incoming_at_end"]].key,
        "overlap": overlap_bucket(t.labels["overlap"]),
    }


def position(t, q: str, key: str) -> int:
    """Where an option sits: its section index, or its overlap bucket index."""
    if q == "overlap":
        return list(OVERLAP).index(key)
    track = t.b if q.startswith("incoming") else t.a
    return next(i for i, s in enumerate(track.sections) if s.key == key)


def role(t, q: str, key: str):
    """Track-independent identity of an option: section index from the start of B / end of A."""
    if q == "overlap":
        return key
    i = position(t, q, key)
    return i if q.startswith("incoming") else len(t.a.sections) - 1 - i


def fold(t) -> int:
    return zlib.crc32(t.mix_id.encode()) % FOLDS


# ---------------------------------------------------------------------------------- predictors
def rule_predictions(transitions) -> list:
    def section_at_bar(track, bar):
        for i, s in enumerate(track.sections):
            if bar < s.bar_start + s.bars:
                return i
        return len(track.sections) - 1

    out = []
    for t in transitions:
        a_idx, b_idx = windows(t)
        intro = t.b.sections[0].bars
        a_bars = t.a.sections[-1].bar_start + t.a.sections[-1].bars - 1
        a_at_start = min(max(section_at_bar(t.a, a_bars - intro + 1), a_idx[0]), a_idx[-1])
        b_at_end = min(section_at_bar(t.b, intro + 1), b_idx[-1])
        picks = {
            "incoming_start": t.b.sections[0].key,
            "outgoing_at_start": t.a.sections[a_at_start].key,
            "outgoing_end": t.a.sections[-1].key,
            "incoming_at_end": t.b.sections[b_at_end].key,
            "overlap": overlap_bucket(intro),
        }
        out.append({q: {k: float(k == picks[q]) for k in c["criteria"]} for q, c in build_questions(t).items()})
    return out


def prior_predictions(transitions) -> list:
    counts = [{q: Counter() for q in QUESTIONS} for _ in range(FOLDS)]
    for t in transitions:
        for q, key in gold(t).items():
            counts[fold(t)][q][role(t, q, key)] += 1
    out = []
    for t in transitions:
        train = {q: sum((counts[f][q] for f in range(FOLDS) if f != fold(t)), Counter()) for q in QUESTIONS}
        probs = {}
        for q, spec in build_questions(t).items():
            weights = {k: train[q][role(t, q, k)] + 1 for k in spec["criteria"]}
            total = sum(weights.values())
            probs[q] = {k: w / total for k, w in weights.items()}
        out.append(probs)
    return out


def uniform_predictions(transitions) -> list:
    return [{q: {k: 1 / len(c["criteria"]) for k in c["criteria"]} for q, c in build_questions(t).items()}
            for t in transitions]


class LayaBackend:
    """Laya's Router in-process, exactly as jev_test.py uses it."""

    def __init__(self, model=None):
        from laya import Router
        self.router, self.model = Router(), model
        self.name = f"laya-{model or 'router'}"

    def predict(self, state, questions):
        return self.router.predict(state, questions, model=self.model)["answers"]


class KevBackend:
    """A Kev server speaking TypeSafe's /v1/systemone."""

    def __init__(self, url):
        import httpx
        self.client = httpx.Client(base_url=url, timeout=300)
        info = self.client.get("/v1/models").json()["models"][0]
        self.name = "kev-" + info["run"].split("/")[-1]
        print(f"kev server: {info['run']} on {info['device']} ({info['backend']}, {info['dtype']}), T={info['temperature']:.2f}")

    def predict(self, state, questions):
        resp = self.client.post("/v1/systemone", json={"model": "kev-latest", "state": state, "questions": questions})
        resp.raise_for_status()
        return resp.json()["answers"]


def model_predictions(backend, transitions, variant, reverse=False, concurrency=1):
    """Predictions in transition order, plus per-request latency in ms."""
    def one(t):
        started = time.perf_counter()
        answers = backend.predict(STATES[variant][0](t), build_questions(t, variant, reverse))
        return {q: answers[q]["probabilities"] for q in QUESTIONS}, (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = []
        for i, result in enumerate(pool.map(one, transitions), 1):
            results.append(result)
            if i % 100 == 0:
                print(f"  {variant}{' reversed' if reverse else ''} {i}/{len(transitions)}", flush=True)
    return [p for p, _ in results], [ms for _, ms in results]


# ---------------------------------------------------------------------------------- scoring
def expected_calibration_error(conf, correct, bins=10) -> float:
    total, err = len(conf), 0.0
    for b in range(bins):
        idx = [i for i, c in enumerate(conf) if b / bins < c <= (b + 1) / bins or (b == 0 and c == 0)]
        if idx:
            err += len(idx) / total * abs(mean(correct[i] for i in idx) - mean(conf[i] for i in idx))
    return err


def score(transitions, predictions, expected=False) -> dict:
    """Per-question accuracy, top-2, within-one (adjacent section/bucket), log loss and ECE.

    `expected=True` scores a uniform guesser by its expected value instead of its (arbitrary) argmax.
    """
    out = {}
    for q in QUESTIONS:
        acc, top2, near, nll, conf = [], [], [], [], []
        for t, pred in zip(transitions, predictions):
            p, g = pred[q], gold(t)[q]
            g_pos = position(t, q, g)
            nll.append(-math.log(max(p.get(g, 0.0), 1e-6)))
            if expected:
                k = len(p)
                acc.append(1 / k)
                top2.append(min(2, k) / k)
                near.append(sum(abs(position(t, q, key) - g_pos) <= 1 for key in p) / k)
                continue
            ranked = sorted(p, key=p.get, reverse=True)
            acc.append(ranked[0] == g)
            top2.append(g in ranked[:2])
            near.append(abs(position(t, q, ranked[0]) - g_pos) <= 1)
            conf.append(p[ranked[0]])
        out[q] = {"accuracy": mean(acc), "top2": mean(top2), "within_one": mean(near), "log_loss": mean(nll)}
        if conf:
            out[q]["ece"] = expected_calibration_error(conf, [bool(x) for x in acc])
    return out


def all_right(transitions, predictions) -> float:
    return mean(all(max(pred[q], key=pred[q].get) == gold(t)[q] for q in QUESTIONS)
                for t, pred in zip(transitions, predictions))


def flip_rates(first, second) -> dict:
    return {q: mean(max(a[q], key=a[q].get) != max(b[q], key=b[q].get) for a, b in zip(first, second))
            for q in QUESTIONS}


def print_table(title, n, scores, flips, model_name):
    print(f"\n{title}: {n} transitions. Accuracy (exact section / bucket):")
    print(f"{'question':20s} {'random':>7s} {'rule':>7s} {'prior':>7s} {'model':>7s} | {'top2':>5s} {'±1':>5s}"
          f" {'logloss model/prior':>20s} {'ECE':>5s} {'flips':>6s}")
    for q in QUESTIONS:
        r, ru, pr, m = (scores[s][q] for s in ("random", "rule", "prior", model_name))
        flip = f"{flips[q]:.0%}" if flips else "-"
        print(f"{q:20s} {r['accuracy']:7.0%} {ru['accuracy']:7.0%} {pr['accuracy']:7.0%} {m['accuracy']:7.0%} |"
              f" {m['top2']:5.0%} {m['within_one']:5.0%} {m['log_loss']:10.2f} / {pr['log_loss']:<7.2f} {m['ece']:5.2f} {flip:>6s}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["laya", "kev"], default="laya")
    parser.add_argument("--state", default="structured", help=f"state wording: {', '.join(STATES)} or all")
    parser.add_argument("--limit", type=int, help="only the first N transitions (quick check)")
    parser.add_argument("--no-reverse", action="store_true", help="skip the reversed-option pass")
    parser.add_argument("--model", help="force a Laya checkpoint (default: the Router's own pick)")
    parser.add_argument("--kev-url", default="http://127.0.0.1:8009")
    parser.add_argument("--concurrency", type=int, default=1,
                        help="requests in flight to a Kev server (MLX serves one at a time; helps on CUDA)")
    args = parser.parse_args()
    concurrency = args.concurrency if args.backend == "kev" else 1
    variants = list(STATES) if args.state == "all" else [args.state]

    transitions, dropped = load_transitions()
    print(f"{len(transitions)} usable transitions from {len({t.mix_id for t in transitions})} mixes; dropped {dropped}")
    if args.limit:
        transitions = transitions[: args.limit]
    backend = LayaBackend(args.model) if args.backend == "laya" else KevBackend(args.kev_url)

    baselines = {"random": uniform_predictions(transitions), "rule": rule_predictions(transitions),
                 "prior": prior_predictions(transitions)}
    splits = {"dev": [i for i, t in enumerate(transitions) if fold(t) in DEV_FOLDS],
              "test": [i for i, t in enumerate(transitions) if fold(t) not in DEV_FOLDS]}
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    summary = {}
    for variant in variants:
        started = time.perf_counter()
        preds, ms = model_predictions(backend, transitions, variant, concurrency=concurrency)
        wall = time.perf_counter() - started
        print(f"{backend.name} / {variant}: {mean(ms):.0f} ms per request, {len(transitions) / wall:.1f} requests/s")
        rev = None if args.no_reverse else model_predictions(backend, transitions, variant, True, concurrency)[0]
        predictions = {**baselines, backend.name: preds}
        summary[variant] = {"ms_per_request": mean(ms)}
        for split, idx in splits.items():
            ts = [transitions[i] for i in idx]
            sub = {name: [p[i] for i in idx] for name, p in predictions.items()}
            scores = {name: score(ts, p, expected=(name == "random")) for name, p in sub.items()}
            flips = flip_rates(sub[backend.name], [rev[i] for i in idx]) if rev else None
            print_table(f"{backend.name} / {variant} / {split}", len(ts), scores, flips, backend.name)
            summary[variant][split] = {"n": len(ts), "scores": scores, "flip_rates": flips,
                                       "all_five_right": {n: all_right(ts, sub[n]) for n in ("rule", "prior", backend.name)}}

        out_dir = RESULTS / f"{stamp}-{backend.name}-{variant}"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "summary.json").write_text(json.dumps({"backend": backend.name, "state": variant, "dropped": dropped,
                                                           **summary[variant]}, indent=2))
        with open(out_dir / "predictions.jsonl", "w") as f:
            for i, t in enumerate(transitions):
                f.write(json.dumps({
                    "mix": t.mix_id, "fold": fold(t), "outgoing": t.a.title, "incoming": t.b.title,
                    "overlap_bars": t.overlap_bars, "state": STATES[variant][0](t), "gold": gold(t),
                    "model": preds[i], "model_reversed": rev[i] if rev else None,
                    "prior": baselines["prior"][i], "rule": baselines["rule"][i],
                }) + "\n")
        print(f"wrote {out_dir}")

    if len(variants) > 1:
        print(f"\n{backend.name}: accuracy by state wording (dev / test)")
        print(f"{'question':20s} " + " ".join(f"{v:>13s}" for v in variants) + f" {'prior':>13s}")
        for q in QUESTIONS:
            cells = [f"{summary[v]['dev']['scores'][backend.name][q]['accuracy']:5.0%} / "
                     f"{summary[v]['test']['scores'][backend.name][q]['accuracy']:4.0%}" for v in variants]
            prior = f"{summary[variants[0]]['dev']['scores']['prior'][q]['accuracy']:5.0%} / " \
                    f"{summary[variants[0]]['test']['scores']['prior'][q]['accuracy']:4.0%}"
            print(f"{q:20s} " + " ".join(f"{c:>13s}" for c in cells) + f" {prior:>13s}")


if __name__ == "__main__":
    main()
