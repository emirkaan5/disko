"""Spotify as a track source: the account's playlists and Liked Songs, and catalog metadata from the
Web API. No audio.

Playlist ids are Spotify's own (a bare id, a spotify:playlist: URI or an open.spotify.com link all
work), or `liked` for the account's Liked Songs.

What Spotify says about a track is its title, artists, length, ISRC and whether it plays in the
account's market. It supplies no tempo or key (the audio-features endpoint is closed to new apps),
so every Spotify track is short of the mixing minimum and `candidate_pool` lists it with that
reason. `track_info` is the one place that knows the JSON field names; check it against a real
answer with `python -m live.sources.spotify track ID --raw`.

Spotify renamed parts of the playlist API in 2026 (`/playlists/{id}/items`, entries under `item`);
both the new and the older names (`/tracks`, `track`) are read.
"""
import re
import urllib.parse

from ..base import (AudioHandle, Availability, Operation, Page, PlaylistRef, Source, SourceError, TrackId,
                    TrackInfo)
from ..policy import Policy
from .api import NotFound

PROVIDER = "spotify"
PER_PAGE = 50              # the most Spotify returns per page for playlists and saved tracks
MAX_PLAYLISTS = 500
LIKED = "liked"
MARKET = {"market": "from_token"}   # ask for availability in the signed-in account's own country
SPOTIFY_ID = re.compile(r"^[0-9A-Za-z]{22}$")
RESTRICTIONS = {"market": Availability.REGION_RESTRICTED, "product": Availability.ENTITLEMENT_REQUIRED,
                "explicit": Availability.UNAVAILABLE}


def parse_id(text: str, kind: str) -> str:
    """A Spotify id out of a bare id, a spotify:KIND:ID URI or an open.spotify.com/KIND/ID link."""
    text = (text or "").strip()
    if text.startswith(f"spotify:{kind}:"):
        text = text.split(":")[2]
    elif "open.spotify.com" in text:
        parts = [p for p in urllib.parse.urlparse(text).path.split("/") if p]
        if kind in parts and parts.index(kind) + 1 < len(parts):
            text = parts[parts.index(kind) + 1]
    if not SPOTIFY_ID.match(text):
        raise SourceError(f"{text!r} isn't a Spotify {kind} id")
    return text


def _availability(data: dict) -> Availability:
    if data.get("is_local"):
        return Availability.UNAVAILABLE
    playable = data.get("is_playable")
    if playable is True:
        return Availability.AVAILABLE
    if playable is False:
        reason = (data.get("restrictions") or {}).get("reason")
        return RESTRICTIONS.get(reason, Availability.UNAVAILABLE)
    return Availability.UNKNOWN


def track_info(data: dict) -> TrackInfo | None:
    """A Web API track object (or a playlist / saved-track entry wrapping one) as a TrackInfo; None
    for an entry that isn't a track (a podcast episode, or a track removed from the catalog)."""
    if "added_at" in data:  # a playlist or saved-tracks entry: the track is inside (None once it's removed)
        data = data.get("item") or data.get("track")
    if not isinstance(data, dict) or data.get("type", "track") != "track":
        return None
    if data.get("is_local"):
        key = data.get("uri")  # a file on someone's computer, listed in the playlist: no catalog id
    else:
        key = data.get("id")
    if not isinstance(key, str) or not key:
        return None
    artists = [a.get("name") for a in data.get("artists") or [] if isinstance(a, dict) and a.get("name")]
    length = data.get("duration_ms")
    duration = length / 1000 if isinstance(length, (int, float)) and length > 0 else None
    isrc = (data.get("external_ids") or {}).get("isrc")
    provenance = {"duration": "spotify catalog"} if duration is not None else {}
    return TrackInfo(TrackId(PROVIDER, key), data.get("name") or "Untitled", ", ".join(artists) or None, duration,
                     _availability(data), (), {}, provenance,
                     isrc=isrc.strip().upper() if isinstance(isrc, str) and isrc.strip() else None)


def _count(playlist: dict) -> int | None:
    for field in ("items", "tracks"):
        total = (playlist.get(field) or {}).get("total") if isinstance(playlist.get(field), dict) else None
        if isinstance(total, int):
            return total
    return None


class SpotifySource(Source):
    provider = PROVIDER
    label = "Spotify"

    def __init__(self, client, policy: Policy, session=None):
        super().__init__(policy)
        self.client, self.session = client, session
        self._items_path = "/playlists/{}/items"   # falls back to /tracks once if this one is missing

    def account(self) -> dict:
        data = self.client.get("/me")
        return {"name": data.get("display_name") or data.get("id")} if isinstance(data, dict) else {}

    def _playlists(self) -> list:
        out, offset = [PlaylistRef(LIKED, "Liked Songs")], 0
        while len(out) < MAX_PLAYLISTS:
            items, total = self.client.page("/me/playlists", offset, PER_PAGE)
            for p in items:
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    out.append(PlaylistRef(p["id"], p.get("name") or "Untitled playlist", _count(p)))
            offset += len(items)
            if not items or (total is not None and offset >= total):
                break
        return out[:MAX_PLAYLISTS]

    def _page(self, playlist_id: str, offset: int, limit: int) -> tuple:
        if playlist_id == LIKED:
            return self.client.page("/me/tracks", offset, limit, MARKET)
        pid = parse_id(playlist_id, "playlist")
        try:
            return self.client.page(self._items_path.format(pid), offset, limit, MARKET)
        except NotFound:
            if self._items_path.endswith("/tracks"):
                raise
            self._items_path = "/playlists/{}/tracks"  # the older name of the same list
            return self.client.page(self._items_path.format(pid), offset, limit, MARKET)

    def _tracks(self, playlist_id: str, offset: int, limit: int) -> Page:
        items, total = [], None
        while len(items) < limit:
            want = min(PER_PAGE, limit - len(items))
            chunk, total = self._page(playlist_id, offset + len(items), want)
            items.extend(chunk)
            if len(chunk) < want:
                break
        infos = tuple(i for i in map(track_info, (x for x in items if isinstance(x, dict))) if i is not None)
        return Page(infos, offset, total)

    def _info(self, track_id: TrackId) -> TrackInfo:
        info = track_info(self.client.get(f"/tracks/{parse_id(track_id.key, 'track')}", MARKET))
        if info is None:
            raise SourceError(f"Spotify sent no track for {track_id}")
        return info

    def raw_track(self, track_id: TrackId) -> dict:
        """Spotify's own JSON for a track (to check `track_info` against)."""
        self._own(track_id)
        self._live()
        self.policy.require(Operation.METADATA, track_id)
        return self.client.get(f"/tracks/{parse_id(track_id.key, 'track')}", MARKET)

    def _open_audio(self, track_id: TrackId) -> AudioHandle:
        raise SourceError("disko has no licensed way to read Spotify audio; Spotify tracks can be browsed, not played")
