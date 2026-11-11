"""The listener's own decision about Spotify audio (SPOTIFY_INTEGRATION_PLAN.md, Milestone 2).

Spotify's own terms never permit a third-party app to read its audio, so there is no agreement to
record here and there never will be. What there is instead is the listener's acknowledgement: audio
stays off until they read what it means and turn it on. `SpotifySource.open_audio` then works
because the policy says so, not because anything was granted by Spotify.

The choice lives in a small file of its own, `~/.disko/spotify/consent.json`:

    {"version": 1, "audio": true, "date": "2026-11-11"}

Not in the Keychain (it isn't a secret), not in `.env` or the environment (a headless run must use
the same stored choice as the window, and no variable may unlock audio on its own), and not in
`QSettings` (the CLI reads it too). Signing out clears it, so a forgotten account leaves no switch
turned on behind it.
"""
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

SETTINGS = Path.home() / ".disko" / "spotify" / "consent.json"
VERSION = 1

# Shown where the choice is made, in the window and in the terminal. Plain words, no selling.
WARNING = """Turning on Spotify audio means:

- disko reads your tracks through librespot, an unofficial open-source Spotify client, and
  decodes them on this computer. It needs a Spotify Premium account.
- It is against Spotify's terms of service, whatever plan the account holds: Spotify does not
  permit a third-party app to read its audio, and disko has no agreement with it.
- The account could be suspended. disko fetches one track at a time, only when it is about to
  play, which keeps the risk low but not zero.
- It can stop working without notice, whenever Spotify changes its protocol and librespot has
  not caught up.

The audio is used for this session only: it is decoded into a temporary folder that is emptied as
it goes and removed when disko exits. disko never records or exports a set that contains it, and
nothing it keeps about a track is audio. None of that makes it permitted by Spotify.
"""


@dataclass(frozen=True)
class Choice:
    """What the listener chose. `audio=False` is browse only: what they have before choosing at all."""

    audio: bool = False
    date: str = ""       # the day the choice was made (ISO), for the agreement wording
    version: int = VERSION

    @property
    def reference(self) -> str:
        """The `agreement.reference` a policy built on this choice records, worded honestly."""
        when = f" on {self.date}" if self.date else ""
        return f"listener's own choice, made in the app{when} (unofficial client; not permitted by Spotify)"

    @property
    def summary(self) -> str:
        if not self.audio:
            return "browse only (Spotify audio is off)"
        return f"Spotify audio is on (turned on {self.date})" if self.date else "Spotify audio is on"


class ConsentStore:
    """The stored choice, in one JSON file. A file that can't be read counts as no choice: audio off."""

    def __init__(self, path: Path = SETTINGS):
        self.path = Path(path)

    @property
    def folder(self) -> Path:
        return self.path.parent

    def read(self) -> Choice | None:
        """The stored choice, or None when the listener has never been asked."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("audio"), bool):
            return None
        when = data.get("date")
        return Choice(bool(data["audio"]), when if isinstance(when, str) else "",
                      data.get("version", VERSION) if isinstance(data.get("version"), int) else VERSION)

    def write(self, choice: Choice) -> Choice:
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": choice.version, "audio": choice.audio,
                                         "date": choice.date}, indent=1) + "\n")
        return choice

    def clear(self):
        """Forget the choice: the next run is browse only again (called on sign-out)."""
        self.path.unlink(missing_ok=True)

    def choose(self, audio: bool, when: str | None = None) -> Choice:
        """Record a fresh choice, dated today unless a date is given (tests pass one)."""
        return self.write(Choice(bool(audio), (when or date.today().isoformat()) if audio else ""))

    def enabled(self) -> bool:
        choice = self.read()
        return bool(choice and choice.audio)
