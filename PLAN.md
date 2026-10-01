# disko: plan

An AI DJ. A **System One decision model** (Jev, Kev or Laya) picks the next track from a local music library and decides how to mix into it. Ordinary code analyses the audio, keeps the model's options valid, and renders beatmatched, phrase-aligned transitions.

> **Status, 2026-09-27:** plan only; nothing is built yet. Open decisions are listed in [section 14](#14-open-decisions). Until they're settled, this plan assumes the defaults there.

## Contents

1. [Goal and scope](#1-goal-and-scope)
2. [System One models](#2-system-one-models)
3. [Design principles](#3-design-principles)
4. [Architecture](#4-architecture)
5. [Analysis: track cards](#5-analysis-track-cards)
6. [Brain: decisions](#6-brain-decisions)
7. [Engine: rendering](#7-engine-rendering)
8. [Transition techniques](#8-transition-techniques)
9. [Later modes: knob mode and live](#9-later-modes-knob-mode-and-live)
10. [Tech stack](#10-tech-stack)
11. [Roadmap](#11-roadmap)
12. [Evaluation and learning](#12-evaluation-and-learning)
13. [Risks](#13-risks)
14. [Open decisions](#14-open-decisions)
15. [References](#15-references)

## 1. Goal and scope

Given a folder of music and a seed track, disko plays a set. At every step it:

1. picks the next track,
2. decides how to get there: technique (EQ bass swap, filter sweep, echo-out, cut, crossfade), overlap length, and where in each track the transition happens,
3. renders the transition, beatmatched and on the phrase.

**MVP scope:** offline rendering to an audio file · 4/4 electronic music (house/techno) · five techniques · a decision log that explains every choice.

**Later:** knob mode (the model works the EQ and filters move by move) · more techniques · fine-tuned local models · live playback with controls.

**Non-goals for now:** AI-generated audio · streaming services (they don't expose raw audio) · DJ hardware and controllers · MC/voice · beatless or odd-meter music.

## 2. System One models

System One models are non-autoregressive decision models, first released in September 2026. You send a **state** (text or JSON) and a set of typed **questions**. One forward pass returns calibrated probabilities, with no text generation.

| Primitive | Asks | Returns |
|---|---|---|
| `choice` | Pick one of N described options | Chosen key + probability per option |
| `score` | Place on an ordered rubric of 2–10 described levels | Probability-weighted score |
| `noul` | Yes or no | P(true) |

Every backend below speaks TypeSafe's `POST /v1/systemone` protocol, so switching backends is a config change:

```json
{
  "model": "jev-latest",
  "state": "string, object or array",
  "questions": {
    "some_choice": { "type": "choice", "instructions": "...", "criteria": { "key_a": "description", "key_b": "description" } },
    "some_score":  { "type": "score",  "instructions": "...", "criteria": ["level 0", "level 1", "level 2"] },
    "some_noul":   { "type": "noul",   "instructions": "..." }
  }
}
```

Each answer comes back under `answers.<id>`, holding `choice` + `probabilities`, `score`, or `noul`, plus a `confidence`. The response also includes a `usage` block.

| | Jev | Kev | Laya |
|---|---|---|---|
| Maker | TypeSafe AI | Jared Palmer | Convai Innovations |
| Access | Hosted API (key needed) | Open weights, Apache-2.0 | Open weights, Apache-2.0 |
| Size | Undisclosed | 0.8B / 4B / 9B (Qwen3.5 base) | 421M (ModernBERT-large) |
| On a Mac | n/a | Yes, via MLX (4B: ~0.7 s per uncached request on an M5) | Yes, CPU or MPS |
| Context | Not documented | Serves 64k tokens, but trained on states of ≤384 tokens | English checkpoint: 512 tokens, options share ~192 |
| Options per `choice` | ≤255 | ≤255 | ~20 before accuracy drops |
| Cost | $0.042 per million input tokens, output free | Own hardware | Own hardware |
| Fine-tuning | No | `python -m kev.train` on labelled JSONL | Kaggle notebook (free 2×T4) |

For Laya, start with the `laya-typed-decisions` checkpoint (1,024-token context). On typed decisions, the base English checkpoint scores 0.36 zero-shot and the fine-tuned one scores 0.77.

**Known weaknesses that shape this design:**

- Weak at numbers, arithmetic, counting and dates → do the math in code and pass named buckets.
- Irrelevant context distracts → keep states small and focused.
- Option order can change the answer (documented for Kev) → score candidates one at a time, or shuffle and average.
- Questions in one request can't see each other's answers → dependent decisions need sequential requests.
- Reads instructions literally → use exact, unambiguous wording.
- Laya: `noul` can follow its true/false labels instead of the state, and `score` is its weakest primitive → on Laya, prefer `choice` with meaningful labels.
- No audio input → the model knows only what the analysis layer tells it.

**Where these models actually help here:**

- Calibrated probabilities: sample for variety, fall back when unsure.
- Typed answers drive code directly.
- Cheap enough to score every shortlisted track on several questions. That's roughly 1¢ per 2-hour set on Jev (rough estimate) and free locally.
- Small open models can be fine-tuned on our own ratings.

Speed barely matters for picking tracks, since there are minutes to decide. It starts to matter in knob mode and live mode.

## 3. Design principles

1. **Code in control, narrow decisions.** Code enumerates only valid options: reachable tempo, phrase-aligned cue points, techniques the track structure allows. The model picks among them and never produces free-form output.
2. **The model can't hear.** Track cards are its only view of the music, so card quality caps DJ quality. The card format is versioned and iterated against evals.
3. **No raw numbers in state.** BPM gaps, key distance, energy and bar counts become named buckets ("close match", "compatible neighbor", "lift").
4. **Small states.** At most ~300 tokens of state per request, which fits Kev's training range and Laya's window. Track selection scores one candidate per request.
5. **Decompose, then compose.** Ask several single-property questions and combine them with explicit weights in code, so results are tunable and explainable.
6. **Use the probabilities.** Sample with a temperature for variety. Gate on confidence, with safe fallbacks. Compute confidence ourselves from the returned probabilities, because backends define `confidence` differently (Jev: `(n·p_max − 1)/(n − 1)`, Laya: 1 − entropy).
7. **Backend-agnostic.** One `/v1/systemone` client; the backend is a setting.
8. **Log everything.** Every request, answer and probability is saved with the mix. The log feeds the "why" view, debugging and future training data.

## 4. Architecture

```
local music files
  │  ANALYSIS (once per track, cached)
  ▼  beats/bars/phrases · labelled sections · key · energy per bar · stems → vocals? bass?
track cards: short text, numbers pre-bucketed ("tempo: close match", "key: compatible")
  │  BRAIN
  ▼  code shortlists + checks what's possible → System One decides → code combines + samples
transition plan: next track · technique · length · mix-out / mix-in points
  │  ENGINE
  ▼  warp both decks onto one beat grid · 3-band EQ · filter · echo · fader · limiter
mix.wav + mix.cue + mix.json (every decision, with probabilities)
```

Proposed layout:

```
disko/
  analysis/        audio → TrackCard, cached in SQLite
    beats.py       beats, downbeats, bars, phrases
    sections.py    labelled segments
    key.py         key → Camelot
    energy.py      loudness, per-bar energy
    stems.py       per-bar vocal / bass / drums activity
    card.py        TrackCard schema, bucketing, text rendering
  brain/
    client.py      /v1/systemone client (Jev | Kev | Laya)
    setplan.py     target energy arc over the set
    shortlist.py   hard constraints + cheap ranking
    select.py      next-track scoring + sampling
    transition.py  technique, length and cue-point decisions
    questions.py   question definitions (versioned)
  engine/
    timeline.py    master beat grid + tempo curve
    warp.py        time-map stretching onto the grid
    deck.py        per-deck chain: EQ → filter → FX sends → fader
    dsp/           isolator EQ, filters, echo, reverb, limiter
    recipes/       one module per technique → automation curves
    render.py      block-based offline renderer
  eval/
    baselines.py   random-valid, heuristic
    metrics.py     automatic checks
    review/        blind A/B + rating UI
    benchmarks/    next-track prediction on real DJ tracklists
  cli.py           disko analyze | inspect | render | mix | review | eval
```

## 5. Analysis: track cards

Computed once per track and cached, keyed by a hash of the audio content.

| Field | Source | Notes |
|---|---|---|
| Title, artist, genre, year | File tags | Genre tags are often missing or wrong; an audio tagger can come later |
| BPM, beats, downbeats | allin1 | Check for half/double-tempo errors; store a confidence flag |
| Bars, phrases | Derived | 4/4 bars; 8/16/32-bar phrases aligned to downbeats |
| Sections | allin1 | Bar ranges. allin1's labels are pop-style (verse / chorus), but EDM needs buildup / drop / breakdown. Check against Raveform's annotated tracks, then map the labels or retrain if needed |
| Key → Camelot | Essentia `KeyExtractor`, EDM profile | Keep the key strength |
| Loudness | pyloudnorm | Integrated LUFS, for gain staging |
| Energy per bar | RMS, onset density, spectral balance | Bucketed low / medium / high, plus trend |
| Layers per bar | Demucs stems | Which of vocals / bass / drums / other are playing |
| Vibe tags | Later: CLAP zero-shot or a genre classifier | Optional |

The model sees three text forms built from the cards:

- **Summary card** (~60 tokens, for track selection): genre, vibe, energy level and shape, vocal or instrumental.
- **Section card** (for transitions): what a given window sounds like, e.g. "outro, 32 bars left: drums and bass, no vocals, energy easing off".
- **Relation line** (pairwise, computed in code): tempo, key and energy relations as buckets. Genre isn't bucketed; the model compares the genre words on the two cards itself.

Starting buckets, to be tuned:

| Relation | Buckets |
|---|---|
| Tempo | exact (≤0.5%) · close (≤3%) · stretchable (≤6%) · half/double time · too far |
| Key (Camelot) | same · neighbor (±1 or relative major/minor) · boost (+2) · clash |
| Energy step | big drop · drop · hold · lift · big lift |

## 6. Brain: decisions

### 6.1 Set plan

A target energy arc over the length of the set: warm-up → build → peak → cool-down. The MVP uses a code template scaled to `--minutes`. The current phase goes into every state as a bucket.

Optional later: an LLM turns a free-text brief ("sunset rooftop, deep into melodic techno") into the arc and a genre path, once, up front. System 2 plans, System 1 plays.

### 6.2 Next track

1. **Shortlist (code).**
   - Drop tracks already played, and artists heard in the last few tracks.
   - Require a tempo bucket other than "too far".
   - Prefer non-clashing keys (a soft preference, not a rule).
   - Rank by cheap similarity to the current track and the energy target, then keep the top K (≈12–20).
2. **Score (model).** One request per candidate, all in parallel. The state holds the set phase and last three tracks, the now-playing summary card, the candidate's summary card, and the relation line.

   | Question | Type | Instructions | Criteria |
   |---|---|---|---|
   | `flow` | score | How naturally would the candidate follow the current track? | Jarring · Noticeable shift · Smooth · Perfect follow-up |
   | `energy_fit` | score | Does the candidate move the energy the way this part of the set needs? | Wrong direction · Neutral · Right direction |
   | `repetitive` | noul | Would the candidate feel repetitive after the last three tracks? | none |
   | `moment` (peak phase only) | noul | Is the candidate a big crowd moment? | none |

3. **Combine (code).**
   - Normalise each score to 0–1, then compute `s = 0.5·flow + 0.3·energy_fit − 0.2·P(repetitive) + key_bonus`.
   - Pick a track with probability `p_i ∝ exp(s_i / τ)`. The temperature τ is the "adventurousness" knob.
   - The weights start hand-set and are later learned from ratings (§12).
4. **Log** every candidate's answers, which powers a "why this track" view.

An alternative to A/B test in evaluation: a single `choice` over the whole shortlist (Jev only, since it handles long option lists), with shuffled option order, averaged.

### 6.3 Transition

This takes two requests in sequence, because answers within one request can't depend on each other.

**Request A: technique, length, character.** The state holds the set phase, a section card for the outgoing track's likely mix-out region, a section card for the incoming track's opening, and the relation line. Code offers only feasible options:

- `bass_swap` and `filter_sweep` need the tempo within "stretchable", plus enough runway on both tracks for the overlap length.
- `cut` needs a strong entry point in the incoming track: a drop, or a full-energy phrase start.
- `echo_out` and `crossfade` are always feasible. When tempo is "too far", `crossfade` is the only overlap technique allowed.
- Lengths longer than the available runway are not offered.

Example request (illustrative):

```json
{
  "model": "jev-latest",
  "state": {
    "set": "38 of 90 min, building toward the peak",
    "outgoing": "deep house, in its outro (32 bars left): drums and bass, no vocals, energy easing off",
    "incoming": "tech house, 16-bar drums-only intro, bassline enters at bar 17, vocals at bar 33",
    "relation": "tempo: close match; key: compatible neighbor; energy: lift"
  },
  "questions": {
    "technique": {
      "type": "choice",
      "instructions": "How should the DJ move from the outgoing track into the incoming track?",
      "criteria": {
        "bass_swap": "Long EQ blend that swaps the basslines on a phrase",
        "filter_sweep": "High-pass the outgoing track away while the incoming track's filter opens",
        "echo_out": "Echo the outgoing track out and drop the incoming track on the downbeat",
        "cut": "Hard cut on the downbeat"
      }
    },
    "length": {
      "type": "choice",
      "instructions": "How long should both tracks play together?",
      "criteria": {
        "short": "About 8 bars",
        "medium": "About 16 bars",
        "long": "About 32 bars"
      }
    },
    "big_moment": {
      "type": "noul",
      "instructions": "Should this switch be a noticeable moment for the crowd rather than a seamless blend?"
    }
  }
}
```

Example answer (illustrative):

```json
{
  "answers": {
    "technique": { "type": "choice", "choice": "bass_swap", "probabilities": { "bass_swap": 0.71, "filter_sweep": 0.17, "echo_out": 0.09, "cut": 0.03 }, "confidence": 0.61 },
    "length": { "type": "choice", "choice": "long", "probabilities": { "short": 0.08, "medium": 0.34, "long": 0.58 }, "confidence": 0.37 },
    "big_moment": { "type": "noul", "noul": 0.18, "confidence": 0.64 }
  }
}
```

**Request B: cue points.** Given the technique and length, code enumerates phrase-aligned candidates:

- mix-out windows in the outgoing track, up to 5: outro start, end of the last chorus, into the break, latest possible;
- mix-in cues in the incoming track, up to 4: bar 1 of the intro, bassline entry, first drop or chorus.

Code first removes any combination that would put two vocals on top of each other for more than a couple of bars. The questions are `mix_out` (choice) and `mix_in` (choice), with each option described by its section card.

**Confidence gating.** If our computed confidence in `technique` falls below a threshold (tuned in Phase 4), use the safest feasible technique instead: echo-out for big jumps, bass swap for close matches. The fallback is logged.

**Decided in code for the MVP.** These are candidates to become model decisions in knob mode:

- The bass-swap bar: the phrase boundary nearest the middle of the overlap, or the incoming bassline's entry if it falls inside the window.
- EQ curve shapes.
- Echo settings: ½-beat delay, ~0.55 feedback.
- Filter sweep ranges.

### 6.4 Decision log

`mix.json` records the following for every transition:

- backend and model version; card and question-set versions;
- the shortlist, with each candidate's answers and combined score;
- the pick, its probability, and τ;
- the technique, length and cue answers, with probabilities;
- any fallback;
- the render parameters and timestamps.

With the same backend and random seed, a mix can be reproduced from its log.

## 7. Engine: rendering

- **Master timeline.** One beat grid for the whole mix, with a tempo curve. Each deck's own beats are mapped onto it with a Rubber Band time map (pyrubberband `timemap_stretch`). That keeps beats locked even when a track drifts, and it makes tempo ramps easy.
- **Tempo policy.** During a transition, the incoming track follows the outgoing tempo. Once the outgoing track is gone, the master tempo glides to the incoming track's native tempo over ~16 bars. Stretch is capped at ~6% by default.
- **Deck chain:** warped audio → isolator EQ → filter → FX sends → fader.
  - **Isolator EQ:** Linkwitz-Riley 4th-order crossovers (~250 Hz and ~3 kHz, tunable) split the signal into low / mid / high bands, with allpass compensation so the bands sum back flat at unity. Each band has its own gain curve, from 0 dB down to a full kill. "Lower the mids" is literally a gain curve on one band.
  - **Filter:** a one-knob DJ filter, resonant low-pass one way and high-pass the other (pedalboard `LadderFilter`, updated per block).
  - **Echo:** a tempo-synced delay (¼, ½ or 1 beat) with feedback. For an echo-out, the outgoing deck feeds the delay and its dry signal is cut, so the tail rings out.
  - **Reverb:** a send, for washes.
  - **Fader:** a gain curve, either equal-power, linear, or an instant cut.
- **Master bus.** Every track is loudness-normalised to the same integrated LUFS before mixing, and a limiter sits on the output.
- **Automation.** Every parameter is a curve on the master timeline, with breakpoints in beats. The renderer evaluates the curves per block (~512 samples). EQ band gains are applied per sample.
- **Recipes.** Each technique is a function `(plan, outgoing deck, incoming deck) → automation curves` (see §8).
- **Outputs.** `mix.wav`, `mix.cue` (track start times) and `mix.json` (see §6.4).

## 8. Transition techniques

In the table, T is the overlap length in bars.

| Technique | Automation | Good for |
|---|---|---|
| **Bass swap** (T = 16/32) | Incoming fades in over 1–2 bars with lows killed and mids at about −9 dB; its highs come up over the first quarter. On the swap bar the lows trade places on the downbeat: incoming to 0 dB, outgoing killed. Incoming mids return; outgoing mids and highs fall away and its fader is out by bar T. | Same-genre house/techno blends |
| **Filter sweep** (T = 8/16) | Outgoing high-pass rises from off to ~1–2 kHz, with a little resonance. Incoming starts low-passed (~300 Hz) and opens fully. Equal-power faders. Incoming lows stay killed until T/2, so two basslines never play at full together. | Softening a change of vibe |
| **Echo-out** (T = 1–4) | On the last beats of an outgoing phrase, send the outgoing deck to a ½-beat echo and cut its dry signal on the downbeat. The incoming track starts at full level on that downbeat. The echo tail can be high-passed. | Tempo, key or genre jumps |
| **Cut** (T = 0) | Switch on the downbeat at a phrase boundary, with a few milliseconds of fade to avoid clicks. | Drops, hip-hop, drum & bass |
| **Crossfade** (T = 4–16) | Equal-power fader crossfade, no EQ, no beatmatching needed. | Beatless material; low-confidence fallback |

**Later techniques:**

- double drop: two drops aligned, one bassline;
- loop-extend: loop the outgoing's last 4/8 bars to buy runway;
- stem swaps: the incoming vocal over the outgoing instrumental, or bass-stem swaps;
- tempo-ramp transitions for bigger BPM gaps;
- reverb wash;
- spinback / brake;
- shifting the incoming track ±1 semitone to fix a key clash.

## 9. Later modes: knob mode and live

### Knob mode

Instead of choosing a template, the model works the mixer itself. During a transition it decides once every 4 bars (about 7.5 s at 128 BPM):

- **State:** the mixer as buckets (each deck's low / mid / high as full, cut or killed; filter position; fader), what each deck plays in the next 4 bars, bars left in the outgoing track, and the set phase.
- **Question:** `move` (choice): bring in incoming highs · bring in incoming mids · swap bass · cut outgoing mids · cut outgoing highs · open incoming filter · sweep outgoing filter up · echo out now · hold.
- **Guards (code):**
  - never two lows at full;
  - the outgoing track must be gone before its audio ends;
  - moves land on beat boundaries, and every change is smoothed.

Knob mode is judged against the template version in blind A/B tests and trained on ratings. Jev was demoed in this kind of fast decision loop (games, simulations).

### Live mode

- **Just-in-time rendering.** While track N plays, disko decides and renders the N → N+1 transition, queues it, and streams the audio (`sounddevice`). That leaves minutes of headroom, so no real-time DSP engine is needed.
- **Live controls** (keyboard or a web page): more energy · less energy · skip · stay on this vibe · new vibe (free text). They update the set-plan state and take effect at the next decision point.
- **Knob mode** decisions can run live, every 4 bars.

## 10. Tech stack

| Area | Choice | Alternatives / notes |
|---|---|---|
| Language and env | Python 3.12, uv | |
| Beats, downbeats, sections | allin1 (all-in-one-mlx on Apple Silicon) | beat_this for beats only |
| Key | Essentia `KeyExtractor`, `edma` profile | libKeyFinder; madmom CNN key model |
| Stems | Demucs (htdemucs) | RoFormer models via audio-separator |
| Loudness, energy | pyloudnorm, librosa | |
| Vibe tags (later) | CLAP zero-shot | Essentia genre/mood models |
| Model client | TypeSafe Python SDK or plain httpx | The SDK is documented to work against a Kev server; Laya serves the same protocol |
| Model backends | Jev (hosted) · Kev-4B via `kev.serve` (MLX) · Laya via `laya-serve` | Kev-0.8B when speed matters |
| Time-stretch | Rubber Band via pyrubberband (`brew install rubberband`) | pedalboard `time_stretch` |
| DSP | numpy/scipy (EQ, curves) · pedalboard (`LadderFilter`, `Delay`, `Reverb`, `Limiter`) | |
| Audio I/O | soundfile, ffmpeg | |
| Storage | SQLite (cards, decisions, ratings) | |
| Playback (live) | sounddevice | |
| UI (review, live) | Small FastAPI app + one web page | Terminal UI |

## 11. Roadmap

Each phase ends with a check.

**Phase 0: spike (2–3 days).** De-risk both halves before building either.

- **0a, engine.**
  - Steps: two hand-picked house tracks → allin1 → warp the incoming onto the outgoing's grid → 16-bar bass swap with band EQ → WAV.
  - *Done when:* it sounds DJ-clean: no flamming kicks, the swap lands on the phrase, no level dip.
- **0b, brain.**
  - Steps: write ~10 transition situations and ~3 next-track situations as cards by hand. Ask Jev (if we have access), Kev-4B and `laya-typed-decisions`, and repeat each question 5 times with shuffled options.
  - *Done when:* we have a table of answers, shuffle agreement and latency per backend, plus a go/no-go on the card format.

**Phase 1: analysis.**

- `disko analyze <folder>` builds the cached cards.
- `disko inspect <track>` plays a track with clicks on the downbeats and prints its sections and card.
- *Done when:* spot checks on 30–50 tracks show correct downbeats and phrases for the large majority, and failures get flagged automatically.

**Phase 2: engine.**

- `disko render plan.json` turns a hand-written plan (tracks, techniques, cue bars) into `mix.wav`/`.cue`/`.json`.
- Includes the five techniques, the tempo policy, loudness normalisation and the limiter.
- *Done when:* a 3–4-track plan that uses every technique renders cleanly.

**Phase 3: brain.**

- Shortlist, candidate scoring, transition requests, gating and logging:
  `disko mix --start <track> --minutes 60 [--vibe "..."] [--backend jev|kev|laya] [--temperature 0.7]`.
- *Done when:* 60-minute mixes render unattended, and `mix.json` explains every decision.

**Phase 4: evaluation.**

- Baselines, metrics, review UI (`disko review`), next-track benchmark (§12).
- *Done when:* a scorecard compares backends against baselines, and we know whether the model beats the heuristic.

**Phase 5: learning and knob mode.**

- Learned weights, a fine-tuned local model, knob mode, more techniques.
- *Done when:* each change beats the Phase 4 scorecard.

**Phase 6: live.**

- Just-in-time rendering, streaming playback, live controls.
- *Done when:* a 60-minute live session plays with no gaps and reacts to controls at the next decision point.

## 12. Evaluation and learning

**Baselines**

- *Random-valid:* a random shortlisted track + a random feasible technique, length and cues.
- *Heuristic:* closest tempo + compatible key + energy-arc target, always with a 16-bar bass swap (echo-out when tempo or key is far).

**Automatic metrics per mix**

- key-clash transitions;
- bars with two vocals at once;
- bars with two full basslines;
- max/mean stretch %;
- phrase-alignment errors;
- loudness jumps between tracks (LU);
- energy-arc fit (target vs actual);
- artist and genre streaks;
- fallback rate.

**Listening tests** (in the review UI)

- Blind A/B of the same track pair mixed by two systems (isolates transition quality).
- Blind A/B of 20-minute excerpts (tests selection and flow).
- 1–5 ratings of single transitions. Every rating is stored as training data.

**Next-track benchmark.** Real DJ tracklists from Raveform (MixesDB mixes with tracklists) or the earlier djmix dataset (Kim et al., ISMIR 2020). Given the current track, rank the true next track among 9 tempo-matched decoys, and report hit@1 and MRR. Card features need the audio, so this is for personal research use only. A metadata-only variant tests the model's world knowledge instead.

**Backend comparison:** quality vs latency vs cost for Jev, Kev (0.8B / 4B / 9B) and Laya.

**Stability:** how often the top pick survives shuffled options and paraphrased instructions.

**Learning**

- **Labels:** our own ratings and A/B picks; labels distilled from Jev or a large LLM on generated situations; real DJ sequences for next-track choice.
- **Transition labels from real mixes:**
  - Raveform's beat-level mix-to-track alignments show where DJs brought each track in and out, and how long the overlap ran. That gives labels for `mix_in`, `mix_out` and `length`.
  - The same lab's fader/EQ estimation methods (NIME 2021, DAFx 2022) recover which EQ moves were used, which gives `technique` labels.
  - Both datasets ship links, not audio, so this is for personal research use.
- **Cheapest win first:** fit the composite-scoring weights with logistic regression on logged answers vs ratings.
- **Fine-tuning, Kev:** `python -m kev.train` from a released checkpoint takes about 15 min on an H100 for ~1k examples. Fit the temperature on held-out data. The `kev-finetune` agent skill automates the whole loop.
- **Fine-tuning, Laya:** the free Kaggle notebook.
- **Goal:** a small local model that matches or beats zero-shot Jev on our eval, so everything runs offline.

## 13. Risks

| Risk | Mitigation |
|---|---|
| The model can't hear, so card quality caps DJ quality | Version the card format, iterate it against evals, add vibe descriptors |
| Small open models are weak zero-shot | Start with Jev, Kev-4B or `laya-typed-decisions`; plan to fine-tune or distil |
| Numbers get misread | Bucket everything in code |
| Option-order bias | Score candidates one per request; shuffle and average `choice` questions |
| Beat/phrase errors (odd meters, live drummers, half/double tempo) | Start with 4/4 electronic; confidence flags; skip tracks that fail checks; manual override file |
| allin1 section labels are pop-style, not EDM-style | Validate on Raveform's annotated tracks; map the labels, or train a small EDM section model |
| Time-stretch artefacts | Cap stretch at ~6%; ramp tempo between tracks |
| Vocal clashes, muddy lows | Stem-based vocal and bass activity as hard constraints |
| Library access | Local files only (MP3/FLAC/AIFF/WAV). Streaming services don't expose audio, and Spotify's audio-features API is closed to new apps |
| Copyright | Rendered mixes are for personal use; public demos use CC-licensed music (FMA, MTG-Jamendo) |
| Young, fast-moving model ecosystem | Isolate behind the `/v1/systemone` client; record model versions in every `mix.json` |

## 14. Open decisions

| Decision | Default until decided |
|---|---|
| Offline mixes first, or live from day one? | Offline first; live in Phase 6 |
| Genre focus | 4/4 house/techno |
| Backend | Build to `/v1/systemone`; start with Jev (if we get access) or Kev-4B locally |
| Library | Local files; size and formats TBD |
| Project goal (personal tool, portfolio, research) | TBD; decides how rigorous Phase 4 gets |

## 15. References

**System One models**

- TypeSafe docs: [index](https://docs.typesafe.ai/llms.txt) · [API](https://docs.typesafe.ai/api.md) · [building with System One](https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md) · [state](https://docs.typesafe.ai/concepts/state.md) · [composite scoring](https://docs.typesafe.ai/patterns/composite-scoring.md) · [Jev known issues](https://docs.typesafe.ai/model-jaggedness/jev-1.13.md)
- [Kev](https://github.com/jaredpalmer/kev) · [Laya](https://github.com/NandhaKishorM/laya) · [systemonemodels.org](https://systemonemodels.org/)

**Audio tools**

- [allin1](https://github.com/mir-aidj/all-in-one) · [all-in-one-mlx](https://github.com/ssmall256/all-in-one-mlx) · [Essentia](https://essentia.upf.edu/) · [Demucs](https://github.com/facebookresearch/demucs) · [pyrubberband](https://github.com/bmcfee/pyrubberband) · [pedalboard](https://github.com/spotify/pedalboard)

**DJ mix datasets**

- [Raveform](https://mir-aidj.github.io/raveform/) (mir-aidj): real DJ mixes with tracklists, beat-level mix-to-track alignments, and 1,423 tracks annotated with EDM sections (intro / buildup / drop / breakdown / outro). Links only, no audio.
- [djmix dataset](https://github.com/mir-aidj/djmix-dataset) (`pip install djmix`): the lab's earlier release.

**Automatic DJ research**

- Kim et al., [A Computational Analysis of Real-World DJ Mixes using Mix-To-Track Subsequence Alignment](https://arxiv.org/abs/2008.10267) (ISMIR 2020)
- Kim, Yang & Nam, Reverse-Engineering the Transition Regions of Real-World DJ Mixes using Sub-band Analysis with Convex Optimization (NIME 2021)
- Kim, Yang & Nam, [Joint Estimation of Fader and Equalizer Gains of DJ Mixers using Convex Optimization](https://mac.kaist.ac.kr/pubs/KimYangNam-dafx2022.pdf) (DAFx 2022)
- Vande Veire & De Bie, [From raw audio to a seamless mix: creating an automated DJ system for Drum and Bass](https://imec-publications.be/handle/20.500.12860/32120) (EURASIP JASMP 2018) · [code](https://github.com/aida-ugent/dnb-autodj)
- Chen et al., [Automatic DJ Transitions with Differentiable Audio Effects and Generative Adversarial Networks](https://arxiv.org/abs/2110.06525) (ICASSP 2022)
- Zehren et al., [Automatic Detection of Cue Points for the Emulation of DJ Mixing](https://direct.mit.edu/comj/article-pdf/46/3/67/2164818/comj_a_00652.pdf) (Computer Music Journal)
- [Cue Point Estimation using Object Detection](https://arxiv.org/abs/2407.06823) (2024)
