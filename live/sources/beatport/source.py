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

Audio isn't here. The docs list /catalog/tracks/{id}/stream-sdk/ without saying what it returns
or what may be done with it, and BEATPORT_INTEGRATION_PLAN.md (Milestone 2) makes playback a
choice between disko decoding the audio (Branch A) and Beatport's player playing it (Branch B)
that only the written terms can settle. Until they do, `open_audio` is refused by the policy
before anything is fetched, and would fail here even if the policy allowed it.
"""
from ..base import Availability, Operation, Page, PlaylistRef, Source, SourceError, TrackId, TrackInfo
from ..policy import Policy

PROVIDER = "beatport"
PER_PAGE = 100          # Beatport's page size for list calls
MAX_PLAYLISTS = 500
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

    def _open_audio(self, track_id: TrackId):
        raise SourceError("Beatport playback isn't built: its stream interface isn't documented, and the "
                          "written terms decide whether disko may decode it (BEATPORT_INTEGRATION_PLAN.md, "
                          "Milestone 2)")
