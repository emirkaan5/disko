"""Analysis in the background: the fast pass over a folder's new files when it is opened (or over a
source's tracks, through the session temp folder), and allin1 on each track just before it plays.

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
from .sources import Operation, PolicyDenied, SourceError, allows, require
from .sources.base import TrackInfo
from .sources.store import TEMP_ANALYSIS, SessionTemp, SourceStore

# allin1 reads the audio and writes temporary stem WAVs along the way (both gone once it's done);
# keeping its results on disk also needs PERSIST_ANALYSIS (a store in the session temp folder doesn't)
DEEP_OPERATIONS = TEMP_ANALYSIS

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


class SourceIndexer:
    """The fast pass over a source's tracks that have no file: each one is decoded through its source,
    written to the session temp folder, analyzed, and deleted as soon as its result is in. Only a few
    are on disk at once. Results go to a SourceStore and into the Tracks themselves.

    Events as FastIndexer's: ("started", {total, workers}), ("done", {id, title, artist, bpm, key, ...}),
    ("failed", {id, rel, error}), ("finished", {}), ("fatal", {error})."""

    AHEAD = 2  # decoded tracks waiting beyond one per helper process

    def __init__(self, store: SourceStore, tracks: list, temp: SessionTemp, workers: int | None = None):
        self.store, self.temp, self.workers = store, temp, workers or max(2, min((os.cpu_count() or 4) - 4, 8))
        for t in tracks:
            store.apply(t)  # analyzed before: nothing to decode
        self.tracks = {store.key(t): t for t in tracks}
        self.todo = [t for t in store.pending(tracks) if all(allows(t, op) for op in TEMP_ANALYSIS)]
        self.events = queue.Queue()
        self.proc = None
        self._slots = threading.Semaphore(self.workers + self.AHEAD)
        self._audio = {}       # key -> TempAudio still on disk
        self._failures = {}    # track id -> {"error"}
        self._lock = threading.Lock()
        self._cancelled = False

    def start(self):
        if not self.todo:
            self.events.put(("finished", {}))
            return
        if not FAST_PYTHON.exists():
            self.events.put(("fatal", {"error": f"The analysis environment is missing ({FAST_PYTHON})."}))
            return
        self.store.prepare()
        cmd = [str(FAST_PYTHON), str(WORKERS / "fast.py"), "--jobs", "-", "--out", str(self.store.analysis),
               "--workers", str(min(self.workers, len(self.todo)))]
        with open(self.store.dir / "index.log", "a") as log:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
                                         bufsize=1, start_new_session=True)
        self.events.put(("started", {"total": len(self.todo), "workers": min(self.workers, len(self.todo))}))
        threading.Thread(target=self._feed, daemon=True, name="source-index-feed").start()
        threading.Thread(target=self._read, daemon=True, name="source-index").start()

    def _fail(self, track: Track, error: str, remember: bool = True):
        if remember:
            with self._lock:
                self._failures[str(track.id)] = {"error": error}
        self.events.put(("failed", {"id": self.store.key(track), "rel": str(track.id), "title": track.title,
                                    "error": error}))

    def _feed(self):
        try:
            for track in self.todo:
                self._slots.acquire()
                if self._cancelled:
                    break
                try:
                    audio = self.temp.decode(track)
                except PolicyDenied as e:  # may change with the policy: not remembered
                    self._fail(track, str(e), remember=False)
                    self._slots.release()
                    continue
                except (SourceError, OSError, ValueError) as e:
                    self._fail(track, str(e))
                    self._slots.release()
                    continue
                key = self.store.key(track)
                with self._lock:
                    self._audio[key] = audio
                job = {"id": key, "path": str(audio.path), "rel": str(track.id), "bytes": audio.bytes}
                self.proc.stdin.write(json.dumps(job) + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):  # the helper is gone; the reader reports it
            pass
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _release(self, key: str):
        with self._lock:
            audio = self._audio.pop(key, None)
        if audio is not None:
            audio.close()
            self._slots.release()

    def _read(self):
        finished = False
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.pop("event", "")
            if kind == "started":
                continue  # announced already, with the real total
            track = self.tracks.get(msg.get("id"))
            if kind in ("done", "failed"):
                self._release(msg.get("id"))
            if kind == "done" and track is not None:
                self.store.apply(track)
                msg.update(title=track.title, artist=track.artist)
            elif kind == "failed" and track is not None:
                with self._lock:
                    self._failures[str(track.id)] = {"error": msg.get("error")}
                msg["title"] = track.title
            elif kind == "finished":
                finished = True
                with self._lock:
                    failures = dict(self._failures)
                if failures:
                    self.store.skip(failures)
                if self.store.tracks:
                    self.store.save()
            elif kind == "fatal":
                finished = True
            self.events.put((kind, msg))
        self.proc.stdout.close()
        code = self.proc.wait()
        for key in list(self._audio):
            self._release(key)
        if not finished and not self._cancelled:
            self.events.put(("fatal", {"error": f"The analysis stopped (exit code {code}); "
                                                f"see {self.store.dir / 'index.log'}"}))

    def run(self, progress=None) -> dict:
        """Start and wait (no window): {"done": n, "failed": n}, or raise RuntimeError on a fatal error."""
        self.start()
        counts = {"done": 0, "failed": 0}
        while True:
            kind, info = self.events.get()
            if progress is not None:
                progress(kind, info)
            if kind in counts:
                counts[kind] += 1
            elif kind == "fatal":
                raise RuntimeError(info["error"])
            elif kind == "finished":
                return counts

    def cancel(self):
        self._cancelled = True
        _stop(self.proc)
        for _ in range(len(self.todo)):
            self._slots.release()


class PreviewIndexer:
    """The fast pass over a pool's previews (SPOTIFY_INTEGRATION_PLAN.md, Milestone 4).

    A preview is a 30 s file already on disk (live/sources/spotify/preview.py), so nothing is decoded
    and the session temp folder isn't used: the clip's path goes to the fast worker as it stands, and
    what comes back is kept as that track's preview analysis and put in the track as estimates. A
    track that has one already is filled in from the store without being sent again.

    run() blocks: it is called while the pool is being built, before the set starts, and reports each
    track through `progress(kind, info)`."""

    def __init__(self, store: SourceStore, tracks: list, previews, workers: int | None = None):
        self.store, self.previews = store, previews
        self.workers = workers or max(2, min((os.cpu_count() or 4) - 4, 8))
        self.tracks = {store.key(t): t for t in tracks}
        self.proc, self.scratch = None, None
        self.todo = self._todo(tracks)   # [(track, the clip's path, the provider that supplied it)]
        self._cancelled = False

    def _todo(self, tracks) -> list:
        """The clips that still have to be analyzed; ones analyzed before go into their tracks now."""
        todo = []
        for track in tracks:
            saved = self.store.preview(track)
            if saved is not None:
                self.store.apply_preview(track, saved, self._provider(track))
                continue
            found = self.previews.cached(self._info(track))
            if found is not None:
                todo.append((track, found.path, found.provider))
        return todo

    def run(self, progress=None) -> dict:
        """Analyze the clips in `todo` and wait for them: {"done": n, "failed": n}, or raise
        RuntimeError when the analysis can't run at all."""
        counts = {"done": 0, "failed": 0}
        if progress is not None:  # what this pool holds and how much of it still needs a clip read
            progress("start", {"event": "start", "tracks": len(self.tracks), "todo": len(self.todo)})
        if not self.todo:
            return counts
        if not FAST_PYTHON.exists():
            raise RuntimeError(f"The analysis environment is missing ({FAST_PYTHON}).")
        self.store.prepare()
        self.scratch = self.store.dir / "previews"
        self.scratch.mkdir(parents=True, exist_ok=True)
        cmd = [str(FAST_PYTHON), str(WORKERS / "fast.py"), "--jobs", "-", "--out", str(self.scratch),
               "--workers", str(min(self.workers, len(self.todo)))]
        with open(self.store.dir / "index.log", "a") as log:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True,
                                         bufsize=1, start_new_session=True)
        threading.Thread(target=self._feed, daemon=True, name="preview-index-feed").start()
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            kind = msg.pop("event", "")
            if kind in ("done", "failed"):
                kept = self._finish(msg, kind)
                if kind == "done" and not kept:
                    kind = msg["event"] = "failed"
                counts[kind] += 1
                if progress is not None:
                    progress(kind, msg)
            elif kind == "fatal":
                raise RuntimeError(msg.get("error", "the analysis stopped"))
            elif kind == "finished":
                break
        self.proc.stdout.close()
        code = self.proc.wait()
        if not self._cancelled and not any(counts.values()) and code:
            raise RuntimeError(f"The analysis stopped (exit code {code}); see {self.store.dir / 'index.log'}")
        return counts

    def _feed(self):
        try:
            for track, path, _ in self.todo:
                size = Path(path).stat().st_size if Path(path).exists() else 0
                job = {"id": self.store.key(track), "path": str(path), "rel": str(track.id), "bytes": size}
                self.proc.stdin.write(json.dumps(job) + "\n")
                self.proc.stdin.flush()
        except (OSError, ValueError):  # the helper is gone; the reader reports what happened
            pass
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _finish(self, msg: dict, kind: str) -> bool:
        """Take in what the fast pass reported for one clip; False when a 'done' came with no results
        (the track then counts as failed, and stays without a preview analysis)."""
        track = self.tracks.get(msg.get("id"))
        if track is not None:
            msg.update(title=track.title, artist=track.artist, rel=str(track.id))
        if kind == "failed" or track is None:
            return True
        try:
            results = json.loads((self.scratch / f"{self.store.key(track)}.fast.json").read_text())
        except (OSError, ValueError):
            msg["error"] = "the fast pass wrote nothing for the preview"
            return False
        self.store.apply_preview(track, results, self._provider(track))
        return True

    def _provider(self, track: Track) -> str:
        found = self.previews.cached(self._info(track))
        return found.provider if found is not None else "missing"

    @staticmethod
    def _info(track: Track) -> TrackInfo:
        return TrackInfo(track.id, track.title, artist=track.artist, duration=track.duration or None)

    def cancel(self):
        self._cancelled = True
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


class FullPass:
    """The fast pass over one whole track, run just before it is analyzed deeply (Milestone 5, step 2).

    A track whose features came from a 30 s preview is measured again on its own audio as soon as it
    is picked, so the values the mix uses are the track's own and the beat grid covers all of it.
    The audio is already in the session temp folder (the deep pass decoded it there), so this is one
    job to the fast worker, waited for: it runs on the analyzer's background thread, never the UI's.

    The helper stays up for the set: its imports and models cost a few seconds, and a track picked
    minutes later must not pay them again."""

    TIMEOUT = 120.0  # one track's fast pass; seconds on 12 cores, so this is a hang, not slowness

    def __init__(self, store: SourceStore, workers: int = 1):
        self.store, self.workers = store, workers
        self.proc, self.log = None, None
        self._lines = queue.Queue()
        self._reader = None
        self._lock = threading.Lock()

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if not FAST_PYTHON.exists():
            raise RuntimeError(f"the analysis environment is missing ({FAST_PYTHON})")
        self.store.prepare()
        self.log = open(self.store.dir / "index.log", "a")
        self._lines = queue.Queue()
        self.proc = subprocess.Popen([str(FAST_PYTHON), str(WORKERS / "fast.py"), "--jobs", "-",
                                      "--out", str(self.store.analysis), "--workers", str(self.workers)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True,
                                     bufsize=1, start_new_session=True)
        self._reader = threading.Thread(target=self._read, args=(self.proc, self._lines), daemon=True,
                                        name="full-pass")
        self._reader.start()

    def run(self, track: Track, path) -> bool:
        """Analyze the track's own audio and replace the estimates a preview produced. Blocks; False
        when the helper couldn't be asked or gave nothing back (the estimates then stand)."""
        if not self.store.preview_only(track):
            return False   # nothing to replace: the source's own values, or the full pass ran already
        with self._lock:
            try:
                self.start()
                key = self.store.key(track)
                self.proc.stdin.write(json.dumps({"id": key, "path": str(path), "rel": str(track.id),
                                                  "bytes": Path(path).stat().st_size}) + "\n")
                self.proc.stdin.flush()
            except (OSError, RuntimeError, ValueError):
                self.proc = None
                return False
            if not self._wait(key):
                return False
        return self.store.apply(track)

    def _wait(self, key: str) -> bool:
        deadline = time.monotonic() + self.TIMEOUT
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                _stop(self.proc)   # the next pick starts a fresh helper
                return False
            try:
                message = self._lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if message is None or message.get("id") == key:
                return bool(message) and message.get("event") == "done"

    def _read(self, proc, lines: queue.Queue):
        for line in proc.stdout:
            try:
                lines.put(json.loads(line))
            except ValueError:
                continue
        lines.put(None)

    def close(self):
        _stop(self.proc)
        self.proc = None
        if self.log is not None:
            self.log.close()
            self.log = None


class DeepAnalyzer:
    """allin1 on one track at a time, in a helper that stays up (its imports take a few seconds).

    `store` is a Playlist (local files, read where they are) or a SourceStore (a source's tracks,
    decoded into the session temp folder `temp` first and deleted once mapped).

    analyze(track) returns a Future that resolves, with the seconds it took, once the track's
    structure is filled in and tracks.csv updated; asking again for a track in progress returns
    the same Future. Every step also goes out on `events` as (kind, track, info), kind being
    analyzing, analyzed or failed."""

    TIMEOUT = 240  # seconds before an analysis that hangs is given up (and the helper restarted)

    def __init__(self, store, temp: SessionTemp | None = None):
        self.store, self.temp = store, temp
        self.full = FullPass(store) if isinstance(store, SourceStore) and temp is not None else None
        self.events = queue.Queue()
        self.proc = None
        self._lock = threading.Lock()
        self._futures = {}  # uid -> Future, while it is being analyzed
        self._tracks = {}   # uid -> Track
        self._keys = {}     # the id the helper knows it by -> uid
        self._audio = {}    # uid -> TempAudio, for a track decoded into the session temp folder
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
        self.store.analysis.mkdir(parents=True, exist_ok=True)
        with open(self.store.dir / "analyze.log", "a") as log:
            self.proc = subprocess.Popen([str(DEEP_PYTHON), str(WORKERS / "deep.py")], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=log, text=True, bufsize=1,
                                         start_new_session=True)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="deep-read").start()

    def _check(self, track: Track) -> bool:
        """Raise PolicyDenied unless this track may be mapped; True if it's a local file read in place."""
        require(track, *DEEP_OPERATIONS)
        if track.provider == "local" and track.path is not None:
            require(track, Operation.PERSIST_ANALYSIS)
            return True
        if self.temp is None:
            raise PolicyDenied(track.provider, Operation.TEMP_AUDIO, "no session temp folder to decode it into",
                               track.id)
        if getattr(self.store, "persistent", True):
            require(track, Operation.PERSIST_ANALYSIS)
        return False

    def analyze(self, track: Track) -> Future:
        with self._lock:
            if track.deep:
                done = Future()
                done.set_result(0.0)
                return done
            if track.uid in self._futures:
                return self._futures[track.uid]
            future = Future()
            try:  # asked before anything is decoded or sent to the helper: a denied track is never read or written
                in_place = self._check(track)
            except PolicyDenied as e:
                future.set_exception(e)
                return future
            self._futures[track.uid], self._tracks[track.uid] = future, track
        if in_place:
            self._send(track, track.path)
        else:  # decoding a stream takes a while: never on the caller's (often the UI) thread
            threading.Thread(target=self._decode_and_send, args=(track,), daemon=True, name="deep-decode").start()
        return future

    def _decode_and_send(self, track: Track):
        try:
            audio = self.temp.decode(track)
        except Exception as e:
            self._finish(track.uid, error=str(e))
            return
        with self._lock:
            self._audio[track.uid] = audio
        if self.full is not None:
            # Milestone 5, step 2: the track itself replaces the estimates a 30 s preview produced,
            # so the grid the mix uses covers the whole track. One track, just before it plays.
            self.full.run(track, audio.path)
        self._send(track, audio.path, tmp=self.temp.folder("stems"))

    def _send(self, track: Track, path, tmp=None):
        key = self.store.key(track)
        request = {"id": key, "path": str(path), "out": str(self.store.analysis), **({"tmp": str(tmp)} if tmp else {})}
        try:
            with self._lock:
                self._spawn()
                self._keys[key] = track.uid
                self._owner[track.uid] = self.proc
                self.proc.stdin.write(json.dumps(request) + "\n")
                self.proc.stdin.flush()
        except (OSError, RuntimeError) as e:
            self._finish(track.uid, error=str(e))

    def _finish(self, uid: str, seconds: float | None = None, error: str | None = None):
        with self._lock:
            future, track = self._futures.pop(uid, None), self._tracks.pop(uid, None)
            audio = self._audio.pop(uid, None)
            self._started.pop(uid, None)
            self._owner.pop(uid, None)
            for key in [k for k, u in self._keys.items() if u == uid]:
                del self._keys[key]
        if audio is not None:
            audio.close()  # the decoded audio goes as soon as the helper is done with it
        if future is None:
            return
        if error is None and not self.store.add_structure(track):
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
            uid = self._keys.get(msg.get("id"))
            if msg.get("event") == "started" and uid in self._tracks:
                self._started[uid] = time.time()
                self.events.put(("analyzing", self._tracks[uid], {}))
            elif msg.get("event") == "done" and uid is not None:
                self._finish(uid, seconds=msg.get("seconds"))
            elif msg.get("event") == "failed" and uid is not None:
                self._finish(uid, error=msg.get("error", "allin1 failed"))
        proc.stdout.close()
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
        if self.full is not None:
            self.full.close()
        proc = self.proc
        if proc is not None:
            try:
                proc.stdin.close()  # the helper exits when its input ends...
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                _stop(proc)        # ...or is stopped if it's mid-track
        with self._lock:
            leftover, self._audio = list(self._audio.values()), {}
        for audio in leftover:
            audio.close()
