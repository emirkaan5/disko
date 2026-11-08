"""A bounded set of tracks to DJ from, built from a source's playlist (BEATPORT_INTEGRATION_PLAN.md,
Milestone 3, step 3, in its provider-neutral part).

A streaming playlist can be long and a catalog endless, so the pool reads pages up to a limit and
never indexes more. Tracks that can't be played (unavailable, region-restricted, short of the
mixing minimum) are left out with their reason rather than retried later. Nothing here touches
audio: the metadata goes into Tracks exactly as the source gave it, unknowns staying unknown.
"""
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
    if not 1 <= limit <= MAX_POOL:
        raise ValueError(f"a pool holds 1-{MAX_POOL} tracks")
    tracks, skipped, seen = [], [], set()
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
            missing = [f for f in MIX_MINIMUM if info.features.get(f) is None]
            if not info.availability.playable:
                skipped.append((info, info.availability.value))
            elif missing:
                skipped.append((info, "unknown " + " and ".join(missing)))
            else:
                tracks.append(track_from_info(info, source))
        if len(seen) >= limit:
            truncated = truncated or page.next_offset is not None
            break
        offset = page.next_offset
    return Pool(tracks, skipped, truncated)
