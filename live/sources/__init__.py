"""Where tracks come from, and what disko may do with them (BEATPORT_INTEGRATION_PLAN.md, Milestone 1).

    base.py     ids, availability, operations, browsing pages and audio handles
    policy.py   fail-closed permissions from a recorded capability matrix
    local.py    the listener's own playlist folder
    fake.py     a synthetic catalog for tests
    beatport/   Beatport sign-in and browsing, shut until its written terms are recorded

Code that touches a track's audio or sends it anywhere asks through `require` first.
"""
from .base import (AudioHandle, AuthExpired, Availability, Operation, Page, PlaylistRef, Source, SourceError,
                   SourceTimeout, TrackId, TrackInfo, TrackUnavailable)
from .policy import Policy, PolicyDenied

_LOCAL = Policy.local()


def policy_for(track) -> Policy:
    """The policy covering a track: its source's, local files' for a plain local file, else deny-all."""
    if track.source is not None:
        return track.source.policy
    if track.provider == "local":
        return _LOCAL
    return Policy.deny_all(track.provider, "the track has no source to answer for it")


def require(track, *ops: Operation):
    """Raise PolicyDenied unless every operation is permitted for this track (nothing is read)."""
    policy_for(track).require_all(ops, track.id)


def allows(track, op: Operation) -> bool:
    return policy_for(track).allows(op)


def open_track_audio(track) -> AudioHandle:
    """A handle on a track's audio, through its source (a local file needs only its path)."""
    if track.source is not None:
        return track.source.open_audio(track.id)
    require(track, Operation.DECODE)
    if track.path is None:
        raise SourceError(f"{track.display} has no file to read")
    from .local import FileAudio
    return FileAudio(track.id, track.path)


__all__ = ["AudioHandle", "AuthExpired", "Availability", "Operation", "Page", "PlaylistRef", "Policy", "PolicyDenied",
           "Source", "SourceError", "SourceTimeout", "TrackId", "TrackInfo", "TrackUnavailable", "allows",
           "open_track_audio", "policy_for", "require"]
