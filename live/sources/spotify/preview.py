"""Previews: fetch a 30 s clip for a Spotify track so it can be analysed (SPOTIFY_INTEGRATION_PLAN.md,
Milestone 4).

Spotify's catalog carries no tempo and no key, and `candidate_pool` drops any track that has neither, so
a Spotify playlist would reach the mix empty. A 30 s clip is enough to bring a track's tempo, key,
energy and loudness out of the audio, so each track's preview is fetched once and analysed in the
track's place. The clip is taken from the first provider that has one, tried in this order: Spotify's
own embed page, then the iTunes Search API by ISRC, then Deezer by ISRC. The two by-ISRC providers are
asked only when the track has an ISRC.

A preview that was fetched is kept in this fetcher's own folder, under the track id, and so the folder is
this provider's preview cache inside the analysis store: a track whose file is already there is never
fetched again. A preview is a courtesy, not a guarantee of the source, so a track no provider will serve
is simply left without one (`fetch` returns None and never raises); only a failure to write the file is
an error (SourceError).

A preview is a hook, not the track: it is usually the loudest, most energetic part, so energy and
loudness read high on one (the plan's "Known bias"). That is fine to rank a Spotify playlist, which is
biased the same way throughout, but preview features must not be compared against full-track features
from another source.
"""
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..base import SourceError, TrackInfo

PREVIEW_SECONDS = 30.0
PROVIDERS = ("spotify", "itunes", "deezer")   # tried in this order
EMBED = "https://open.spotify.com/embed/track/{}"
ITUNES = "https://itunes.apple.com/lookup?isrc={}&entity=song"
DEEZER = "https://api.deezer.com/track/isrc:{}"
TIMEOUT = 10.0
GAP = 0.4                                     # seconds between two lookups of one provider
EXTENSIONS = (".mp3", ".m4a", ".ogg")
DEFAULT_EXTENSION = ".mp3"
SAFE_KEY = re.compile(r"^[0-9A-Za-z]{22}$")   # a Spotify id is file-safe as it stands
AUDIO_PREVIEW = re.compile(r'"audioPreview"\s*:\s*\{\s*"url"\s*:\s*"([^"]+)"')


@dataclass(frozen=True)
class Preview:
    """A downloaded preview: the file on disk, the provider that supplied it, and its length (None
    when the provider doesn't say)."""
    path: Path
    provider: str
    seconds: float | None = PREVIEW_SECONDS


def open_url(url: str, timeout: float = TIMEOUT) -> bytes:
    """The default opener: one plain GET, the body bytes and nothing else. A bad status raises OSError,
    which the caller reads as the provider having no usable preview."""
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def _safe(key: str) -> str:
    """A file name for a track key: the key itself when it is a Spotify id, else a hash of it, so a key
    (a local file's uri, say) can never carry a separator or a `..` out of the folder."""
    return key if SAFE_KEY.match(key) else hashlib.sha1(key.encode()).hexdigest()


def _extension(url: str) -> str:
    """The file's extension when the URL has a known audio one, else the safe default (Spotify's own
    preview URLs carry their id, not an extension)."""
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix if suffix in EXTENSIONS else DEFAULT_EXTENSION


def _is_audio(body: bytes) -> bool:
    """True for a body that could be audio: not empty, and not HTML served in a preview's place."""
    return bool(body) and body.lstrip()[:1] != b"<"


def _from_embed(body: bytes) -> str | None:
    found = AUDIO_PREVIEW.search(body.decode("utf-8", "replace"))
    return found.group(1).replace("\\/", "/") if found else None


def _from_itunes(body: bytes) -> str | None:
    data = json.loads(body)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    url = results[0].get("previewUrl")
    return url if isinstance(url, str) and url else None


def _from_deezer(body: bytes) -> str | None:
    data = json.loads(body)
    url = data.get("preview") if isinstance(data, dict) else None
    return url if isinstance(url, str) and url else None


class PreviewFetcher:
    """Fetches one track's preview, from the first provider that has one, and keeps it in `folder`.

    The folder is safe to share between fetchers: a preview is named after the track id and the provider
    that supplied it, so `cached` recognises a file left by an earlier run. Lookups to one provider are
    spaced by `gap` seconds as a courtesy; downloads are not, since they follow a lookup that just went
    out and point at a different host.
    """

    def __init__(self, folder: Path, *, embed: str = EMBED, itunes: str = ITUNES, deezer: str = DEEZER,
                 timeout: float = TIMEOUT, gap: float = GAP, sleep=time.sleep, opener=None):
        self.folder, self.timeout, self.gap, self.sleep = Path(folder), timeout, gap, sleep
        self.embed, self.itunes, self.deezer = embed, itunes, deezer
        self.last = {}   # provider -> the time of its last lookup, for the courtesy gap
        if opener is None:
            def opener(url):
                return open_url(url, timeout)
        self.opener = opener
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SourceError(f"can't use {self.folder} for previews: {e}") from None

    def cached(self, info: TrackInfo) -> Preview | None:
        """The file already here for this track, if there is one. Nothing is fetched."""
        if not self.folder.is_dir():
            return None
        for path in sorted(self.folder.glob(f"{_safe(info.id.key)}.*")):
            found = self._named(path)
            if found is not None:
                return found
        return None

    def fetch(self, info: TrackInfo) -> Preview | None:
        """The track's preview: from the folder when it is already there, else downloaded. None when no
        provider has one. Only a failed write raises (SourceError)."""
        have = self.cached(info)
        if have is not None:
            return have
        for provider in PROVIDERS:
            url = self._lookup(provider, info)
            if url is None:
                continue
            written = self._download(provider, info, url)
            if written is not None:
                return written
        return None

    def close(self):
        """Forget the rate-limit clock. Previews aren't deleted: they are worth keeping."""
        self.last.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ------------------------------------------------------------ internals

    def _lookup(self, provider: str, info: TrackInfo) -> str | None:
        """The preview URL this provider offers for the track, or None when it offers nothing. Every
        answer that isn't a usable URL (a miss, an error dict, non-JSON, a bad status) is read as None
        and the next provider is tried."""
        try:
            if provider == "spotify":
                return _from_embed(self._get("spotify", self.embed.format(info.id.key)))
            if not info.isrc:
                return None   # both by-ISRC providers are worthless without one
            isrc = urllib.parse.quote(info.isrc, safe="")
            if provider == "itunes":
                return _from_itunes(self._get("itunes", self.itunes.format(isrc)))
            return _from_deezer(self._get("deezer", self.deezer.format(isrc)))
        except (OSError, ValueError):
            return None

    def _get(self, provider: str, url: str) -> bytes:
        last = self.last.get(provider)
        if last is not None and self.gap > 0:
            rest = self.gap - (time.monotonic() - last)
            if rest > 0:
                self.sleep(rest)
        self.last[provider] = time.monotonic()
        return self.opener(url)

    def _download(self, provider: str, info: TrackInfo, url: str) -> Preview | None:
        try:
            body = self.opener(url)
        except OSError:
            return None
        if not _is_audio(body):
            return None   # an empty body or a page where audio was promised: leave nothing behind
        path = self.folder / f"{_safe(info.id.key)}.{provider}{_extension(url)}"
        try:
            path.write_bytes(body)
        except OSError as e:
            raise SourceError(f"couldn't write the preview for {info.id} to {self.folder}: {e}") from None
        return Preview(path, provider)

    def _named(self, path: Path) -> Preview | None:
        """A file this module wrote, read back as a Preview; None for anything else in the folder."""
        key, _, rest = path.name.partition(".")
        provider, _, ext = rest.partition(".")
        if path.is_file() and (SAFE_KEY.match(key) or re.fullmatch(r"[0-9a-f]{40}", key)):
            if provider in PROVIDERS and "." + ext in EXTENSIONS:
                return Preview(path, provider)
        return None
