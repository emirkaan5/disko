"""Shared test pieces: a deterministic stand-in for Kev and a session on the synthetic catalog."""
import tempfile
import time
from pathlib import Path

from live.audio import SR, load_deck, sync_ratio
from live.engine import Engine
from live.library import Library
from live.session import Session
from live.sources.fake import FakeSource


class StubKev:
    """Answers Kev's questions the same way every time, with no model: the first option of every
    choice, top marks on every score, 'no' on every yes/no."""
    name = "stub"
    info = {"name": "stub"}

    def __init__(self):
        self.asked = 0

    def ping(self):
        return True

    def start(self):
        return True

    def stop(self):
        pass

    def _answer(self, q):
        if q["type"] == "score":
            n = len(q["criteria"])
            return {"score": n - 1, "probabilities": {}}
        if q["type"] == "noul":
            return {"noul": 0.1}
        options = list(q["criteria"])
        return {"choice": options[0], "probabilities": {k: (1.0 if i == 0 else 0.0) for i, k in enumerate(options)}}

    def ask(self, state, questions):
        self.asked += 1
        return {"answers": {k: self._answer(q) for k, q in questions.items()}, "latency_ms": 1.0}

    def choose(self, state, qid, question, orders=4):
        self.asked += 1
        options = list(question["criteria"])
        return {k: (0.9 if i == 0 else 0.1 / max(len(options) - 1, 1)) for i, k in enumerate(options)}, 1.0


def prepare(track, ratio=1.0):
    return load_deck(track, ratio), None


def prepare_synced(track, period):
    return prepare(track, sync_ratio(track, period))


def fake_session(n=5, seconds=90.0, **source_args):
    src = FakeSource(n=n, seconds=seconds, **source_args)
    lib = Library(src.library_tracks(), name="fake")
    engine = Engine(audio=False)
    kev = StubKev()
    session = Session(lib, engine, kev, prepare, prepare_synced)
    events = []
    session.on(lambda kind, data: events.append((kind, data)))
    return session, engine, src, events


def run_until(session, engine, done, timeout=60.0, step=SR // 20):
    """Tick the session (playing audio along) until done() or the timeout; returns done()."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        session.tick()
        if done():
            return True
        if engine.playing:
            engine.advance(step)
        time.sleep(0.005)
    return done()


def isolate_spotify_consent(case_module_cleanup) -> Path:
    """Point the default Spotify audio choice (~/.disko/spotify/consent.json) at an empty temp file for
    the whole test module, so no test reads the listener's real choice or clears it (logout does).
    Call from setUpModule with unittest.addModuleCleanup."""
    from live.sources.spotify.consent import ConsentStore
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "spotify" / "consent.json"
    defaults = ConsentStore.__init__.__defaults__
    ConsentStore.__init__.__defaults__ = (path,)
    case_module_cleanup(lambda: setattr(ConsentStore.__init__, "__defaults__", defaults))
    case_module_cleanup(tmp.cleanup)
    return path
