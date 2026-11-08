"""What every track source shares: ids, what a source may do, browsing, and audio handles.

A source is where tracks come from: a playlist folder on this machine, a synthetic test catalog,
or (once authorized) a streaming provider. Identity and metadata are kept apart from audio: a
source can list a track without anyone having the right to decode it, and opening its audio goes
through the source's policy (live/sources/policy.py), which says no before anything is read.

Unknown stays unknown: a field a source doesn't supply is None, never a made-up default.
"""
import time
from dataclasses import dataclass, field
from enum import Enum

import numpy as np


class SourceError(Exception):
    """Something a source can't do right now; the message is fit to show the listener."""


class TrackUnavailable(SourceError):
    """The track can't be played (removed, region-restricted, needs another subscription)."""


class SourceTimeout(SourceError):
    """The source didn't answer in time; trying again later may work."""


class AuthExpired(SourceError):
    """The account's sign-in is gone or expired: nothing more may be read until it's renewed."""


@dataclass(frozen=True, order=True)
class TrackId:
    """A track's identity: the source it comes from and that source's own key for it.

    Two sources can use the same key ('abc' in a folder and in a catalog) without the tracks
    being confused: they differ in `provider`."""
    provider: str
    key: str

    def __post_init__(self):
        if not self.provider or ":" in self.provider:
            raise ValueError(f"bad provider name {self.provider!r}")
        if not self.key:
            raise ValueError("a track id needs a key")

    def __str__(self) -> str:
        return f"{self.provider}:{self.key}"

    @classmethod
    def parse(cls, text: str) -> "TrackId":
        provider, sep, key = text.partition(":")
        if not sep:
            raise ValueError(f"{text!r} isn't a provider-qualified track id")
        return cls(provider, key)


class Availability(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"            # gone from the catalog, or unreadable
    REGION_RESTRICTED = "region restricted"
    ENTITLEMENT_REQUIRED = "needs another subscription"
    UNKNOWN = "unknown"

    @property
    def playable(self) -> bool:
        return self is Availability.AVAILABLE


class Operation(Enum):
    """Things disko might do with a source's tracks. A policy grants each one explicitly."""
    BROWSE = "browse playlists and tracks"
    METADATA = "read track metadata"
    DECODE = "decode audio to samples (PCM)"
    ANALYZE = "analyze audio on this machine (Essentia, allin1, Demucs)"
    PERSIST_ANALYSIS = "store analysis results on disk"
    PERSIST_AUDIO = "write decoded audio or stems to disk"
    SEND_TO_MODEL = "send track descriptions to Kev"
    MIX = "mix with disko's own engine (beatmatch, effects, transitions)"
    RECORD = "record or export the set"


@dataclass(frozen=True)
class PlaylistRef:
    id: str
    name: str
    track_count: int | None = None   # None: the source doesn't say


@dataclass(frozen=True)
class TrackInfo:
    """What a source knows about a track without touching its audio.

    `features` holds only what the source itself supplies (say a catalog's BPM or key), each
    under the Track field it would fill; anything absent is unknown. `provenance` says where each
    of those came from."""
    id: TrackId
    title: str
    artist: str | None = None
    duration: float | None = None
    availability: Availability = Availability.UNKNOWN
    genres: tuple = ()
    features: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Page:
    items: tuple
    offset: int
    total: int | None   # None: the source doesn't say how many there are

    @property
    def next_offset(self) -> int | None:
        end = self.offset + len(self.items)
        if not self.items or (self.total is not None and end >= self.total):
            return None
        return end


class AudioHandle:
    """Access to one track's audio, for a bounded time. Close it when done (or use `with`);
    reading after close or past `expires_at` fails rather than returning stale audio."""

    def __init__(self, track_id: TrackId, ttl: float | None = None):
        self.track_id = track_id
        self.expires_at = None if ttl is None else time.monotonic() + ttl
        self.closed = False

    def _check(self):
        if self.closed:
            raise SourceError(f"the audio handle for {self.track_id} is closed")
        if self.expires_at is not None and time.monotonic() >= self.expires_at:
            self.close()
            raise AuthExpired(f"access to {self.track_id} has expired")

    def read(self) -> tuple:
        """(samples (n, channels) float32, sample rate) for the whole track."""
        self._check()
        return self._read()

    def _read(self) -> tuple:
        raise NotImplementedError

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Source:
    """A place tracks come from. Subclasses fill in the `_` methods; the public ones check the
    policy first, so a denied operation never reaches the subclass."""

    provider = ""      # the TrackId provider name, unique per kind of source
    label = ""         # shown to the listener

    def __init__(self, policy):
        self.policy = policy
        self.connected = True
        self._handles = set()

    def _live(self):
        if not self.connected:
            raise AuthExpired(f"{self.label or self.provider} is disconnected")

    # ------------------------------------------------------------ browsing

    def playlists(self) -> list:
        self._live()
        self.policy.require(Operation.BROWSE)
        return self._playlists()

    def tracks(self, playlist_id: str, offset: int = 0, limit: int = 100) -> Page:
        self._live()
        self.policy.require(Operation.BROWSE)
        if limit < 1 or limit > 500 or offset < 0:
            raise ValueError("pages are 1-500 tracks from a non-negative offset")
        return self._tracks(playlist_id, offset, limit)

    def info(self, track_id: TrackId) -> TrackInfo:
        self._own(track_id)
        self._live()
        self.policy.require(Operation.METADATA, track_id)
        return self._info(track_id)

    # ------------------------------------------------------------ audio

    def open_audio(self, track_id: TrackId) -> AudioHandle:
        """A handle on the track's samples; refused before anything is read unless the policy
        allows decoding."""
        self._own(track_id)
        self._live()
        self.policy.require(Operation.DECODE, track_id)
        handle = self._open_audio(track_id)
        self._handles.add(handle)
        return handle

    def read_audio(self, track_id: TrackId) -> tuple:
        with self.open_audio(track_id) as h:
            return h.read()

    def close(self):
        """Disconnect: every audio handle this source gave out stops working, and nothing more is
        read until a new connection is made."""
        self.connected = False
        for h in list(self._handles):
            h.close()
        self._handles.clear()

    def _own(self, track_id: TrackId):
        if track_id.provider != self.provider:
            raise SourceError(f"{track_id} doesn't belong to {self.label or self.provider}")

    # ------------------------------------------------------------ for subclasses

    def _playlists(self) -> list:
        raise NotImplementedError

    def _tracks(self, playlist_id: str, offset: int, limit: int) -> Page:
        raise NotImplementedError

    def _info(self, track_id: TrackId) -> TrackInfo:
        raise NotImplementedError

    def _open_audio(self, track_id: TrackId) -> AudioHandle:
        raise NotImplementedError


class ArrayAudio(AudioHandle):
    """A handle over samples already in memory (synthetic sources, tests)."""

    def __init__(self, track_id: TrackId, samples: np.ndarray, sr: int, ttl: float | None = None):
        super().__init__(track_id, ttl)
        self._samples, self._sr = samples, sr

    def _read(self) -> tuple:
        return self._samples, self._sr
