"""What disko may do with a source's tracks, decided before it does it, and no by default.

A policy is built from a capability matrix: a JSON record of what a provider's written agreement
and SDK contract actually permit (BEATPORT_INTEGRATION_PLAN.md, Gate 0). Every operation not
granted there is denied, and for any provider other than the listener's own files the whole
matrix counts for nothing until it names the agreement it comes from. Callers ask before they
decode, analyze, write, send or export; a denial raises PolicyDenied and nothing is read.

    {
      "provider": "beatport",
      "agreement": {"reference": "...", "date": "2027-01-31", "scope": "personal listening"},
      "allow": ["BROWSE", "METADATA", "DECODE", "ANALYZE"],
      "retention": {"analysis_on_disk": false, "audio_on_disk": false, "audio_in_session_temp": false,
                    "handle_ttl_s": 3600},
      "notes": "..."
    }
"""
import json
from dataclasses import dataclass, field
from pathlib import Path

from .base import Operation, SourceError, TrackId

MATRICES = Path(__file__).resolve().parent / "capabilities"


class PolicyDenied(SourceError):
    """The operation isn't permitted for this source; nothing was read or written."""

    def __init__(self, provider: str, op: Operation, reason: str, track_id: TrackId | None = None):
        self.provider, self.op, self.reason, self.track_id = provider, op, reason, track_id
        what = f" for {track_id}" if track_id else ""
        super().__init__(f"{provider}: not permitted to {op.value}{what} ({reason})")


@dataclass(frozen=True)
class Policy:
    provider: str
    allowed: frozenset = frozenset()
    agreement: dict | None = None
    analysis_on_disk: bool = False
    audio_on_disk: bool = False
    audio_in_session_temp: bool = False  # decoded audio may sit in the session temp folder while it's analyzed
    handle_ttl: float | None = None   # seconds an audio handle stays valid; None: no limit
    reason: str = ""                  # why things are denied, shown with every denial
    notes: str = field(default="", compare=False)

    def allows(self, op: Operation) -> bool:
        if op is Operation.PERSIST_ANALYSIS and not self.analysis_on_disk:
            return False
        if op is Operation.PERSIST_AUDIO and not self.audio_on_disk:
            return False
        if op is Operation.TEMP_AUDIO and not (self.audio_in_session_temp or self.audio_on_disk):
            return False  # keeping audio for good covers keeping it for the session
        return op in self.allowed

    def require(self, op: Operation, track_id: TrackId | None = None):
        if not self.allows(op):
            raise PolicyDenied(self.provider, op, self.reason or "not granted in its capability matrix", track_id)

    def require_all(self, ops, track_id: TrackId | None = None):
        for op in ops:
            self.require(op, track_id)

    # ------------------------------------------------------------ building one

    @classmethod
    def deny_all(cls, provider: str, reason: str) -> "Policy":
        return cls(provider, reason=reason)

    @classmethod
    def local(cls) -> "Policy":
        """The listener's own files on this machine: everything, analysis kept next to the music."""
        return cls("local", frozenset(Operation), agreement={"reference": "the listener's own files"},
                   analysis_on_disk=True, audio_on_disk=True)

    @classmethod
    def from_matrix(cls, data: dict) -> "Policy":
        """A policy from a capability matrix; anything malformed or missing is denied."""
        provider = data.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("a capability matrix needs its provider")
        agreement = data.get("agreement")
        if not isinstance(agreement, dict) or not agreement.get("reference"):
            return cls.deny_all(provider, "no written authorization is recorded for this source")
        allowed = set()
        for name in data.get("allow", []):
            try:
                allowed.add(Operation[name])
            except KeyError:
                raise ValueError(f"unknown operation {name!r} in {provider}'s capability matrix") from None
        retention = data.get("retention") or {}
        ttl = retention.get("handle_ttl_s")
        return cls(provider, frozenset(allowed), agreement=agreement,
                   analysis_on_disk=retention.get("analysis_on_disk") is True,
                   audio_on_disk=retention.get("audio_on_disk") is True,
                   audio_in_session_temp=retention.get("audio_in_session_temp") is True,
                   handle_ttl=float(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else None,
                   notes=str(data.get("notes", "")))

    @classmethod
    def load(cls, provider: str, folder: Path = MATRICES) -> "Policy":
        """The recorded matrix for a provider, or deny-all if there's none (or it can't be read)."""
        path = Path(folder) / f"{provider}.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return cls.deny_all(provider, f"no capability matrix at {path}")
        if data.get("provider") != provider:
            return cls.deny_all(provider, f"{path} is for another provider")
        try:
            return cls.from_matrix(data)
        except ValueError as e:
            return cls.deny_all(provider, str(e))
