"""Fetch one Spotify track into a WAV, in a process of its own (SPOTIFY_INTEGRATION_PLAN.md, Gate 0
step 2 and Milestone 5).

Spotify's Web API has no audio, so the track is read through librespot, an unofficial open-source
Spotify client that talks Spotify's own protocol and needs a Premium account. It is against Spotify's
terms, which is why nothing here runs unless the listener turned audio on themselves (consent.py) and
why the app only ever asks for one track at a time, when that track is about to play.

This helper is deliberately inspectable and deliberately alone: it imports nothing from `live/` and
nothing from the app's packages, so it can run in a virtual environment of its own
(requirements/spotify.txt) where librespot and its protobuf pin live.

    .venv-spotify/bin/python live/workers/spotify_fetch.py --login     # sign in once, through the browser
    time .venv-spotify/bin/python live/workers/spotify_fetch.py --track ID --out /tmp/x.wav   # Gate 0 step 3

As a helper it reads one JSON request per line on stdin and answers on stdout:

    {"id": ..., "track": "22-character Spotify id", "path": "/where/to/write.wav"}   -> in
    {"event": "ready", "backend": "python"}                                          <- out
    {"event": "done", "id": ..., "path": ..., "seconds": 6.2, "bytes": 4060416}
    {"event": "failed", "id": ..., "kind": "auth"|"unavailable"|"timeout"|"fetch", "error": "..."}

It exits when stdin closes, so it never outlives the app. Decoding happens through ffmpeg: what
librespot hands over is the encrypted-then-decrypted Ogg file, which ffmpeg turns into the float WAV
at the decks' rate (44.1 kHz, stereo, see live/audio.py). The container file sits in a temporary
folder of its own and goes with this process; the WAV is written where the caller asked, and the
caller deletes it (live/sources/spotify/fetch.py puts it in the session temp folder).

Backends (`--backend`):

    python   librespot-python (pip install librespot). Decrypts through the library's own API, which
             needs the separate sign-in this helper makes with --login (its own credentials file,
             ~/.disko/spotify/librespot/credentials.json: a librespot sign-in, not the Web API one).
    command  any command the listener names in DISKO_SPOTIFY_FETCH_CMD, with {track}, {out} and
             {quality} substituted, expected to write the decoded audio to {out}. This is the escape
             hatch for Gate 0's alternates (a small helper on the Rust librespot crates, or the stock
             librespot binary with its pipe backend). Note that a pipe backend feeds audio at playback
             speed, so a six-minute track takes six minutes; the app's own timeout would have to be
             raised for it (see the plan's "If only realtime works").
"""
import argparse
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

SR = 44100                # the decks' rate (live/audio.py)
CHANNELS = 2
CHUNK = 0x2000            # 8 KiB at a time from the stream; a whole track at once also works
FETCH_TIMEOUT_S = 240.0   # a full track plus its decode; longer than this is a stall, not a slow fetch
CREDENTIALS = Path.home() / ".disko" / "spotify" / "librespot" / "credentials.json"
BACKENDS = ("python", "command")
ENV_COMMAND = "DISKO_SPOTIFY_FETCH_CMD"
QUALITIES = {"normal": 96, "high": 160, "very_high": 320}   # kbps, as Spotify names them


class FetchFailed(Exception):
    """A fetch that didn't produce audio. `kind` is what the app maps to its own error types."""

    def __init__(self, message: str, kind: str = "fetch"):
        self.kind = kind
        super().__init__(message)


def ffmpeg() -> str:
    """ffmpeg on PATH or Homebrew's, else the static one imageio-ffmpeg brings into this environment
    (requirements/spotify.txt), so a Mac without ffmpeg can still decode a fetched track."""
    found = shutil.which("ffmpeg") or next((p for p in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
                                            if os.access(p, os.X_OK)), None)
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # not installed, or its binary is missing: decode() says what to do
        return "ffmpeg"


def decode(container: Path, dest: Path):
    """The decrypted Ogg file as the WAV the caller asked for, through ffmpeg (libsndfile can't)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg(), "-v", "error", "-nostdin", "-y", "-i", str(container), "-ac", str(CHANNELS),
               "-ar", str(SR), "-c:a", "pcm_f32le", str(dest)]
    try:
        r = subprocess.run(command, capture_output=True, timeout=FETCH_TIMEOUT_S)
    except FileNotFoundError:
        raise FetchFailed("decoding a fetched track needs ffmpeg: set up Spotify audio again on disko's "
                          "Spotify page (it installs one), or brew install ffmpeg") from None
    except subprocess.TimeoutExpired:
        raise FetchFailed(f"ffmpeg took longer than {FETCH_TIMEOUT_S}s on the fetched track", "timeout") from None
    if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        detail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise FetchFailed(f"ffmpeg couldn't decode the fetched track ({detail[-1] if detail else r.returncode})")


class PythonBackend:
    """librespot-python, the library's documented API: content_feeder().load(track) then read its stream.

    One session for the process: signing in to Spotify's access point again for every track would be
    both slower and noisier towards the account.
    """

    name = "python"

    def __init__(self, credentials: Path = CREDENTIALS, quality: str = "very_high", device_name: str = "disko"):
        from librespot.audio.decoders import AudioQuality
        self.credentials, self.device_name = Path(credentials), device_name
        self.quality = AudioQuality[quality.upper()]  # NORMAL | HIGH | VERY_HIGH (Spotify's own scale)
        self._session = None

    def session(self):
        if self._session is None:
            if not self.credentials.exists():
                raise FetchFailed(f"no Spotify sign-in for the fetcher at {self.credentials}: "
                                  f"sign in for audio on disko's Spotify page, or run "
                                  f"live/workers/spotify_fetch.py --login once", "auth")
            from librespot.core import Session
            conf = (Session.Configuration.Builder()
                    .set_stored_credential_file(str(self.credentials))
                    .set_cache_dir(str(self.credentials.parent / "cache")).build())
            self._session = Session.Builder(conf).set_device_name(self.device_name).stored_file().create()
        return self._session

    def container(self, track: str, deadline: float) -> bytes:
        """The track's audio as decrypted Ogg Vorbis bytes: Spotify's file, unencrypted by librespot."""
        from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality
        from librespot.metadata import TrackId
        quality = self.quality or AudioQuality.VERY_HIGH
        try:
            loaded = self.session().content_feeder().load(TrackId.from_uri(f"spotify:track:{track}"),
                                                          VorbisOnlyAudioQuality(quality), False, None)
        except FetchFailed:
            raise
        except Exception as e:  # the library's own errors: restricted content, a dead session, HTTP
            raise FetchFailed(str(e), _kind(e)) from None
        stream = loaded.input_stream.stream()
        chunks, size = [], 0
        while True:
            if time.monotonic() > deadline:
                raise FetchFailed("the fetch passed its deadline while the audio was still arriving", "timeout")
            try:
                data = stream.read(CHUNK)
            except Exception as e:
                raise FetchFailed(str(e), _kind(e)) from None
            if not data:
                break
            chunks.append(data)
            size += len(data)
        if size == 0:
            raise FetchFailed("Spotify sent no audio for this track", "unavailable")
        return b"".join(chunks)


class CommandBackend:
    """Whatever DISKO_SPOTIFY_FETCH_CMD names, run per track: Gate 0's alternates, kept out of the app."""

    name = "command"

    def __init__(self, command: str, quality: str = "very_high", cwd: Path | None = None):
        if not command or "{track}" not in command or "{out}" not in command:
            raise FetchFailed(f"{ENV_COMMAND} must name a command using {{track}} and {{out}}")
        self.command, self.quality, self.cwd = command, quality, cwd

    def fetch(self, track: str, dest: Path, deadline: float):
        argv = [part.format(track=track, out=str(dest), quality=str(QUALITIES.get(self.quality, 320)))
                for part in shlex.split(self.command)]
        try:
            r = subprocess.run(argv, capture_output=True, cwd=self.cwd, timeout=max(1.0, deadline - time.monotonic()))
        except FileNotFoundError:
            raise FetchFailed(f"{argv[0]} isn't there (from {ENV_COMMAND})") from None
        except subprocess.TimeoutExpired:
            raise FetchFailed(f"the fetch command didn't finish in time", "timeout") from None
        if r.returncode != 0:
            detail = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise FetchFailed(f"the fetch command failed ({detail[-1] if detail else r.returncode})")


def _kind(error: Exception) -> str:
    """What the app should make of a library error: the wording is all librespot gives us."""
    text = str(error).lower()
    if "restrict" in text or "suitable audio" in text or "cannot get alternative" in text:
        return "unavailable"
    if "auth" in text or "credential" in text or "login" in text or "401" in text:
        return "auth"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "fetch"


def fetch(backend, request: dict, timeout: float = FETCH_TIMEOUT_S) -> dict:
    """One request: fetch `request["track"]`, decode it, and leave the WAV at `request["path"]`."""
    started = time.monotonic()
    dest = Path(request["path"])
    # The decrypted container goes in a folder of its own, removed here: the WAV the app asked for is
    # the only audio left behind, and the app's temp folder deletes that.
    with tempfile.TemporaryDirectory(prefix="disko-fetch-") as tmp:
        if isinstance(backend, CommandBackend):
            backend.fetch(request["track"], dest, started + timeout)
        else:
            container = Path(tmp) / f"{request['track']}.ogg"
            container.write_bytes(backend.container(request["track"], started + timeout))
            decode(container, dest)
    if not dest.exists() or dest.stat().st_size == 0:
        raise FetchFailed(f"the fetch left no audio at {dest}")
    return {"event": "done", "id": request.get("id"), "path": str(dest), "seconds": round(time.monotonic() - started, 2),
            "bytes": dest.stat().st_size}


def login(credentials: Path = CREDENTIALS, open_browser=None, cache: Path | None = None) -> str:
    """Sign in to Spotify once, the way librespot does it, and keep the reusable credentials.

    This is the fetcher's own sign-in, separate from the Web API one the app keeps in the Keychain:
    librespot needs its own access point credentials, and its OAuth flow uses its own client ID.
    """
    from librespot.core import Session
    credentials = Path(credentials)
    credentials.parent.mkdir(parents=True, exist_ok=True)
    cache = cache or credentials.parent / "cache"
    conf = (Session.Configuration.Builder().set_stored_credential_file(str(credentials))
            .set_cache_dir(str(cache)).build())

    def show(url: str):
        # The link is printed either way: the app shows it if the browser didn't open.
        print(f"Open this link in a browser and approve disko:\n{url}", file=sys.stderr, flush=True)
        if open_browser:
            open_browser(url)

    session = Session.Builder(conf).set_device_name("disko").oauth(show).create()
    print(f"fetcher sign-in kept in {credentials}", file=sys.stderr)
    return session.username()


def backend_for(args) -> object:
    if args.backend == "command":
        return CommandBackend(os.environ.get(ENV_COMMAND, ""), args.quality)
    return PythonBackend(Path(args.credentials), args.quality)


def missing_librespot() -> str | None:
    """The sentence to print when librespot isn't in this interpreter, or None if it is.

    Without this, a hand-run of --login or --track dies in a ModuleNotFoundError traceback, which says
    nothing about the fix. The library is only needed by the python backend.
    """
    if importlib.util.find_spec("librespot") is not None:
        return None
    return (f"librespot isn't installed in this environment ({sys.executable}).\n"
            f"This helper runs in one of its own: set up Spotify audio on disko's Spotify page, or "
            f"scripts/setup.sh --spotify-audio (requirements/spotify.txt), then run it with "
            f".venv-spotify/bin/python.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="live/workers/spotify_fetch.py", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", choices=BACKENDS, default=os.environ.get("DISKO_SPOTIFY_FETCH_BACKEND", "python"))
    ap.add_argument("--credentials", default=str(CREDENTIALS),
                    help=f"the fetcher's own librespot sign-in (default {CREDENTIALS})")
    ap.add_argument("--quality", choices=sorted(QUALITIES), default="very_high")
    ap.add_argument("--timeout", type=float, default=FETCH_TIMEOUT_S)
    ap.add_argument("--login", action="store_true", help="sign in once, through the browser, and exit")
    ap.add_argument("--open-browser", action="store_true",
                    help="with --login: open the sign-in link in the browser instead of only printing it")
    ap.add_argument("--track", help="fetch this one track id and exit (Gate 0)")
    ap.add_argument("--out", help="where to write the WAV (with --track)")
    args = ap.parse_args(argv)

    missing = missing_librespot() if args.backend == "python" else None
    if args.login:
        if missing:
            print(missing, file=sys.stderr)
            return 2
        import webbrowser
        login(Path(args.credentials), open_browser=webbrowser.open if args.open_browser else None)
        return 0
    if args.track and args.out:
        if missing:
            print(missing, file=sys.stderr)
            return 2
        # Our stdout is the protocol; anything librespot logs goes to stderr.
        try:
            print(json.dumps(fetch(backend_for(args), {"id": args.track, "track": args.track, "path": args.out},
                                   args.timeout)))
        except FetchFailed as e:
            print(json.dumps({"event": "failed", "id": args.track, "kind": e.kind, "error": str(e)}))
            return 1
        return 0
    if args.track or args.out:
        ap.error("--track and --out go together")

    proto = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)  # a stray print from a library lands on stderr, never in the protocol

    def say(**msg):
        try:
            proto.write(json.dumps(msg) + "\n")
        except BrokenPipeError:  # the app is gone
            os._exit(0)

    try:
        if missing:
            say(event="fatal", error=missing.replace("\n", " "))
            return 2
        backend = backend_for(args)
        if isinstance(backend, PythonBackend) and not Path(args.credentials).exists():
            say(event="fatal", error=f"no Spotify sign-in for audio yet ({args.credentials}): "
                                     f"finish setting up Spotify audio on disko's Spotify page")
            return 1
        say(event="ready", backend=backend.name, quality=args.quality)
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
            except ValueError:
                continue
            try:
                say(**fetch(backend, request, args.timeout))
            except FetchFailed as e:
                say(event="failed", id=request.get("id"), kind=e.kind, error=str(e))
            except Exception as e:  # a bug here must not take the helper (or the set) down
                traceback.print_exc()
                say(event="failed", id=request.get("id"), kind="fetch", error=f"{type(e).__name__}: {e}")
    except FetchFailed as e:
        say(event="fatal", error=str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
