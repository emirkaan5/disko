"""Kev as the DJ: it picks every next track and plans the mix into it.

Code keeps every choice small and valid (eight shortlisted tracks, only the techniques the tempos
allow, only bars where the mix still fits) and describes tracks in words rather than numbers, which
small decision models read far better (PLAN.md §3). Kev makes the calls. Each answer, with its
probabilities, goes out as an event so the booth can show the decision as it happens.

Tracks are shortlisted and rated on the fast features alone. Their structure (allin1's bars,
sections and stems) is only worked out once Kev has picked one, since the mix plan needs it.
"""
import queue
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np

from .audio import SR, Deck
from .kev import Kev
from .library import MAX_STRETCH, Library, Track, key_relation, tempo_match
from .mixes import TECHNIQUES, Plan, build, mix_in_points, mix_window, recommend

SHORTLIST = 8
TEMPERATURE = 0.012  # sampling the pick: Kev's scores sit close together, so its top track should usually win
UNSURE = 1.2        # Kev's technique yields to the rules only if its top pick is under 1.2x a coin flip

PICK_QUESTIONS = {
    "flow": {"type": "score", "instructions": "How naturally would the candidate follow the track now playing?",
             "criteria": ["jarring", "noticeable shift", "smooth", "perfect follow-up"]},
    "energy": {"type": "score", "instructions": "Does the candidate move the energy the way the DJ wants right now?",
               "criteria": ["wrong direction", "neutral", "right direction"]},
    "repeat": {"type": "noul", "instructions": "Would the candidate feel repetitive after the recent tracks?"},
}
TECHNIQUE_TEXT = {
    "bass_swap": "long EQ blend: both tracks play together and the basslines trade places on a downbeat; for similar tracks",
    "filter_sweep": "filter sweep: the old track is high-passed away while the new one opens up; smooths a change of vibe",
    "echo_out": "echo out: the old track is thrown into an echo and the new one drops in on the beat; works across any tempo or key",
    "drop_cut": "hard cut on the downbeat straight into the new track; for a big jump in energy",
    "smooth_fade": "plain crossfade without EQ tricks; gentle, for breakdowns and calm tracks",
}
LENGTH_TEXT = {
    "blend": {"short": "a quick change, about 8 bars", "medium": "about 16 bars", "long": "a long blend, about 32 bars"},
    "echo_out": {"short": "a short echo tail of about 2 bars", "medium": "an echo tail of about 4 bars",
                 "long": "a long echo tail of about 8 bars"},
}
LENGTH_BARS = {"short": 8, "medium": 16, "long": 32}
TAIL_BARS = {"short": 2, "medium": 4, "long": 8}
SETTING = {  # one extra knob per technique, asked together with the cue points
    "bass_swap": ("swap", {"type": "choice", "instructions": "When in the blend should the basslines swap?",
                           "criteria": {"early": "early, a quarter of the way in", "middle": "halfway through",
                                        "late": "late, three quarters of the way in"}}),
    "filter_sweep": ("depth", {"type": "choice", "instructions": "How far should the filter sweep go?",
                               "criteria": {"gentle": "a gentle sweep that leaves some of the old track",
                                            "deep": "a full sweep that removes the old track completely"}}),
    "echo_out": ("echo", {"type": "choice", "instructions": "Which echo should the old track get?",
                          "criteria": {"half": "a fast half-beat echo", "beat": "a slower one-beat echo"}}),
    "drop_cut": ("bass_kill", {"type": "noul",
                               "instructions": "Should the old track's bass drop out for the last two beats before the cut?"}),
    "smooth_fade": ("bass_swap", {"type": "noul", "instructions": "Should the basses swap halfway instead of overlapping?"}),
}
SECTION_WORDS = {"intro": "intro", "verse": "verse", "chorus": "main part", "bridge": "bridge",
                 "inst": "instrumental part", "solo": "lead part", "break": "breakdown", "outro": "outro"}
KEY_WORDS = {"same key": "same key", "next door": "compatible neighbours", "relative key": "relative keys",
             "energy boost": "an energy-boost key change", "energy dip": "slightly off keys",
             "diagonal": "loosely related keys", "key clash": "clashing keys"}
IN_WORDS = {"Track start": "from the very start", "Bass lands on the swap": "so its bass comes in right on the swap",
            "Second phrase": "from its second phrase", "Third phrase": "from its third phrase",
            "First drop": "straight from its first drop"}


def bars_words(n: int) -> str:
    return ("no bars" if n <= 0 else "a few bars" if n <= 4 else "about 8 bars" if n <= 12 else
            "about 16 bars" if n <= 24 else "about 32 bars" if n <= 48 else "a long stretch")


class Cards:
    """Tracks in plain words: buckets, not raw numbers."""

    def __init__(self, library: Library):
        self.lib = library

    def _energy(self, t: Track) -> str:
        e = self.lib.percentile(t, "energy")
        return "low" if e < 0.25 else "medium" if e < 0.6 else "high" if e < 0.85 else "very high"

    def vibe(self, t: Track) -> str:
        pct = lambda f: self.lib.percentile(t, f)
        mood = "dark" if pct("mood") < 0.3 else "bright" if pct("mood") > 0.7 else "neutral"
        vocals = "instrumental" if t.vocals < 0.05 else "a few vocal parts" if t.vocals < 0.25 else "vocal-led"
        groove = "sparse groove" if pct("busyness") < 0.3 else "busy groove" if pct("busyness") > 0.7 else "steady groove"
        tone = "dark sound" if pct("brightness") < 0.3 else "bright sound" if pct("brightness") > 0.7 else "balanced sound"
        return f"{self.genre(t)}; energy {self._energy(t)}, mood {mood}; {vocals}; {groove}; {tone}"

    def genre(self, t: Track) -> str:
        return ", ".join(t.genre_list[:2]) or "electronic"

    def short(self, t: Track) -> str:
        return f"{self.genre(t)} (energy {self._energy(t)})"

    def fit(self, master_bpm: float, a: Track, b: Track) -> str:
        dev = tempo_match(master_bpm, b.bpm)[1]
        tempo = ("exact match" if dev <= 0.005 else "close match" if dev <= 0.03 else
                 "stretchable" if dev <= MAX_STRETCH else "too far apart to blend")
        step = (b.energy - a.energy) / self.lib.spread("energy")
        energy = ("big drop" if step < -1 else "drop" if step < -0.35 else "hold" if step <= 0.35 else
                  "lift" if step <= 1 else "big lift")
        return f"tempo {tempo}; {KEY_WORDS[key_relation(a.camelot, b.camelot)[1]]}; energy {energy}"

    def at_bar(self, t: Track, bar: int) -> str:
        """What a track is doing at a bar: its section, bass and vocals."""
        if not t.deep:
            return "music"
        when = t.downbeats[min(max(bar, 1), t.n_bars) - 1]
        section = next((s.label for s in t.sections if s.start <= when < s.end), "")
        on = lambda bits: bar <= len(bits) and bits[bar - 1] == "1"
        return (f"{SECTION_WORDS.get(section, 'music')}; {'bass on' if on(t.bass_bars) else 'no bass'}, "
                f"{'vocals' if on(t.vocal_bars) else 'no vocals'}")

    def ending(self, t: Track) -> str:
        if not t.deep:
            return "how it ends is not known"
        parts = [f"an outro of {bars_words(t.outro_bars)}"] if t.outro_bars else ["no real outro"]
        if t.vocals < 0.05:
            parts.append("no vocals")
        else:
            parts.append("vocals until the very end" if t.vocals_out_bars < 4 else
                         f"no vocals in its last {bars_words(t.vocals_out_bars)}")
        tail = t.bass_bars[-8:]
        parts.append("bass to the end" if tail.count("1") >= 6 else "the bass already gone at the end")
        return "it ends with " + ", ".join(parts)

    def opening(self, t: Track) -> str:
        if not t.deep:
            return "how it opens is not known"
        parts = [f"{bars_words(t.bass_in_bars)} before its bass comes in"]
        parts.append("no vocals" if t.vocals < 0.05 else
                     "vocals from the start" if t.vocals_in_bars < 4 else f"vocals after {bars_words(t.vocals_in_bars)}")
        return "it opens with " + ", ".join(parts)

    def set_line(self, minutes: float, lift: int, recent: list) -> str:
        phase = ("warming up" if minutes < 8 else "building" if minutes < 25 else
                 "at the peak" if minutes < 60 else "deep into the night")
        want = {-1: "bring the energy down", 0: "keep the energy where it is", 1: "lift the energy"}[lift]
        before = ("Recent tracks: " + ", ".join(self.short(t) for t in recent[-3:]) + "."
                  if recent else "This is the first track of the set.")
        return f"Set: {phase}; the DJ wants to {want}.\n{before}"


@dataclass(eq=False)
class Candidate:
    """A shortlisted track and how Kev rated it."""
    track: Track
    fit: str
    flow: float | None = None    # 0-1
    energy: float | None = None  # 0-1
    repeat: float | None = None  # probability it would feel repetitive
    score: float | None = None
    chance: float | None = None  # probability of being picked
    ms: float = 0.0


class NothingLeft(Exception):
    """Every track in the library has been played."""


class KevDJ:
    def __init__(self, library: Library, kev: Kev, prepare, prepare_synced, structure=None):
        """structure(track) blocks until allin1 has mapped the track (raising if it can't); None
        when every track is mapped already."""
        self.lib, self.kev, self.cards = library, kev, Cards(library)
        self.prepare, self.prepare_synced = prepare, prepare_synced
        self.structure = structure or (lambda track: 0.0)
        self.broken = set()  # uids allin1 couldn't map: not picked again
        self.events = queue.Queue()
        self.lift = 0
        self._token = 0
        self._brain = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kev-dj")
        self._io = ThreadPoolExecutor(max_workers=8, thread_name_prefix="kev-io")

    def think(self, a: Deck, now, played: list, recent: list, minutes: float, avoid=()):
        """Decide the next track and the mix into it, in the background. `now()` gives deck A's position."""
        self._token += 1
        self._brain.submit(self._run, self._token, a, now, list(played), list(recent), minutes, set(avoid))

    def cancel(self):
        self._token += 1

    def _run(self, token, a, now, played, recent, minutes, avoid):
        emit = lambda kind, **data: token == self._token and self.events.put((kind, data))
        try:
            self._decide(token, emit, a, now, played, recent, minutes, avoid)
        except NothingLeft:
            emit("exhausted")
        except Exception as e:  # the set must go on: report it and let the window fall back
            emit("error", text=f"{type(e).__name__}: {e}")

    def _map(self, emit, t: Track) -> bool:
        """Have allin1 map a track's bars, sections and stems if it hasn't yet (10-25 s)."""
        if t.deep:
            return True
        emit("status", text=f"Mapping “{t.title}”: bars, sections, stems…", tone="info")
        start = time.time()
        try:
            self.structure(t)
        except Exception as e:
            emit("log", text=f"Couldn't map “{t.title}” ({e})", tone="warn")
            return False
        emit("mapped", track=t, seconds=time.time() - start)
        emit("log", text=f"Mapped “{t.title}” in {time.time() - start:.0f} s: {t.n_bars} bars, "
                         f"{len(t.sections)} sections")
        return True

    def _decide(self, token, emit, a, now, played, recent, minutes, avoid):
        if not a.track.deep:  # the opener: allin1 maps it while it plays; the plan needs its bars
            self._map(emit, a.track)
            a.reset_grid()
        if token != self._token:
            return
        if not self.kev.ping():
            emit("status", text="Waking Kev up (loading the model)…", tone="warn")
            self.kev.start()
        emit("model", name=self.kev.name)

        # 1. Shortlist in code, then Kev rates each candidate on its own (smaller states, no order bias).
        # Two tracks in a row by one artist are skipped (unless that leaves nothing; untagged files
        # all count as "Unknown artist", which isn't one).
        recent_artists = {t.artist for t in recent[-2:] if t.artist != "Unknown artist"}
        ranked = self.lib.rank(a.track, a.bpm, set(played) | avoid | self.broken, self.lift)
        if not ranked:
            raise NothingLeft
        fresh = [c for c in ranked if c.track.artist not in recent_artists] or ranked
        short = [Candidate(c.track, self.cards.fit(a.bpm, a.track, c.track)) for c in fresh[:SHORTLIST]]
        emit("shortlist", candidates=short)
        emit("status", text=f"Kev is weighing {len(short)} tracks…", tone="info")
        context = self.cards.set_line(minutes, self.lift, recent[:-1])
        playing = "Now playing: " + self.cards.vibe(a.track) + "."
        jobs = {self._io.submit(self._rate, context, playing, c): i for i, c in enumerate(short)}
        for done in as_completed(jobs):
            done.result()
            emit("rated", index=jobs[done])
        if token != self._token:
            return

        # 2. Pick: sample from Kev's scores, so the set doesn't always take the obvious track.
        scores = np.array([c.score for c in short])
        weights = np.exp((scores - scores.max()) / TEMPERATURE)
        for c, p in zip(short, weights / weights.sum()):
            c.chance = float(p)
        left = list(range(len(short)))
        while True:
            p = np.array([short[j].chance for j in left]) + 1e-12
            i = left[int(np.random.choice(len(left), p=p / p.sum()))]
            pick = short[i]
            b = pick.track
            emit("picked", index=i)
            emit("log", text=f"Picked “{b.title}” ({pick.chance:.0%} chance) — flow {pick.flow:.0%}, "
                             f"energy fit {pick.energy:.0%}")
            # Load the track (and time-stretch it to the set tempo) while allin1 maps it and Kev plans the mix.
            dev = tempo_match(a.bpm, b.bpm)[1]
            stretchable = dev <= MAX_STRETCH
            native = self._io.submit(self.prepare, b, 1.0)
            synced = self._io.submit(self.prepare_synced, b, a.period) if stretchable else None
            if self._map(emit, b):
                break
            if len(short) - len(left) >= 2 or len(left) == 1:  # a third failure: allin1 itself is in trouble
                emit("log", text=f"Mixing “{b.title}” on its rough beat grid instead", tone="warn")
                break
            self.broken.add(b.uid)  # allin1 couldn't read it: Kev's next choice
            left.remove(i)
            emit("log", text="Kev takes its next choice", tone="warn")
        if token != self._token:
            return
        stretchable = stretchable and tempo_match(a.bpm, b.bpm)[1] <= MAX_STRETCH  # allin1 may have refined its tempo

        # 3. Technique, asked under four option orders, then its length.
        emit("status", text="Kev is planning the mix…", tone="info")
        state = "\n".join([self.cards.set_line(minutes, self.lift, recent[:-1]),
                           f"Outgoing: {self.cards.vibe(a.track)}; {self.cards.ending(a.track)}.",
                           f"Incoming: {self.cards.vibe(b)}; {self.cards.opening(b)}.",
                           f"Fit: {pick.fit}."])
        options = {k: text for k, text in TECHNIQUE_TEXT.items() if stretchable or not TECHNIQUES[k].blend}
        probs, ms = self.kev.choose(state, "technique", {
            "type": "choice", "instructions": "How should the DJ move from the outgoing track into the incoming one?",
            "criteria": options})
        technique = max(probs, key=probs.get)
        overruled = None
        if probs[technique] < UNSURE / len(probs):
            rule = recommend(a.track, b, a.bpm).technique
            if rule in options and rule != technique:
                overruled, technique = technique, rule
        emit("technique", probs=probs, choice=technique, overruled=overruled, ms=ms)
        tech = TECHNIQUES[technique]
        params = tech.defaults()
        lengths = LENGTH_TEXT["echo_out"] if technique == "echo_out" else LENGTH_TEXT["blend"] if tech.blend else None
        if lengths and tech.blend and key_relation(a.track.camelot, b.camelot)[0] < 0.5:
            lengths = {k: v for k, v in lengths.items() if k != "long"}  # clashing keys: no long overlap
        if lengths:
            answer = self.kev.ask(state + f"\nPlan: {tech.name.lower()}.", {"length": {
                "type": "choice", "instructions": "How long should the transition take?", "criteria": lengths}})
            lp = answer["answers"]["length"]["probabilities"]
            length = max(lp, key=lp.get)
            if technique == "echo_out":
                params["tail"] = TAIL_BARS[length]
            else:
                allowed = [v for _, v in next(p for p in tech.params if p.key == "bars").options]
                params["bars"] = min(allowed, key=lambda v: abs(v - LENGTH_BARS[length]))
            emit("length", probs=lp, choice=length)

        # 4. Where to mix out and in, plus one setting of the technique.
        sync = tech.blend or stretchable
        auto = tech.build(params, a.bpm)
        first, last = mix_window(a, now(), technique, params, auto.b_start)
        outs = self._out_options(a.track, first, last)
        if not outs:  # too late in the track for this mix: echo out at the next bar instead
            technique, tech = "echo_out", TECHNIQUES["echo_out"]
            params, sync = tech.defaults(), stretchable
            auto = tech.build(params, a.bpm)
            outs = self._out_options(a.track, *mix_window(a, now(), technique, params, auto.b_start))
            emit("log", text="Too late in the track for that — echoing out at the next bar")
            if not outs:
                raise RuntimeError("the track is ending; no room left to mix")
        ins = [(f"in_{bar}", bar, label) for label, bar in mix_in_points(b, auto)]
        questions = {
            "mix_out": {"type": "choice", "instructions": "Where in the outgoing track should the mix happen?",
                        "criteria": {k: text for k, _, text in outs}},
            "mix_in": {"type": "choice", "instructions": "Where in the incoming track should it come in?",
                       "criteria": {k: f"{IN_WORDS.get(label, label)}: {self.cards.at_bar(b, bar)}"
                                    for k, bar, label in ins}}}
        setting_key, setting_q = SETTING[technique]
        questions[setting_key] = setting_q
        plan_line = f"\nPlan: {tech.name.lower()}" + (f" over {bars_words(params['bars'])}" if "bars" in params else "") + "."
        answers = self.kev.ask(state + plan_line, questions)["answers"]
        setting = self._apply_setting(technique, params, answers[setting_key])
        auto = tech.build(params, a.bpm)
        out_key, in_key = answers["mix_out"]["choice"], answers["mix_in"]["choice"]
        out_bar = dict((k, bar) for k, bar, _ in outs)[out_key]
        in_label, in_bar = next((label, bar) for k, bar, label in ins if k == in_key)
        if in_label == "Bass lands on the swap":  # the swap point may have moved with the setting
            in_bar = next((bar for label, bar in mix_in_points(b, auto) if label == in_label), in_bar)
        emit("cues", outs=[(k, bar, text) for k, bar, text in outs], out_probs=answers["mix_out"]["probabilities"],
             out_choice=out_key, ins=[(k, bar, IN_WORDS.get(label, label)) for k, bar, label in ins],
             in_probs=answers["mix_in"]["probabilities"], in_choice=in_key, setting=setting)

        # 5. Render it, ready to queue.
        deck, image = (synced if sync else native).result()
        deck.reset_grid()  # it may have loaded before allin1's grid came in
        plan = Plan(technique, params, sync, out_bar, in_bar)
        scheduled = build(plan, a, deck)
        if scheduled.start < now() + SR:  # the chosen bar went by while deciding: take the next one that fits
            first, last = mix_window(a, now(), technique, params, auto.b_start)
            if first > last:
                raise RuntimeError("the chosen bar went by and nothing later fits")
            plan.out_bar = first + (-(first - 1)) % 8 if first + (-(first - 1)) % 8 <= last else first
            scheduled = build(plan, a, deck)
        emit("ready", scheduled=scheduled, image=image)

    def _rate(self, context: str, playing: str, c: Candidate):
        state = "\n".join([context, playing, f"Candidate: {self.cards.vibe(c.track)}.", f"Fit: {c.fit}."])
        reply = self.kev.ask(state, PICK_QUESTIONS)
        ans = reply["answers"]
        c.flow = ans["flow"]["score"] / 3
        c.energy = ans["energy"]["score"] / 2
        c.repeat = ans["repeat"]["noul"]
        c.score = 0.5 * c.flow + 0.3 * c.energy - 0.2 * c.repeat
        c.ms = reply.get("latency_ms", 0.0)

    def _out_options(self, t: Track, first: int, last: int) -> list:
        """Bars in the back half of the track where the mix can land: phrase starts, the outro,
        the end of the vocals, the latest bar that still fits."""
        if first > last:
            return []
        floor = max(first, int(t.n_bars * 0.55))
        if floor > last:
            floor = first
        bars = {bar: "at a phrase start" for bar in range(1, last + 1, 8) if bar >= floor}
        if t.outro_bars and floor <= t.n_bars - t.outro_bars + 1 <= last:
            bars[t.n_bars - t.outro_bars + 1] = "where the outro starts"
        if t.vocals > 0.05 and t.vocals_out_bars and floor <= t.n_bars - t.vocals_out_bars + 1 <= last:
            bars[t.n_bars - t.vocals_out_bars + 1] = "right after the last vocals"
        bars[last] = "as late as the mix still fits"
        if len(bars) > 6:  # keep the special spots and the last few phrase starts
            special = {b: s for b, s in bars.items() if s != "at a phrase start"}
            phrases = sorted(b for b, s in bars.items() if s == "at a phrase start")
            for b in phrases[-(6 - len(special)):] if len(special) < 6 else []:
                special[b] = bars[b]
            bars = special
        return [(f"bar_{bar}", bar, f"{tag}: {self.cards.at_bar(t, bar)}") for bar, tag in sorted(bars.items())]

    @staticmethod
    def _apply_setting(technique: str, params: dict, answer: dict) -> str:
        if technique == "bass_swap":
            params["swap"] = {"early": 0.25, "middle": 0.5, "late": 0.75}[answer["choice"]]
            return f"swap {answer['choice']} ({answer['probabilities'][answer['choice']]:.0%})"
        if technique == "filter_sweep":
            params["depth"] = {"gentle": 0.7, "deep": 1.0}[answer["choice"]]
            return f"{answer['choice']} sweep ({answer['probabilities'][answer['choice']]:.0%})"
        if technique == "echo_out":
            params["time"] = {"half": 0.5, "beat": 1.0}[answer["choice"]]
            return f"{'half-beat' if answer['choice'] == 'half' else 'one-beat'} echo ({answer['probabilities'][answer['choice']]:.0%})"
        yes = answer["noul"] >= 0.5
        if technique == "drop_cut":
            params["bass_kill"] = 2 if yes else 0
            return f"{'kill' if yes else 'keep'} the bass before the cut ({answer['noul']:.0%} yes)"
        params["bass_swap"] = yes
        return f"{'swap' if yes else 'overlap'} the basses ({answer['noul']:.0%} yes)"
