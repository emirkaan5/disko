"""Getting the fetch helper ready from the app: its own environment, and its own Spotify sign-in.

Spotify audio needs two things besides the listener's choice (consent.py), and both are made here,
from the window, so that a disko downloaded from GitHub needs no terminal to play a Spotify track:

    environment  .venv-spotify, where librespot and its protobuf pin live (requirements/spotify.txt),
                 built with uv, which disko already needs to start Kev. The file also brings a static
                 ffmpeg (imageio-ffmpeg) for a Mac without one.
    sign-in      librespot's own credentials (~/.disko/spotify/librespot/credentials.json), made by the
                 helper's --login, which opens the browser. It is not the Web API sign-in the app keeps
                 in the Keychain: librespot signs in with its own client ID.

scripts/setup.sh --spotify-audio and `spotify_fetch.py --login` still do the same by hand.

    state = readiness()
    if not state.environment:
        build(say)          # minutes the first time: uv may download a Python, then the wheels
    if not state.signed_in:
        sign_in(say)        # waits for the listener to approve disko in the browser
"""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...steps import SetupFailed, run as _run
from .fetch import FETCH_PYTHON, HELPER, REPO

VENV = FETCH_PYTHON.parent.parent
REQUIREMENTS = REPO / "requirements" / "spotify.txt"
PYTHON_VERSION = "3.14"          # scripts/setup.sh's PYTHON_VERSION
# The helper's default (live/workers/spotify_fetch.py CREDENTIALS); that script runs in its own venv
# and can't be imported from here, so tests check that the two agree.
CREDENTIALS = Path.home() / ".disko" / "spotify" / "librespot" / "credentials.json"
# What the environment must hold to be ready: the packages requirements/spotify.txt names.
PACKAGES = ("librespot", "imageio_ffmpeg")
BUILD_TIMEOUT_S = 1200.0         # a first build may download a Python; past this it is stuck
SIGN_IN_TIMEOUT_S = 300.0        # the listener has this long to approve disko in the browser


@dataclass(frozen=True)
class Readiness:
    environment: bool    # the helper's venv holds every package in PACKAGES
    signed_in: bool      # librespot's credentials are on disk

    @property
    def ready(self) -> bool:
        return self.environment and self.signed_in

    @property
    def summary(self) -> str:
        if self.ready:
            return "ready"
        left = [what for what, done in (("install the audio helper", self.environment),
                                        ("sign in to Spotify for audio", self.signed_in)) if not done]
        return " and ".join(left)


def _installed(venv: Path, package: str) -> bool:
    """Whether `package` is in the venv, without starting its Python (this runs on the window thread)."""
    return any((site / package / "__init__.py").exists() for site in venv.glob("lib/python*/site-packages"))


def readiness(python: Path = FETCH_PYTHON, credentials: Path = CREDENTIALS) -> Readiness:
    python = Path(python)
    venv = python.parent.parent
    environment = python.exists() and all(_installed(venv, p) for p in PACKAGES)
    return Readiness(environment, Path(credentials).exists())


def find_uv() -> str | None:
    """uv on PATH or where its installer puts it (an app opened from Finder has a short PATH)."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv")):
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def build(say: Callable[[str], None] = lambda _: None, cancelled: Callable[[], bool] = lambda: False,
          venv: Path = VENV, requirements: Path = REQUIREMENTS, uv: str | None = None,
          timeout: float = BUILD_TIMEOUT_S):
    """Make the helper's environment, or bring one built before up to requirements/spotify.txt."""
    uv = uv or find_uv()
    if not uv:
        raise SetupFailed("installing the audio helper needs uv, which disko also uses to start Kev: "
                          "install it from https://docs.astral.sh/uv/ and try again")
    venv = Path(venv)
    if not (venv / "bin" / "python").exists():
        say(f"Creating {venv.name} (Python {PYTHON_VERSION})…")
        _run([uv, "venv", venv, "--python", PYTHON_VERSION], say, cancelled, timeout, "creating the environment")
    say("Installing librespot and ffmpeg…")
    _run([uv, "pip", "install", "--python", venv, "-r", requirements], say, cancelled, timeout,
         "installing the audio helper")


def sign_in(say: Callable[[str], None] = lambda _: None, cancelled: Callable[[], bool] = lambda: False,
            python: Path = FETCH_PYTHON, helper: Path = HELPER, credentials: Path = CREDENTIALS,
            timeout: float = SIGN_IN_TIMEOUT_S):
    """librespot's own sign-in: the helper opens the browser and waits for the listener to approve."""
    if not Path(python).exists():
        raise SetupFailed("the audio helper isn't installed yet")
    _run([python, helper, "--login", "--open-browser", "--credentials", credentials], say, cancelled, timeout,
         "the Spotify sign-in for audio")
    if not Path(credentials).exists():
        raise SetupFailed("the Spotify sign-in for audio finished without keeping a sign-in")


def prepare(say: Callable[[str], None] = lambda _: None, cancelled: Callable[[], bool] = lambda: False) -> Readiness:
    """Whatever is still missing, in order: the environment, then the sign-in. Safe to call again."""
    state = readiness()
    if not state.environment:
        build(say, cancelled)
    if not readiness().environment:
        raise SetupFailed(f"the audio helper's environment is still missing a package ({', '.join(PACKAGES)})")
    if not state.signed_in:
        say("Opening Spotify in your browser: approve disko there to finish.")
        sign_in(say, cancelled)
    return readiness()
