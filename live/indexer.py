"""Analysis in the background: the fast pass over a folder's new files when it is opened, and
allin1 on each track just before it plays.

Both run as helper processes in their own Python environments (Essentia's TensorFlow build and
allin1's MLX stack don't install next to the app's packages) and report back in JSON lines. The
app never waits on them on the UI thread: progress and results arrive on `events` queues.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
from concurrent.futures import Future
from pathlib import Path

from .library import Track
from .playlist import Playlist

REPO = Path(__file__).resolve().parent.parent
WORKERS = REPO / "live" / "workers"
FAST_PYTHON = Path(os.environ.get("DISKO_FAST_PYTHON", REPO / ".venv-essentia-tf" / "bin" / "python"))
DEEP_PYTHON = Path(os.environ.get("DISKO_DEEP_PYTHON", REPO / ".venv-audio" / "bin" / "python"))


def _stop(proc: subprocess.Popen | None):
    if proc is not None and proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


class FastIndexer:
    """The fast pass over every file a playlist hasn't seen yet, about half a second per track on
    12 cores. Events: ("started", {total, workers}), ("done", {id, title, artist, bpm, key, ...}),
    ("failed", {id, rel, error}), ("finished", {}), or ("fatal", {error}) if it can't go on."""

    def __init__(self, playlist: Playlist, workers: int | None = None):
        self.playlist, self.workers = playlist, workers
        self.todo = playlist.pending()
        self.events = queue.Queue()
        self.proc = None
        self._cancelled = False

    def start(self):
        pl = self.playlist
        pl.prepare([f.id for f in self.todo])
        if not self.todo:
            self.events.put(("finished", {}))
            return
        if not FAST_PYTHON.exists():
            self.events.put(("fatal", {"error": f"The analysis environment is missing ({FAST_PYTHON})."}))
            return
        jobs = pl.dir / "jobs.json"
        jobs.write_text(json.dumps([{"id": f.id, "path": str(f.path), "rel": f.rel, "bytes": f.bytes} for f in self.todo]))
        cmd = [str(FAST_PYTHON), str(WORKERS / "fast.py"), "--jobs", str(jobs), "--out", str(pl.analysis)]
        if self.workers:
            cmd += ["--workers", str(self.workers)]
        with open(pl.dir / "index.log", "a") as log:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log, text=True, start_new_session=True)
        threading.Thread(target=self._read, daemon=True, name="fast-index").start()

    def _read(self):
        finished, failures = False, {}
        sizes = {f.rel: f.bytes for f in self.todo}
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.pop("event", "")
            if kind == "failed":
                failures[msg["rel"]] = {"bytes": sizes.get(msg["rel"]), "error": msg["error"]}
            elif kind == "finished" and failures:
                self.playlist.skip(failures)
            finished |= kind in ("finished", "fatal")
            self.events.put((kind, msg))
        code = self.proc.wait()
        (self.playlist.dir / "jobs.json").unlink(missing_ok=True)
        if not finished and not self._cancelled:
            self.events.put(("fatal", {"error": f"The analysis stopped (exit code {code}); "
                                                f"see {self.playlist.dir / 'index.log'}"}))

    def cancel(self):
        self._cancelled = True
        _stop(self.proc)


class DeepAnalyzer:
    """allin1 on one track at a time, in a helper that stays up (its imports take a few seconds).

    analyze(track) returns a Future that resolves, with the seconds it took, once the track's
    structure is filled in and tracks.csv updated; asking again for a track in progress returns
    the same Future. Every step also goes out on `events` as (kind, track, info), kind being
    analyzing, analyzed or failed."""

    TIMEOUT = 240  # seconds before an analysis that hangs is given up (and the helper restarted)

    def __init__(self, playlist: Playlist):
        self.playlist = playlist
        self.events = queue.Queue()
        self.proc = None
        self._lock = threading.Lock()
        self._futures = {}  # uid -> Future, while it is being analyzed
        self._tracks = {}   # uid -> Track
        self._owner = {}    # uid -> the helper process it was sent to
        self._started = {}  # uid -> time.time() when the helper took it on
        self._closed = False
        threading.Thread(target=self._watch, daemon=True, name="deep-watchdog").start()

    def start(self):
        """Launch the helper ahead of time, so its imports are done by the first request."""
        with self._lock:
            self._spawn()

    def _spawn(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if not DEEP_PYTHON.exists():
            raise RuntimeError(f"allin1's environment is missing ({DEEP_PYTHON})")
        with open(self.playlist.dir / "analyze.log", "a") as log:
            self.proc = subprocess.Popen([str(DEEP_PYTHON), str(WORKERS / "deep.py")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
                                         start_new_session=True)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="deep-read").start()

    def analyze(self, track: Track) -> Future:
        with self._lock:
            if track.deep:
                done = Future()
                done.set_result(0.0)
                return done
            if track.uid in self._futures:
                return self._futures[track.uid]
            future = Future()
            try:
                self._spawn()
                self.proc.stdin.write(json.dumps({"id": track.uid, "path": str(track.path),
                                                  "out": str(self.playlist.analysis)}) + "\n")
                self.proc.stdin.flush()
            except (OSError, RuntimeError) as e:
                future.set_exception(e)
                return future
            self._futures[track.uid], self._tracks[track.uid], self._owner[track.uid] = future, track, self.proc
            return future

    def _finish(self, uid: str, seconds: float | None = None, error: str | None = None):
        with self._lock:
            future, track = self._futures.pop(uid, None), self._tracks.pop(uid, None)
            self._started.pop(uid, None)
            self._owner.pop(uid, None)
        if future is None:
            return
        if error is None and not self.playlist.add_structure(track):
            error = "allin1 found no bars in it"
        if error is None:
            future.set_result(seconds)
            self.events.put(("analyzed", track, {"seconds": seconds}))
        else:
            future.set_exception(RuntimeError(error))
            self.events.put(("failed", track, {"error": error}))

    def _read(self, proc: subprocess.Popen):
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            uid = msg.get("id")
            if msg.get("event") == "started" and uid in self._tracks:
                self._started[uid] = time.time()
                self.events.put(("analyzing", self._tracks[uid], {}))
            elif msg.get("event") == "done":
                self._finish(uid, seconds=msg.get("seconds"))
            elif msg.get("event") == "failed":
                self._finish(uid, error=msg.get("error", "allin1 failed"))
        proc.wait()
        with self._lock:  # the helper is gone: whatever it still had fails (asking again restarts it)
            waiting = [uid for uid, owner in self._owner.items() if owner is proc]
        for uid in waiting:
            self._finish(uid, error="the analysis helper stopped" if not self._closed else "the app is closing")

    def _watch(self):
        while not self._closed:
            time.sleep(5)
            stuck = [uid for uid, t in list(self._started.items()) if time.time() - t > self.TIMEOUT]
            if stuck:
                _stop(self.proc)  # the reader then fails everything that was waiting

    def close(self):
        self._closed = True
        proc = self.proc
        if proc is None:
            return
        try:
            proc.stdin.close()  # the helper exits when its input ends...
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            _stop(proc)        # ...or is stopped if it's mid-track
