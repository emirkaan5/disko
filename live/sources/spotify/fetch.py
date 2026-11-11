"""The fetch helper as a process: one Spotify track at a time, into the session temp folder
(SPOTIFY_INTEGRATION_PLAN.md, Milestone 5).

SpotifySource reads its audio through this. The work happens in a helper of its own
(live/workers/spotify_fetch.py, run in the fetcher's virtual environment where librespot and its
protobuf pin live), so a crash or a hang there can't take the set down, and it is asked for one track
at a time, only when that track is about to play: the plan's main protection against the account
being flagged.

    fetcher = Fetcher(temp.folder("fetch"))
    source = spotify.connect(fetcher=fetcher)

The WAV the helper writes lands in `folder` (the app's session temp folder, which removes it on exit)
and this class keeps only the last couple of them: a track's samples are held by its deck, and a mix
needs the track playing and the one coming in. Anything older is deleted as the next fetch arrives.
"""
import json
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path

from ..base import AuthExpired, SourceError, SourceTimeout, TrackUnavailable

REPO = Path(__file__).resolve().parents[3]
HELPER = REPO / "live" / "workers" / "spotify_fetch.py"
FETCH_PYTHON = Path(os.environ.get("DISKO_SPOTIFY_FETCH_PYTHON", REPO / ".venv-spotify" / "bin" / "python"))
TIMEOUT_S = 300.0    # a full track, fetched and decoded; past it the helper is treated as stuck
KEEP = 2             # the track in the mix and the one coming into it
KINDS = {"auth": AuthExpired, "unavailable": TrackUnavailable, "timeout": SourceTimeout}


def _safe(key: str) -> str:
    """A file name for a track key, by the same rule live/sources/store.py uses for its own files."""
    import hashlib
    import re
    return key if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key) else hashlib.sha1(key.encode()).hexdigest()[:20]


class Fetcher:
    """The helper as a process, and the audio it has fetched for this session."""

    def __init__(self, folder, python=FETCH_PYTHON, helper=HELPER, timeout: float = TIMEOUT_S, backend=None,
                 credentials=None, keep: int = KEEP, environ: dict | None = None):
        self.folder, self.python, self.helper = Path(folder), Path(python), Path(helper)
        self.timeout, self.backend, self.keep = timeout, backend, max(1, keep)
        self.environ = environ
        self.credentials = credentials
        self.log = self.folder / "fetch.log"
        self._lock = threading.Lock()
        self._proc = None
        self._reader = None
        self._lines = queue.Queue()
        self._stderr = None
        self._files = {}       # track id -> the WAV kept for it, oldest first
        self._closed = False

    # ------------------------------------------------------------ the helper

    def _spawn(self):
        if self._proc is not None and self._proc.poll() is None:
            return
        if not self.python.exists():
            raise AuthExpired("Spotify audio isn't set up yet: open the Spotify page and finish setting it up "
                              "(it installs the audio helper and signs you in for audio)")
        self.folder.mkdir(parents=True, exist_ok=True)
        command = [str(self.python), str(self.helper)]
        if self.backend:
            command += ["--backend", str(self.backend)]
        if self.credentials:
            command += ["--credentials", str(self.credentials)]
        if self._stderr is not None:  # a restart: the previous log handle goes
            self._stderr.close()
        self._stderr = open(self.log, "a")
        self._lines = queue.Queue()
        self._proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
                                      text=True, bufsize=1, start_new_session=True, env=self.environ)
        self._reader = threading.Thread(target=self._read, args=(self._proc, self._lines), daemon=True,
                                        name="spotify-fetch")
        self._reader.start()

    def _read(self, proc, lines: queue.Queue):
        for line in proc.stdout:
            try:
                lines.put(json.loads(line))
            except ValueError:
                continue  # a library that printed to our pipe: the protocol says nothing about it
        lines.put(None)  # the helper is gone

    def _stop(self):
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()          # the helper exits when its input ends...
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:                        # ...or is stopped if it is mid-track
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

    # ------------------------------------------------------------ fetching

    def fetch(self, track_id) -> Path:
        """The WAV for one track, fetched now if this session doesn't have it (blocking, one at a time)."""
        with self._lock:
            return self._fetch(track_id)

    def _fetch(self, track_id) -> Path:
        if self._closed:
            raise SourceError("the Spotify fetcher is closed")
        key = str(track_id)
        here = self._files.get(key)
        if here is not None and Path(here).exists():
            self._files[key] = self._files.pop(key)  # it stays the most recent
            return Path(here)
        self._spawn()
        dest = self.folder / f"{_safe(track_id.key)}.fetch.wav"
        request = {"id": key, "track": track_id.key, "path": str(dest)}
        try:
            self._proc.stdin.write(json.dumps(request) + "\n")
            self._proc.stdin.flush()
        except OSError as e:
            self._proc = None
            raise SourceError(f"the Spotify fetch helper is gone ({e}); see {self.log}") from None
        message = self._answer(key)
        if message.get("event") != "done":
            raise KINDS.get(message.get("kind"), SourceError)(message.get("error") or "the Spotify fetch failed")
        self._keep(key, Path(message.get("path") or dest))
        return self._files[key]

    def _answer(self, key: str) -> dict:
        """Wait for the helper's answer about this track, giving up if it stops answering."""
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self._stop()  # a helper that stopped answering is started again by the next fetch
                raise SourceTimeout(f"fetching from Spotify took longer than {self.timeout:.0f}s")
            try:
                message = self._lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if message is None:
                self._proc = None
                raise SourceError(f"the Spotify fetch helper stopped; see {self.log}")
            if message.get("event") == "fatal":
                self._proc = None  # nothing to ask it again: no sign-in for the fetcher, or no librespot
                raise AuthExpired(message.get("error") or "the Spotify fetcher isn't set up")
            if message.get("id") == key and message.get("event") in ("done", "failed"):
                return message

    def _keep(self, key: str, path: Path):
        self._files.pop(key, None)
        self._files[key] = Path(path)
        while len(self._files) > self.keep:
            old, old_path = next(iter(self._files.items()))
            if old == key:
                break
            del self._files[old]
            Path(old_path).unlink(missing_ok=True)

    def files(self) -> list:
        """The WAVs still here, oldest first (tests check that nothing is left)."""
        with self._lock:
            return [path for path in self._files.values() if Path(path).exists()]

    def drop(self, key: str | None = None):
        """Forget the audio kept for one track, or for all of them; the files go with it."""
        with self._lock:
            paths = list(self._files.values()) if key is None else [self._files.pop(key, None)]
            if key is None:
                self._files.clear()
        for path in paths:
            if path:
                Path(path).unlink(missing_ok=True)

    def close(self):
        """The app is done with Spotify audio: the helper goes and nothing it fetched stays."""
        self.drop()
        self._stop()
        if self._stderr is not None:
            self._stderr.close()
            self._stderr = None
        self._closed = True
