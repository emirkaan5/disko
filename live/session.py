"""A set in progress, without any UI: what is playing, what is queued, and Kev deciding what follows.

The window (live/ui/window.py) is a view over a Session; `python -m live --headless` runs one with
no window at all; the control server (live/control.py) drives either.

Thread ownership is explicit: a Session belongs to the thread that made it (the UI thread in the
app, the main thread headless). Only that thread calls its methods and `tick()`, which it does
every frame or so. Any other thread goes through `call()`: the command is queued, run on the
owner's next tick, and its result comes back on a Future. So state changes are serialized, and
the audio callback only ever copies samples.

What happens goes out to listeners (`on(fn)`) as (kind, data) events:

    loading, opened, open_failed, next, thinking, dj (Kev's own events), queued, missed,
    mix_dropped, mix_cancelled, promoted, ended, stopped, source_lost, analysis, job_failed
"""
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

from . import mixes
from .audio import DECK_OPERATIONS, SR, Deck
from .dj import KevDJ
from .library import Library, Track
from .mixes import Scheduled
from .sources import AuthExpired, Operation, PolicyDenied, policy_for, require

LIFTS = {-1: "cool down", 0: "hold", 1: "lift"}
RETRY_S = 3.0  # after Kev hits a problem, it tries again this much later


class SessionError(Exception):
    """A command that can't be done in the set's current state; the message says why."""


class WrongThread(RuntimeError):
    """A Session method was called from a thread that doesn't own it (use `call()`)."""


def track_info(t: Track) -> dict:
    return {"id": str(t.id), "title": t.title, "artist": t.artist,
            "bpm": None if t.bpm is None else round(t.bpm, 1), "key": t.camelot}


class Session:
    COMMANDS = ("status", "start", "pause", "resume", "stop", "energy", "pick_again", "tracks")

    def __init__(self, library: Library, engine, kev, prepare, prepare_synced, pool=None, analyzer=None):
        """prepare(track, ratio) and prepare_synced(track, master_period) return (Deck, extra);
        the window's extra is the painted waveform, headless it is None."""
        self.owner = threading.get_ident()
        self.library, self.engine, self.kev, self.analyzer = library, engine, kev, analyzer
        self.prepare = prepare
        self.pool = pool or ThreadPoolExecutor(max_workers=3, thread_name_prefix="session")
        self.dj = KevDJ(library, kev, prepare, prepare_synced, structure=self.structure)
        self.autopilot = True        # Kev DJs; off = the listener picks and plans every mix
        self.extras = {}             # uid -> what prepare() returned besides the deck
        self.current: Deck | None = None
        self.played = []             # uids, in order
        self.mixes = []              # the set's transitions so far, as Plans
        self.state = "start"         # start, playing, planning, queued, mixing, ended, stopped
        self.scheduled: Scheduled | None = None
        self.set_seconds = 0.0
        self.token = 0               # bumps when the listener changes their mind; stale jobs are dropped
        self.notice = None           # (text, tone): the latest thing worth telling the listener
        self._jobs = []
        self._calls = queue.Queue()
        self._listeners = []
        self._retry_at = None
        self._last = time.monotonic()

    # ------------------------------------------------------------ plumbing

    def _owned(self):
        if threading.get_ident() != self.owner:
            raise WrongThread("Session methods run on the thread that owns it; use call() from elsewhere")

    def on(self, listener):
        self._listeners.append(listener)

    def _emit(self, event: str, **data):
        for fn in self._listeners:
            fn(event, data)

    def _tell(self, text: str, tone: str = "info"):
        self.notice = (text, tone)

    def submit(self, fn, *args, then, failed=None):
        """Run fn in the background; `then(result)` runs on the owner thread unless the token moved on."""
        self._owned()
        self._jobs.append((self.pool.submit(fn, *args), then, failed, self.token))

    def bump(self):
        self.token += 1

    def _poll_jobs(self):
        for job in [j for j in self._jobs if j[0].done()]:
            self._jobs.remove(job)
            future, then, failed, token = job
            if token != self.token:
                continue
            try:
                result = future.result()
            except Exception as e:  # a broken file or a failed render shouldn't take the set down
                if isinstance(e, AuthExpired):
                    self._source_lost(str(e))
                (failed or (lambda err: self._emit("job_failed", error=str(err))))(e)
                continue
            then(result)

    def call(self, command: str, *args) -> Future:
        """Thread-safe: queue a command (one of COMMANDS) for the owner's next tick."""
        future = Future()
        if command not in self.COMMANDS:
            future.set_exception(SessionError(f"unknown command {command!r}"))
        else:
            self._calls.put((command, args, future))
        return future

    def _run_calls(self):
        while True:
            try:
                command, args, future = self._calls.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(getattr(self, f"cmd_{command}")(*args))
            except Exception as e:
                future.set_exception(e)

    def structure(self, track: Track) -> float:
        """Block until allin1 has mapped the track (background threads only)."""
        if track.deep or self.analyzer is None:
            return 0.0
        return self.analyzer.analyze(track).result()

    # ------------------------------------------------------------ the set

    def open(self, track: Track):
        """Start the set (or start it again) on this track."""
        self._owned()
        if self.state not in ("start", "ended", "stopped"):
            raise SessionError("a set is already playing; stop it first")
        if not track.mixable:
            raise SessionError(f"“{track.title}” can't be played ({track.availability.value}"
                               + (f", unknown {' and '.join(track.missing)}" if track.missing else "") + ")")
        if not getattr(track.source, "connected", True):
            raise SessionError(f"{track.source.label or track.provider} is disconnected; connect it again first")
        require(track, *DECK_OPERATIONS)  # refused here, before anything is read
        self.bump()
        if self.analyzer is not None and not track.deep:  # map it while it plays; Kev's plan waits for it
            self.analyzer.analyze(track)
        self._emit("loading", track=track)
        self.submit(self.prepare, track, 1.0, then=self._opened,
                    failed=lambda e: self._emit("open_failed", track=track, error=str(e)))

    def _opened(self, result):
        deck, extra = result
        self.extras[deck.track.uid] = extra
        self.current = deck
        self.engine.load(deck)
        self.engine.play()
        self.played.append(deck.track.uid)
        self.state = "playing"
        self._emit("opened", deck=deck, extra=extra)
        self._next_step()

    def _next_step(self):
        """After a track starts: Kev decides what follows, or the view waits for the listener."""
        self._emit("next", autopilot=self.autopilot)
        if self.autopilot:
            self.kev_think()

    def kev_think(self, avoid=()):
        self._owned()
        if self.current is None or self.scheduled is not None or self.state == "stopped":
            return
        self._retry_at = None
        self._emit("thinking")
        recent = [self.library.by_uid[u] for u in self.played]
        self.dj.think(self.current, lambda: self.engine.snapshot().pos, self.played, recent,
                      self.set_seconds / 60, avoid, self.mixes)

    def set_autopilot(self, on: bool):
        self._owned()
        self.autopilot = on
        if self.current is None:
            return
        if on:
            if self.scheduled is None and self.state in ("playing", "planning"):
                self.state = "playing"
                self._next_step()
        else:
            self.dj.cancel()  # a mix Kev already queued still plays; only its next decisions stop
            self._emit("autopilot", on=False)

    def repick(self) -> bool:
        """Ask Kev for another pick, dropping a queued mix that hasn't started."""
        self._owned()
        if not self.autopilot or self.current is None or self.state in ("mixing", "stopped"):
            return False
        avoid = set()
        if self.scheduled is not None:
            if not self.engine.cancel():
                return False
            avoid.add(self.scheduled.deck.track.uid)
            self.scheduled = None
            self.state = "playing"
            self._emit("mix_dropped", reason="repick")
        self._emit("repick")
        self.kev_think(avoid)
        return True

    def set_lift(self, lift: int):
        self._owned()
        if lift not in LIFTS:
            raise SessionError("energy is -1 (cool down), 0 (hold) or 1 (lift)")
        self.dj.lift = lift
        if self.autopilot and self.scheduled is None and self.current is not None and self.state != "stopped":
            self.kev_think()  # rethink with the new direction

    def play(self, on: bool):
        self._owned()
        if self.state == "stopped" and on and self.current is not None:  # resume a stopped set
            if self.engine.pos >= self.current.length - SR // 5:
                raise SessionError("the track is over; start a new one")
            self.state = "playing"
            self.engine.play()
            self._next_step()
            return
        self.engine.play(on)

    def toggle(self):
        self.play(not self.engine.playing)

    def stop(self, reason: str = "stopped by the listener"):
        """End the set: playback stops, Kev stops deciding, a queued mix is dropped."""
        self._owned()
        self.bump()
        self.dj.cancel()
        self._retry_at = None
        if self.scheduled is not None and self.engine.cancel():
            self.scheduled = None
        self.engine.play(False)
        if self.state == "mixing":  # stopped mid-transition: the buffer stays, but nothing plays on
            self.scheduled = None
            self.engine.load(self.current, self.engine.snapshot().pos)
        self.state = "stopped" if self.current is not None else "start"
        self._tell(f"Set stopped: {reason}", "warn")
        self._emit("stopped", reason=reason)

    def seek(self, frac: float) -> bool:
        self._owned()
        if self.current is None or self.state == "mixing":
            return False
        target = int(frac * self.current.length)
        if self.engine.seek(target):
            return True
        if self.scheduled is not None and self.engine.cancel():  # jumped past the queued mix: plan a new one
            self.scheduled = None
            self.state = "playing"
            self.engine.seek(target)
            self._emit("mix_dropped", reason="seek")
            if self.autopilot:
                self.kev_think()
            return True
        return False

    def skip_to_mix(self):
        """Jump to a few bars before the queued transition, to hear it now."""
        self._owned()
        s = self.scheduled
        if s is not None and self.state == "queued":
            self.engine.seek(max(int(s.start - 8 * s.spb), self.engine.snapshot().pos))

    def begin_planning(self):
        self._owned()
        self.bump()
        self.state = "planning"

    def end_planning(self):
        self._owned()
        self.bump()
        if self.state == "planning":
            self.state = "playing"

    def schedule(self, s: Scheduled) -> bool:
        """Queue a rendered transition (Kev's, or one the listener planned)."""
        self._owned()
        if self.state == "stopped":
            return False
        if self.state == "ended":  # the track ran out before the mix was ready: just start the next one
            self.engine.load(s.deck, max(s.b_offset + int(s.auto.b_start * s.spb), 0))
            self.engine.play()
            self.scheduled = s
            self._promote()
            return True
        if not self.engine.schedule(s):
            self._emit("missed", autopilot=self.autopilot)
            if self.autopilot:
                self.kev_think()
            return False
        self.scheduled = s
        self.state = "queued"
        mixes.log(s.report)
        self._emit("queued", scheduled=s)
        return True

    def cancel_mix(self) -> bool:
        self._owned()
        if self.scheduled is None or not self.engine.cancel():
            return False
        self.scheduled = None
        self.state = "planning"
        self._emit("mix_cancelled")
        return True

    def _promote(self):
        """The transition finished: deck B is now the one playing."""
        s = self.scheduled
        self.scheduled = None
        self.current = s.deck
        self.played.append(s.deck.track.uid)
        self.mixes.append(s.plan)
        self.state = "playing"
        self._emit("promoted", scheduled=s)
        self._next_step()

    def _source_lost(self, reason: str, provider: str | None = None):
        """A source's access is gone (sign-in expired, disconnected). Whatever came from it stops:
        nothing already decoded is assumed to stay authorized."""
        decks = [d for d in (self.current, self.scheduled.deck if self.scheduled else None) if d is not None]
        if provider is not None and not any(d.track.provider == provider for d in decks):
            return
        self._emit("source_lost", reason=reason)
        if self.state not in ("start", "stopped"):
            self.stop(f"access to the music was lost ({reason})")

    def disconnect(self, source):
        """Sign out of a source: its audio handles close and anything playing from it stops."""
        self._owned()
        source.close()
        self._source_lost(f"{source.label or source.provider} disconnected", source.provider)

    # ------------------------------------------------------------ the loop

    def tick(self):
        """Run on the owner thread every frame or so; returns the engine snapshot it worked from."""
        self._owned()
        self._run_calls()
        self._poll_jobs()
        self._drain_dj()
        self._drain_analyzer()
        now = time.monotonic()
        dt, self._last = now - self._last, now
        if self._retry_at is not None and now >= self._retry_at:
            self._retry_at = None
            if self.scheduled is None:
                self.kev_think()
        snap = self.engine.snapshot()
        if snap.playing:
            self.set_seconds += dt
        s = self.scheduled
        if s is not None:
            if snap.sched is s:
                self.state = "mixing"
            elif snap.sched is None and snap.deck is s.deck:
                self._promote()
                snap = self.engine.snapshot()
                s = None
        if (self.current is not None and s is None and not snap.playing and self.state in ("playing", "planning")
                and snap.pos >= self.current.length - SR // 5):
            self.state = "ended"
            self.bump()
            self._emit("ended", autopilot=self.autopilot)
        return snap

    def _drain_dj(self):
        while True:
            try:
                kind, data = self.dj.events.get_nowait()
            except queue.Empty:
                return
            self._emit("dj", kind=kind, data=data)
            if kind == "ready" and self.autopilot:
                s = data["scheduled"]
                self.extras[s.deck.track.uid] = data["image"]
                self.schedule(s)
            elif kind == "exhausted":
                self._tell("That's every track in the playlist — this one closes the set", "warn")
            elif kind == "skipped":  # Kev's pick turned out unplayable: it's off the list, pick again now
                self._tell(data["text"], "warn")
                if self.autopilot:
                    self._retry_at = time.monotonic()
            elif kind == "blocked":
                self._tell(f"Kev can't continue: {data['text']}", "bad")
            elif kind == "source_lost":
                self._source_lost(data["text"], data.get("provider"))
            elif kind == "error" and self.autopilot:
                self._tell(f"Kev hit a problem ({data['text']}) — trying again", "bad")
                self._retry_at = time.monotonic() + RETRY_S

    def _drain_analyzer(self):
        """allin1 finished a track: if it's playing, switch its deck to the real bars and sections."""
        while self.analyzer is not None:
            try:
                kind, track, info = self.analyzer.events.get_nowait()
            except queue.Empty:
                return
            playing = self.current is not None and self.current.track is track
            if kind == "analyzed" and playing:
                self.current.reset_grid()
            self._emit("analysis", kind=kind, track=track, playing=playing)

    def close(self):
        self.dj.cancel()

    # ------------------------------------------------------------ status and remote commands

    def status(self) -> dict:
        """What is true right now, from the set's own state (nothing is inferred or explained)."""
        snap = self.engine.snapshot()
        out = {"state": self.state, "playing": snap.playing, "autopilot": self.autopilot,
               "energy": LIFTS[self.dj.lift], "set_minutes": round(self.set_seconds / 60, 2),
               "tracks_played": len(self.played),
               "library": {"name": self.library.name, "tracks": len(self.library.tracks),
                           "unplayable": len(self.library.unrankable)},
               "sources": self.sources(),
               "playback": {"where": "the sound output of the Mac running disko", "device": self.engine.output_name},
               "now": None, "next": None,
               "notice": None if self.notice is None else {"text": self.notice[0], "tone": self.notice[1]}}
        if self.current is not None:
            deck = snap.deck or self.current
            out["now"] = {**track_info(deck.track), "position_s": round(snap.pos / SR, 1),
                          "remaining_s": round((deck.length - snap.pos) / SR, 1), "set_bpm": round(deck.bpm, 1)}
        s = self.scheduled
        if s is not None:
            out["next"] = {**track_info(s.deck.track), "technique": s.plan.tech.name, "out_bar": s.plan.out_bar,
                           "starts_in_s": max(0.0, round((s.start - snap.pos) / SR, 1)) if snap.sched is None else 0.0,
                           "mixing": snap.sched is s}
        return out

    def sources(self) -> list:
        """Where this library's tracks come from and what disko is permitted to do with each."""
        out = {}
        for t in self.library.tracks:
            if t.provider not in out:
                policy = policy_for(t)
                label = getattr(t.source, "label", "") or ("This Mac" if t.provider == "local" else t.provider)
                out[t.provider] = {"provider": t.provider, "label": label,
                                   "permitted": [op.name.lower() for op in Operation if policy.allows(op)]}
        return list(out.values())

    def cmd_status(self) -> dict:
        return self.status()

    def _opener(self, query: str | None) -> Track:
        mixable = [t for t in self.library.tracks if t.mixable]
        if query:
            q = query.lower()
            hits = [t for t in mixable if str(t.id) == query or t.uid == query or q in t.title.lower()]
            if not hits:
                raise SessionError(f"no playable track matches {query!r}")
            return hits[0]
        if not mixable:
            raise SessionError("nothing in this library can be played")
        known = [t for t in mixable if t.energy is not None]  # sets usually open low and build
        return min(known, key=lambda t: t.energy) if known else mixable[0]

    def cmd_start(self, query: str | None = None) -> dict:
        if not self.autopilot:
            raise SessionError("the listener is DJing by hand; switch the window to Kev first")
        track = self._opener(query)
        try:
            self.open(track)
        except PolicyDenied as e:
            raise SessionError(str(e)) from None
        return {"starting": track_info(track), "sources": self.sources(),
                "playback": {"where": "the sound output of the Mac running disko", "device": self.engine.output_name}}

    def cmd_pause(self) -> dict:
        if self.current is None:
            raise SessionError("nothing is playing")
        self.play(False)
        return self.status()

    def cmd_resume(self) -> dict:
        if self.current is None:
            raise SessionError("nothing to resume; start a set")
        self.play(True)
        return self.status()

    def cmd_stop(self) -> dict:
        if self.state in ("start", "stopped"):
            return self.status()
        self.stop()
        return self.status()

    def cmd_energy(self, lift) -> dict:
        names = {"down": -1, "cool": -1, "cool down": -1, "hold": 0, "up": 1, "lift": 1}
        value = names.get(lift.lower()) if isinstance(lift, str) else lift
        if isinstance(value, bool) or value not in LIFTS:
            raise SessionError("energy is 'down', 'hold' or 'up' (or -1, 0, 1)")
        self.set_lift(value)
        return self.status()

    def cmd_pick_again(self) -> dict:
        if not self.repick():
            raise SessionError("Kev can't pick again right now (no set playing, mid-transition, or DJing by hand)")
        return self.status()

    def cmd_tracks(self, query: str = "", limit: int = 20) -> dict:
        q = (query or "").lower()
        limit = max(1, min(int(limit), 50))
        hits = [t for t in self.library.tracks if t.mixable and (not q or q in f"{t.title} {t.artist}".lower())]
        return {"total": len(hits), "tracks": [track_info(t) for t in hits[:limit]]}
