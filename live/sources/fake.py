"""A synthetic catalog for tests and demos: deterministic tracks, made-up names, generated audio.

No real music, accounts, endpoints or branding. What it may do is set by its policy, and any
track can be told to fail in a given way, so the app's handling of a streaming source (missing
metadata, unavailable tracks, expired access, timeouts, decode errors) can be checked without one.

    src = FakeSource(n=12, missing={"fake:t03": ("energy", "mood")}, failures={"fake:t05": "region"})
    tracks = src.library_tracks()
"""
import numpy as np

from .base import (AudioHandle, AuthExpired, Availability, Operation, Page, PlaylistRef, Source,
                   SourceError, SourceTimeout, TrackId, TrackInfo, TrackUnavailable)
from .policy import Policy

SR = 44100
KEYS = ("8A", "9A", "8B", "7A", "10A", "5A", "11B", "6A", "12A", "3B")

# failure name -> (availability it reports, exception raised on opening its audio)
FAILURES = {
    "unavailable": (Availability.UNAVAILABLE, TrackUnavailable("the track was removed from the catalog")),
    "region": (Availability.REGION_RESTRICTED, TrackUnavailable("not available in your region")),
    "entitlement": (Availability.ENTITLEMENT_REQUIRED, TrackUnavailable("needs a higher subscription tier")),
    "timeout": (Availability.AVAILABLE, SourceTimeout("the catalog didn't answer in time")),
    "expired": (Availability.AVAILABLE, AuthExpired("the sign-in expired")),
    "decode": (Availability.AVAILABLE, None),  # opens, then fails to read
}


def fake_policy(allow=None, analysis_on_disk: bool = False, handle_ttl: float | None = None) -> Policy:
    """A policy for the fake source; by default everything but recording, nothing kept on disk."""
    allowed = frozenset(Operation) - {Operation.RECORD} if allow is None else frozenset(allow)
    return Policy("fake", allowed, agreement={"reference": "synthetic test source: no third-party music"},
                  analysis_on_disk=analysis_on_disk, handle_ttl=handle_ttl)


def click_track(bpm: float, seconds: float, seed: int = 0) -> np.ndarray:
    """(n, 2) float32: a kick on every beat over a quiet chord, so beat trackers have something to find."""
    n = int(seconds * SR)
    t = np.arange(n) / SR
    rng = np.random.default_rng(seed)
    root = 110 * 2 ** (rng.integers(0, 12) / 12)
    x = 0.08 * (np.sin(2 * np.pi * root * t) + 0.6 * np.sin(2 * np.pi * root * 1.5 * t))
    kick_len = int(0.12 * SR)
    k = np.arange(kick_len) / SR
    kick = 0.7 * np.sin(2 * np.pi * (50 + 80 * np.exp(-k * 30)) * k) * np.exp(-k * 25)
    for start in (np.arange(0, seconds, 60.0 / bpm) * SR).astype(int):
        end = min(start + kick_len, n)
        x[start:end] += kick[:end - start]
    return np.ascontiguousarray(np.stack([x, x], axis=1), dtype=np.float32)


class FakeSource(Source):
    provider = "fake"
    label = "Synthetic test catalog"

    def __init__(self, n: int = 8, seed: int = 0, policy: Policy | None = None, missing=None, failures=None,
                 seconds: float = 24.0, playlist_size: int | None = None):
        super().__init__(policy or fake_policy())
        rng = np.random.default_rng(seed)
        self.seconds = seconds
        self.missing = {TrackId.parse(k): tuple(v) for k, v in (missing or {}).items()}
        self.failures = {TrackId.parse(k): v for k, v in (failures or {}).items()}
        self.reads = []  # track ids whose audio was actually generated, in order: tests check denials leave none
        self._infos = {}
        for i in range(n):
            tid = TrackId(self.provider, f"t{i:02d}")
            bpm = float(np.round(rng.uniform(120, 126), 2))
            features = {"bpm": bpm, "camelot": KEYS[i % len(KEYS)], "key_strength": float(rng.uniform(0.5, 0.9)),
                        "energy": float(rng.uniform(3, 7)), "mood": float(rng.uniform(3, 7)),
                        "danceability": float(rng.uniform(1, 2)), "loudness": -9.0,
                        "busyness": float(rng.uniform(2, 5)), "brightness": float(rng.uniform(1500, 3500)),
                        "vocals": float(rng.uniform(0, 0.5))}
            for name in self.missing.get(tid, ()):
                features.pop(name, None)
            availability = FAILURES[self.failures[tid]][0] if tid in self.failures else Availability.AVAILABLE
            self._infos[tid] = TrackInfo(tid, f"Test Tone {i + 1}", f"Generator {chr(65 + i % 26)}", seconds,
                                         availability, ("Synthetic House",), features,
                                         {k: "fake source" for k in features})
        self.ids = list(self._infos)
        self.playlist_size = playlist_size or n

    def _playlists(self) -> list:
        return [PlaylistRef("all", "All test tones", len(self.ids))]

    def _tracks(self, playlist_id: str, offset: int, limit: int) -> Page:
        if playlist_id != "all":
            raise SourceError(f"no playlist {playlist_id!r}")
        ids = self.ids[:self.playlist_size]
        return Page(tuple(self._infos[i] for i in ids[offset:offset + limit]), offset, len(ids))

    def _info(self, track_id: TrackId) -> TrackInfo:
        if track_id not in self._infos:
            raise SourceError(f"no track {track_id}")
        return self._infos[track_id]

    def _open_audio(self, track_id: TrackId) -> AudioHandle:
        info = self._info(track_id)
        failure = self.failures.get(track_id)
        if failure and FAILURES[failure][1] is not None:
            raise FAILURES[failure][1]
        return _FakeAudio(self, info, failure == "decode")

    def library_tracks(self) -> list:
        """A Track for every catalog entry, with exactly the features the catalog supplies."""
        from .pool import track_from_info
        return [track_from_info(self._infos[tid], self) for tid in self.ids]


class _FakeAudio(AudioHandle):
    def __init__(self, source: FakeSource, info: TrackInfo, broken: bool):
        super().__init__(info.id, source.policy.handle_ttl)
        self.source, self.info, self.broken = source, info, broken

    def _read(self) -> tuple:
        if self.broken:
            raise SourceError(f"couldn't decode {self.info.id}")
        self.source.reads.append(self.info.id)
        bpm = self.info.features.get("bpm", 122.0)  # the generator needs some tempo even when the catalog hides it
        return click_track(bpm, self.source.seconds, seed=int(self.info.id.key[1:])), SR
