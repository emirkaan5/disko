"""A bounded set of tracks to DJ from, built from a source's playlist (BEATPORT_INTEGRATION_PLAN.md,
Milestone 3, step 3, in its provider-neutral part).

A streaming playlist can be long and a catalog endless, so the pool reads pages up to a limit and
never indexes more. Tracks that can't be played (unavailable, region-restricted, short of the
mixing minimum) are left out with their reason rather than retried later. Nothing here touches
audio: the metadata goes into Tracks exactly as the source gave it, unknowns staying unknown.

A source whose catalog carries no tempo and no key is the exception, and is what `preview_pool` is
for (SPOTIFY_INTEGRATION_PLAN.md, Milestone 4): there the features are measured from a 30 s preview
first, and the mixing minimum is judged on the result rather than on absent metadata."""
from dataclasses import dataclass

from ..library import MIX_MINIMUM, Track
from .base import Source, TrackInfo

MAX_POOL = 500


def track_from_info(info: TrackInfo, source: Source) -> Track:
    """A Track carrying only what the source said about it."""
    fields = {k: v for k, v in info.features.items() if k in Track.__dataclass_fields__}
    return Track(uid=info.id.key if info.id.provider == "local" else str(info.id), provider=info.id.provider,
                 source=source, title=info.title, artist=info.artist or "Unknown artist",
                 availability=info.availability, genres="; ".join(info.genres) if info.genres else None,
                 duration=info.duration or 0.0, provenance=dict(info.provenance), **fields)


@dataclass
class Pool:
    tracks: list       # playable Tracks, in playlist order
    skipped: list      # (TrackInfo, reason) for the ones left out
    truncated: bool    # the playlist had more than the limit

    def summary(self) -> str:
        text = f"{len(self.tracks)} playable"
        if self.skipped:
            text += f", {len(self.skipped)} left out"
        return text + (" (playlist cut at the limit)" if self.truncated else "")


def candidate_pool(source: Source, playlist_id: str, limit: int = 200, page_size: int = 100) -> Pool:
    """A pool from a source that says enough in its metadata: what has no tempo or key is left out."""
    infos, skipped, truncated = listed(source, playlist_id, limit, page_size)
    return Pool(judged(source, infos, skipped), skipped, truncated)


def listed(source: Source, playlist_id: str, limit: int = 200, page_size: int = 100):
    """(TrackInfos, [(info, why it was left out)], truncated) for the playlist's first `limit` tracks.

    Only what the source said is read here: a track that can't be played is left out with its reason
    and one listed twice is counted once, but a track whose features the source doesn't supply is
    still listed - an analysis may fill them in later, which is how a Spotify pool is built."""
    if not 1 <= limit <= MAX_POOL:
        raise ValueError(f"a pool holds 1-{MAX_POOL} tracks")
    infos, skipped, seen = [], [], set()
    offset, pages = 0, 0
    max_pages = -(-limit // page_size) + 1  # a source misreporting its totals can't keep us paging
    truncated = False
    while offset is not None and pages < max_pages:
        page = source.tracks(playlist_id, offset, min(page_size, limit - len(seen)) or 1)
        pages += 1
        for info in page.items:
            if len(seen) >= limit:
                truncated = True
                break
            if info.id in seen:  # a playlist can list a track twice
                continue
            seen.add(info.id)
            if not info.availability.playable:
                skipped.append((info, info.availability.value))
            else:
                infos.append(info)
        if len(seen) >= limit:
            truncated = truncated or page.next_offset is not None
            break
        offset = page.next_offset
    return infos, skipped, truncated


def judged(source: Source, infos: list, skipped: list) -> list:
    """The Tracks the mixing minimum is met for; the rest are added to `skipped` with the reason,
    read off the Track as it now stands (an analysis may have filled the features in by then)."""
    tracks = []
    for info in infos:
        track = track_from_info(info, source)
        missing = [f for f in MIX_MINIMUM if getattr(track, f) is None]
        if missing:
            skipped.append((info, "unknown " + " and ".join(missing)))
        else:
            tracks.append(track)
    return tracks


def preview_pool(source: Source, playlist_id: str, previews, store, limit: int = 200, page_size: int = 100,
                 progress=None, workers: int | None = None) -> Pool:
    """A pool for a catalog that supplies no tempo and no key (SPOTIFY_INTEGRATION_PLAN.md, Milestone 4).

    The playlist's playable tracks are listed first, then each one's 30 s preview is fetched
    (live/sources/spotify/preview.py) and analyzed in its place, and only then is the pool decided:
    a track with no clip at all is left out with the reason that says so, and one whose clip didn't
    give a tempo or a key is left out as short of the mixing minimum. What the clips measured is
    kept in `store` (a SourceStore) and put into the Tracks, so a later run doesn't fetch or
    analyze them again. No track is decoded and nothing is fetched in full: that happens when a
    track is picked (Milestone 5)."""
    infos, skipped, truncated = listed(source, playlist_id, limit, page_size)
    ready = []
    for info in infos:
        if previews.fetch(info) is None:
            skipped.append((info, "no preview to analyse"))
        else:
            ready.append(info)
    tracks = [track_from_info(info, source) for info in ready]
    if tracks:
        from ..indexer import PreviewIndexer   # the worker machinery, only when a pool needs it
        PreviewIndexer(store, tracks, previews, workers=workers).run(progress)
    pool = []
    for track in tracks:
        missing = [f for f in MIX_MINIMUM if getattr(track, f) is None]
        if missing:
            skipped.append((TrackInfo(track.id, track.title, artist=track.artist, duration=track.duration or None),
                            "unknown " + " and ".join(missing)))
        else:
            pool.append(track)
    return Pool(pool, skipped, truncated)
