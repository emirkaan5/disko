"""Analysis for tracks that have no file on this Mac (SPOTIFY_INTEGRATION_PLAN.md, Milestone 3).

A streamed or generated track is decoded through its source, written to this session's temp folder
only long enough for an analysis helper to read it, and deleted as soon as that analysis is in. What
the analysis found is kept like a playlist folder's (live/playlist.py), keyed by track id:

    ~/.disko/sources/<provider>/
        tracks.csv
        analysis/<key>.fast.json     the fast pass on the whole track
        analysis/<key>.allin1.json   beats, bars and sections (the deep pass)
        analysis/<key>.stems.json
        skipped.json                 tracks whose audio couldn't be analyzed

A track analyzed once isn't decoded for analysis again. That is only when the source's policy allows
PERSIST_ANALYSIS; otherwise the store lives inside the session temp folder and goes with it.

    SessionTemp   the per-session folder: made on first use, emptied when a source disconnects,
                  removed when the app exits
    SourceStore   saved analysis for one provider's tracks
    TempAudio     one track's audio in the temp folder, removed when closed
"""
import atexit
import hashlib
import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

from .. import playlist as pl
from ..library import Track
from . import Operation, open_track_audio, require

STORES = Path.home() / ".disko" / "sources"
SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# Reading a track's audio for analysis: decode it, analyze it, and hold it in the session temp folder
TEMP_ANALYSIS = (Operation.DECODE, Operation.ANALYZE, Operation.TEMP_AUDIO)
README = """disko keeps what it learned about {provider} tracks here, by track id, so a track is analyzed
only once. No audio is kept: each track is decoded into a temporary folder for its analysis and
deleted as soon as that is done. Delete this folder to have everything analyzed again.
"""


class SessionTemp:
    """This run's temp folder for decoded audio. Each file is deleted as soon as its analysis is
    done; whatever a source still has here goes when it disconnects, and the folder goes on exit."""

    def __init__(self, parent: str | None = None):
        self._parent, self._dir = parent, None
        self._lock = threading.Lock()
        self._files = {}          # provider -> set of paths still here
        self._watched = set()     # ids of sources whose disconnect clears their files

    @property
    def dir(self) -> Path:
        with self._lock:
            if self._dir is None:
                self._dir = Path(tempfile.mkdtemp(prefix="disko-session-", dir=self._parent))
                atexit.register(self.close)
            return self._dir

    def folder(self, name: str) -> Path:
        path = self.dir / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(self, track: Track, samples, sr: int) -> "TempAudio":
        """The track's samples as a WAV here, for an analysis helper to read."""
        import soundfile as sf
        require(track, *TEMP_ANALYSIS)
        audio = self.folder("audio")
        path = audio / f"{_safe(str(track.id))}.{threading.get_ident()}.wav"
        sf.write(path, samples, sr, subtype="FLOAT")
        with self._lock:
            self._files.setdefault(track.provider, set()).add(path)
            source = track.source
            if source is not None and id(source) not in self._watched and hasattr(source, "on_close"):
                self._watched.add(id(source))
                source.on_close(lambda p=track.provider: self.drop(p))
        return TempAudio(self, track.provider, path)

    def decode(self, track: Track) -> "TempAudio":
        """Open the track through its source and write it here (checked against its policy first)."""
        require(track, *TEMP_ANALYSIS)
        with open_track_audio(track) as handle:
            samples, sr = handle.read()
        return self.write(track, samples, sr)

    def remove(self, provider: str, path: Path):
        Path(path).unlink(missing_ok=True)
        with self._lock:
            self._files.get(provider, set()).discard(Path(path))

    def drop(self, provider: str):
        """A source disconnected: none of its audio stays."""
        with self._lock:
            paths = self._files.pop(provider, set())
        for path in paths:
            path.unlink(missing_ok=True)

    def files(self) -> list:
        with self._lock:
            return sorted(p for paths in self._files.values() for p in paths)

    def close(self):
        with self._lock:
            folder, self._dir = self._dir, None
            self._files.clear()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)


class TempAudio:
    """One track's audio in the session temp folder; deleted on close."""

    def __init__(self, temp: SessionTemp, provider: str, path: Path):
        self.temp, self.provider, self.path = temp, provider, path

    @property
    def bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def close(self):
        self.temp.remove(self.provider, self.path)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _safe(text: str) -> str:
    """A file name for a track key: the key itself when it's plain, else a hash of it."""
    return text if SAFE_KEY.match(text) else hashlib.sha1(text.encode()).hexdigest()[:20]


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class SourceStore:
    """Saved analysis for one provider's tracks: the same files a playlist folder keeps, without the
    folder. Duck-types the parts of Playlist the analysis helpers use (dir, analysis, key,
    add_structure, skip)."""

    def __init__(self, provider: str, root: Path, persistent: bool):
        self.provider, self.dir, self.persistent = provider, Path(root), persistent
        self.analysis = self.dir / "analysis"
        self.csv = self.dir / "tracks.csv"
        self.tracks = {}   # uid -> Track, for every track analyzed this session
        self.fast = {}     # uid -> fast-pass results
        self._lock = threading.Lock()

    @classmethod
    def for_source(cls, source, temp: SessionTemp, root: Path = STORES) -> "SourceStore":
        """On disk under ~/.disko/sources/<provider> if the source's policy lets analysis be kept,
        else inside the session temp folder."""
        if source.policy.allows(Operation.PERSIST_ANALYSIS):
            return cls(source.provider, Path(root) / source.provider, True)
        return cls(source.provider, temp.folder(f"analysis-{source.provider}"), False)

    def key(self, track: Track) -> str:
        return _safe(track.id.key)

    def prepare(self):
        self.analysis.mkdir(parents=True, exist_ok=True)
        if self.persistent and not (self.dir / "README.txt").exists():
            (self.dir / "README.txt").write_text(README.format(provider=self.provider))

    def _fast(self, track: Track):
        d = _json(self.analysis / f"{self.key(track)}.fast.json")
        return d if d and d.get("version") == pl.FAST_VERSION else None

    def skipped(self) -> dict:
        return _json(self.dir / "skipped.json") or {}

    def pending(self, tracks) -> list:
        """The tracks the fast pass hasn't seen, minus ones that couldn't be analyzed before."""
        skipped = self.skipped()
        return [t for t in tracks if self._fast(t) is None and str(t.id) not in skipped]

    def skip(self, failures: dict):
        """Remember tracks whose analysis failed ({track id: {"error"}}), so they aren't retried."""
        self.prepare()
        with self._lock:
            (self.dir / "skipped.json").write_text(json.dumps({**self.skipped(), **failures}, indent=1,
                                                              ensure_ascii=False))

    def apply(self, track: Track) -> bool:
        """Fill in what the analysis found for this track: every feature its source left unknown, the
        beat grid, and the structure once the deep pass has run. False if it hasn't been analyzed."""
        d = self._fast(track)
        if d is None:
            return False
        found = pl.fast_fields(d)
        for name, value in found.items():
            if name == "rough_beats" or (name == "duration" and not track.duration) or getattr(track, name) is None:
                setattr(track, name, value)
                if name != "rough_beats":
                    track.provenance[name] = f"fast pass v{pl.FAST_VERSION} on the full track"
        for cached in ("grid", "beat_period", "phrase_grid"):
            track.__dict__.pop(cached, None)
        with self._lock:
            self.tracks[track.uid], self.fast[track.uid] = track, d
        self._structure(track)
        return True

    def _structure(self, track: Track) -> bool:
        key = self.key(track)
        a, st = _json(self.analysis / f"{key}.allin1.json"), _json(self.analysis / f"{key}.stems.json")
        if a is None or st is None or len(a.get("downbeats", [])) < 2:
            return False
        bpm = self.fast[track.uid]["bpm"] if track.uid in self.fast else track.bpm
        track.set_structure(grid_file=self.analysis / f"{key}.allin1.json", **pl.structure(a, st, bpm))
        track.provenance.update(structure="allin1", vocals="stems")
        return True

    def add_structure(self, track: Track) -> bool:
        """The deep pass has just run on a track: fill in its structure and update tracks.csv."""
        if track.uid not in self.fast:
            self.apply(track)
        ok = self._structure(track)
        if ok:
            self.save()
        return ok

    def save(self):
        with self._lock:
            rows = [pl.csv_row(t, self.fast[uid], "") for uid, t in self.tracks.items()]
            self.prepare()
            pl.write_csv(self.csv, rows)
