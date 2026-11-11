"""Beatport as a track source: the account's playlists, Beatport charts and curated playlists,
and catalog metadata.

Playlist ids name where a list lives:

    my:123        one of the account's own playlists   GET /my/playlists/123/tracks/
    catalog:456   a Beatport curated playlist          GET /catalog/playlists/456/tracks/
    chart:789     a Beatport chart                     GET /catalog/charts/789/tracks/

Track metadata comes from the catalog's own fields and nothing else: tempo, Camelot key, genre and
length. The v4 docs list parameters but no response schemas, so the field names below follow the
v4 catalog JSON as public open-source clients read it. `track_info` is the one place that knows
them; check it against a real answer with `python -m live.sources.beatport track ID --raw`.

Audio isn't here in the browsing adapter, but streaming playback is: when the account holds a
Beatport Streaming plan (DISKO_BEATPORT_STREAMING=1), `_open_audio` fetches
`/catalog/tracks/{id}/stream/`, which answers with a short-lived `stream_url` (an encrypted HLS
playlist), and decodes it here through ffmpeg. That URL is the documented-for-partners streaming
endpoint; the plan is what authorizes it. Nothing is written to disk.
"""
import shutil
import subprocess

import numpy as np

from ..base import (AudioHandle, Availability, Operation, Page, PlaylistRef, Source, SourceError, SourceTimeout,
                    TrackId, TrackInfo, TrackUnavailable)
from ..policy import Policy

PROVIDER = "beatport"
PER_PAGE = 100          # Beatport's page size for list calls
MAX_PLAYLISTS = 500
SAMPLE_RATE = 44100     # the decks run at this rate (live/audio.py SR)
FFMPEG_TIMEOUT_S = 180  # decoding a stream longer than this is treated as a stall
LISTS = {"my": "/my/playlists/{}/tracks/", "catalog": "/catalog/playlists/{}/tracks/",
         "chart": "/catalog/charts/{}/tracks/"}


def _parse_playlist(playlist_id: str) -> str:
    kind, _, key = playlist_id.partition(":")
    if kind not in LISTS or not key.isdigit():
        raise SourceError(f"{playlist_id!r} isn't a Beatport list (my:ID, catalog:ID or chart:ID)")
    return LISTS[kind].format(key)


def _camelot(key) -> str | None:
    if not isinstance(key, dict):
        return None
    number, letter = key.get("camelot_number"), key.get("camelot_letter")
    if isinstance(number, int) and 1 <= number <= 12 and letter in ("A", "B"):
        return f"{number}{letter}"
    return None


def _name(obj) -> str | None:
    return obj.get("name") if isinstance(obj, dict) and isinstance(obj.get("name"), str) else None


def track_info(data: dict) -> TrackInfo:
    """A catalog track JSON object (or a playlist entry wrapping one) as a TrackInfo."""
    if isinstance(data.get("track"), dict):  # playlist entries wrap the track with their own id and position
        data = data["track"]
    if not isinstance(data.get("id"), int):
        raise SourceError("Beatport sent a track without an id")
    title = data.get("name") or "Untitled"
    mix = data.get("mix_name")
    if isinstance(mix, str) and mix:
        title = f"{title} ({mix})"
    artists = [n for n in map(_name, data.get("artists") or []) if n]
    features, provenance = {}, {}
    bpm = data.get("bpm")
    if isinstance(bpm, (int, float)) and 40 <= bpm <= 250:
        features["bpm"] = float(bpm)
    camelot = _camelot(data.get("key"))
    if camelot:
        features["camelot"] = camelot
    for k in features:
        provenance[k] = "beatport catalog"
    length = data.get("length_ms")
    duration = length / 1000 if isinstance(length, (int, float)) and length > 0 else None
    genres = tuple(n for n in (_name(data.get("genre")), _name(data.get("sub_genre"))) if n)
    streamable = data.get("is_available_for_streaming")
    availability = (Availability.UNKNOWN if not isinstance(streamable, bool)
                    else Availability.AVAILABLE if streamable else Availability.UNAVAILABLE)
    if duration is not None:
        provenance["duration"] = "beatport catalog"
    if genres:
        provenance["genres"] = "beatport catalog"
    return TrackInfo(TrackId(PROVIDER, str(data["id"])), title, ", ".join(artists) or None, duration,
                     availability, genres, features, provenance)


class BeatportSource(Source):
    provider = PROVIDER
    label = "Beatport"

    def __init__(self, client, policy: Policy, session=None):
        super().__init__(policy)
        self.client, self.session = client, session

    def account(self) -> dict:
        data = self.client.get("/my/account/")
        return {"username": data.get("username")} if isinstance(data, dict) else {}

    def _playlists(self) -> list:
        out, page = [], 1
        while len(out) < MAX_PLAYLISTS:
            items, total = self.client.page("/my/playlists/", page, PER_PAGE)
            for p in items:
                if isinstance(p, dict) and isinstance(p.get("id"), int):
                    count = p.get("track_count")
                    out.append(PlaylistRef(f"my:{p['id']}", p.get("name") or f"Playlist {p['id']}",
                                           count if isinstance(count, int) else None))
            if not items or (total is not None and page * PER_PAGE >= total):
                break
            page += 1
        return out[:MAX_PLAYLISTS]

    def _tracks(self, playlist_id: str, offset: int, limit: int) -> Page:
        path = _parse_playlist(playlist_id)
        items, total = [], None
        while len(items) < limit:
            at = offset + len(items)
            page_items, total = self.client.page(path, at // PER_PAGE + 1, PER_PAGE)
            chunk = page_items[at % PER_PAGE:][:limit - len(items)]
            if not chunk:
                break
            items.extend(chunk)
            if len(page_items) < PER_PAGE:
                break
        infos = []
        for item in items:
            try:
                infos.append(track_info(item))
            except SourceError:
                continue  # an entry without a track (removed from the catalog): nothing to list
        return Page(tuple(infos), offset, total)

    def _info(self, track_id: TrackId) -> TrackInfo:
        if not track_id.key.isdigit():
            raise SourceError(f"{track_id} isn't a Beatport track id")
        return track_info(self.client.get(f"/catalog/tracks/{track_id.key}/"))

    def raw_track(self, track_id: TrackId) -> dict:
        """The catalog's own JSON for a track (to check `track_info` against)."""
        self._own(track_id)
        self._live()
        self.policy.require(Operation.METADATA, track_id)
        if not track_id.key.isdigit():
            raise SourceError(f"{track_id} isn't a Beatport track id")
        return self.client.get(f"/catalog/tracks/{track_id.key}/")

    def _open_audio(self, track_id: TrackId) -> AudioHandle:
        """The stream URL for this track, which ffmpeg decodes to PCM on read. Beatport answers 403
        here when the account has no streaming plan; the policy already allowed DECODE before this."""
        if not track_id.key.isdigit():
            raise SourceError(f"{track_id} isn't a Beatport track id")
        try:
            data = self.client.get(f"/catalog/tracks/{track_id.key}/stream/")
        except TrackUnavailable as e:
            raise TrackUnavailable(f"{e}; playing it needs an active Beatport Streaming plan") from None
        url = data.get("stream_url") if isinstance(data, dict) else None
        if not isinstance(url, str) or not url:
            raise SourceError(f"Beatport didn't return a stream URL for {track_id} "
                              f"(an active Beatport Streaming plan is required)")
        return StreamAudio(track_id, url)


class StreamAudio(AudioHandle):
    """A Beatport stream decoded to PCM here. The `stream_url` is short-lived and already carries the
    plan's entitlement, so no token goes with it; ffmpeg handles the encrypted HLS. Nothing is kept
    on disk: ffmpeg writes the decoded samples to this process's memory."""

    def __init__(self, track_id: TrackId, url: str, sample_rate: int = SAMPLE_RATE):
        super().__init__(track_id)
        self.url, self.sample_rate = url, sample_rate

    def _read(self) -> tuple:
        return decode_stream(self.url, self.sample_rate)


def decode_stream(url: str, sample_rate: int = SAMPLE_RATE) -> tuple:
    """(samples (n, 2) float32, sample_rate): decode an audio/HLS URL through ffmpeg, in memory.

    A Beatport `stream_url` is an AES-128 encrypted HLS playlist, which libsndfile can't read but
    ffmpeg can, so the stream is piped straight to raw float samples. A missing ffmpeg is reported
    like any other unreadable track."""
    ffmpeg = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    command = [ffmpeg, "-v", "error", "-nostdin", "-i", url, "-f", "f32le", "-ac", "2",
               "-ar", str(sample_rate), "-"]
    try:
        raw = subprocess.run(command, capture_output=True, timeout=FFMPEG_TIMEOUT_S, check=True).stdout
    except FileNotFoundError:
        raise SourceError("decoding a Beatport stream needs ffmpeg on PATH") from None
    except subprocess.TimeoutExpired:
        raise SourceTimeout(f"decoding the Beatport stream took longer than {FFMPEG_TIMEOUT_S}s") from None
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise SourceError(f"couldn't decode the Beatport stream ({detail[-1] if detail else e.returncode})") from None
    samples = np.frombuffer(raw, dtype=np.float32)
    if samples.size % 2:
        samples = samples[:-1]
    return samples.reshape(-1, 2), sample_rate
