"""live/sources/spotify/preview.py (SPOTIFY_INTEGRATION_PLAN.md, Milestone 4): the provider order and
each fallback, the cache, the sanity check on what was downloaded, file-safe names, the courtesy gap,
and that a track with no preview is simply None. Every provider is a fake opener, except one path that
runs through the real urllib opener against tests/fake_previews.py. No request leaves the machine.
"""
import hashlib
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

from live.sources import TrackId, TrackInfo
from live.sources.spotify.preview import PREVIEW_SECONDS, PROVIDERS, Preview, PreviewFetcher

from fake_previews import AUDIO, FakePreviews

KEY = "4uLU6hMCjMI75M1A2tKUQC"        # a 22-character Spotify id, like Spotify's own
KEY2 = "6rqhFgbbKwnb9MLmUQDhG6"
SPOTIFY_PREVIEW = "http://cdn/preview/song.mp3"
ITUNES_PREVIEW = "http://cdn/preview/song.m4a"
DEEZER_PREVIEW = "http://cdn/preview/song.mp3"


def track(key, isrc=None):
    return TrackInfo(TrackId("spotify", key), "A Track", isrc=isrc)


def embed_page(url=None):
    inner = f'"audioPreview":{{"url":"{url}"}}' if url else '"audioPreview":null'
    return f"<!doctype html><script>__NEXT_DATA__ = {{{inner}}}</script>".encode()


ITUNES_EMPTY = json.dumps({"resultCount": 0, "results": []}).encode()
DEEZER_ERROR = json.dumps({"error": {"type": "DataException", "message": "no track"}}).encode()


def itunes_page(url):
    return json.dumps({"resultCount": 1, "results": [{"previewUrl": url}]}).encode()


def deezer_page(url):
    return json.dumps({"preview": url}).encode()


class Routes:
    """A stand-in for the opener: answers the first matching prefix, or raises, and keeps the order of
    the urls it was asked for."""

    def __init__(self, *routes):
        self.routes = list(routes)   # (prefix, bytes | Exception)
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for prefix, body in sorted(self.routes, key=lambda r: -len(r[0])):
            if url.startswith(prefix):
                if isinstance(body, Exception):
                    raise body
                return body
        raise OSError(f"no route for {url}")

    @property
    def paths(self):
        return [urllib.parse.urlparse(u).path for u in self.calls]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "previews"
        self.embed = "http://t/embed/track/{}"
        self.itunes = "http://t/lookup?isrc={}&entity=song"
        self.deezer = "http://t/track/isrc:{}"

    def fetcher(self, *routes, **options):
        self.opener = Routes(*routes)
        return PreviewFetcher(self.folder, embed=self.embed, itunes=self.itunes, deezer=self.deezer,
                              opener=self.opener, **options)


class OrderTest(Base):
    def test_spotify_is_asked_first_and_its_preview_is_returned(self):
        f = self.fetcher(("http://t/embed/track/", embed_page(SPOTIFY_PREVIEW)),
                         (SPOTIFY_PREVIEW, AUDIO))
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual((preview.provider, preview.seconds), ("spotify", PREVIEW_SECONDS))
        self.assertEqual(preview.seconds, 30.0)
        self.assertTrue(preview.path.exists())
        self.assertEqual(preview.path.name, f"{KEY}.spotify.mp3")
        # the embed page only, then the file it pointed at; no other provider was asked
        self.assertEqual(self.opener.paths, [f"/embed/track/{KEY}", "/preview/song.mp3"])
        self.assertEqual(Preview(Path("x"), "deezer").seconds, PREVIEW_SECONDS)   # seconds left unset
        self.assertEqual(PROVIDERS, ("spotify", "itunes", "deezer"))

    def test_itunes_is_tried_when_spotify_has_nothing(self):
        f = self.fetcher(("http://t/embed/track/", embed_page()),
                         ("http://t/lookup", itunes_page(ITUNES_PREVIEW)),
                         (ITUNES_PREVIEW, AUDIO))
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview.provider, "itunes")
        self.assertEqual(preview.path.suffix, ".m4a")
        self.assertEqual(preview.path.name, f"{KEY}.itunes.m4a")
        self.assertEqual(self.opener.paths, [f"/embed/track/{KEY}", "/lookup", "/preview/song.m4a"])

    def test_deezer_is_last_when_the_first_two_have_nothing(self):
        f = self.fetcher(("http://t/embed/track/", embed_page()),
                         ("http://t/lookup", ITUNES_EMPTY),
                         ("http://t/track/isrc:", deezer_page(DEEZER_PREVIEW)),
                         (DEEZER_PREVIEW, AUDIO))
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview.provider, "deezer")
        self.assertEqual(preview.path.name, f"{KEY}.deezer.mp3")

    def test_no_isrc_means_the_isrc_providers_are_not_asked(self):
        f = self.fetcher(("http://t/embed/track/", embed_page()))
        self.assertIsNone(f.fetch(track(KEY)))
        self.assertEqual(self.opener.calls, [f"http://t/embed/track/{KEY}"])

    def test_a_track_with_no_preview_anywhere_is_none_and_leaves_nothing(self):
        f = self.fetcher(("http://t/embed/track/", urllib.error.HTTPError("http://t", 404, "gone", {}, None)),
                         ("http://t/lookup", urllib.error.HTTPError("http://t", 503, "busy", {}, None)),
                         ("http://t/track/isrc:", DEEZER_ERROR))
        self.assertIsNone(f.fetch(track(KEY, isrc="QZTEST260000")))   # errors are swallowed, not raised
        self.assertEqual(self.opener.paths, [f"/embed/track/{KEY}", "/lookup", "/track/isrc:QZTEST260000"])
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_provider_that_errors_is_not_retried(self):
        f = self.fetcher(("http://t/embed/track/", OSError("refused")),
                         ("http://t/lookup", OSError("refused")),
                         ("http://t/track/isrc:", deezer_page(DEEZER_PREVIEW)),
                         (DEEZER_PREVIEW, AUDIO))
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview.provider, "deezer")
        self.assertEqual(self.opener.paths, [f"/embed/track/{KEY}", "/lookup", "/track/isrc:QZTEST260000",
                                             "/preview/song.mp3"])   # one request each, in order


class FallbackTest(Base):
    def test_an_html_body_is_a_failure_and_the_next_provider_is_tried(self):
        f = self.fetcher(("http://t/embed/track/", embed_page("http://cdn/bad.mp3")),
                         ("http://cdn/bad.mp3", b"<!doctype html><body>not audio</body>"),
                         ("http://t/lookup", itunes_page(ITUNES_PREVIEW)),
                         (ITUNES_PREVIEW, AUDIO))
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview.provider, "itunes")
        self.assertEqual([p.name for p in self.folder.iterdir()], [f"{KEY}.itunes.m4a"])

    def test_an_empty_body_is_a_failure_and_leaves_nothing(self):
        f = self.fetcher(("http://t/embed/track/", embed_page("http://cdn/empty.mp3")),
                         ("http://cdn/empty.mp3", b""))
        self.assertIsNone(f.fetch(track(KEY)))
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_a_non_json_body_is_skipped(self):
        f = self.fetcher(("http://t/embed/track/", embed_page()),
                         ("http://t/lookup", b"<html>not json</html>"),
                         ("http://t/track/isrc:", deezer_page(DEEZER_PREVIEW)),
                         (DEEZER_PREVIEW, AUDIO))
        self.assertEqual(f.fetch(track(KEY, isrc="QZTEST260000")).provider, "deezer")


class CacheTest(Base):
    def test_a_fetched_preview_is_reused_and_no_second_request_is_made(self):
        f = self.fetcher(("http://t/embed/track/", embed_page(SPOTIFY_PREVIEW)), (SPOTIFY_PREVIEW, AUDIO))
        first = f.fetch(track(KEY, isrc="QZTEST260000"))
        asked = len(self.opener.calls)
        again = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(again, first)
        self.assertEqual(len(self.opener.calls), asked)
        self.assertIsNotNone(f.cached(track(KEY, isrc="QZTEST260000")))

    def test_a_file_left_by_an_earlier_fetcher_is_used(self):
        first = self.fetcher(("http://t/embed/track/", embed_page(SPOTIFY_PREVIEW)), (SPOTIFY_PREVIEW, AUDIO))
        written = first.fetch(track(KEY, isrc="QZTEST260000"))
        fresh = PreviewFetcher(self.folder, embed=self.embed, itunes=self.itunes, deezer=self.deezer,
                               opener=Routes())   # raises for anything: the cache must answer
        preview = fresh.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview, written)
        self.assertEqual(fresh.opener.calls, [])


class NameTest(Base):
    def test_a_key_that_is_not_file_safe_cannot_escape_the_folder(self):
        f = self.fetcher(("http://t/embed/track/", embed_page(SPOTIFY_PREVIEW)), (SPOTIFY_PREVIEW, AUDIO))
        preview = f.fetch(track("../../escape"))
        safe = hashlib.sha1(b"../../escape").hexdigest()
        self.assertEqual(preview.path.name, f"{safe}.spotify.mp3")   # hashed: no separator, no dots in a key
        self.assertEqual(preview.path.parent, self.folder)
        self.assertEqual([p.name for p in self.folder.iterdir()], [f"{safe}.spotify.mp3"])
        self.assertFalse((self.folder.parent / "escape").exists())   # nothing was written above the folder


class GapTest(Base):
    def test_the_gap_between_two_requests_to_one_provider_is_respected(self):
        waits = []
        f = self.fetcher(("http://t/embed/track/", embed_page(SPOTIFY_PREVIEW)), (SPOTIFY_PREVIEW, AUDIO),
                         gap=5.0, sleep=waits.append)
        f.fetch(track(KEY))
        f.fetch(track(KEY2))
        self.assertEqual(len(waits), 1)                  # the first lookup never waits
        self.assertTrue(0 < waits[0] <= 5.0)


class UrllibTest(Base):
    def test_one_fetch_runs_through_the_real_urllib_opener_offline(self):
        server = FakePreviews().start()
        self.addCleanup(server.stop)
        server.spotify[KEY] = f"/preview/{KEY}.mp3"
        f = PreviewFetcher(self.folder, embed=server.url("/embed/track/{}"),
                           itunes=server.url("/lookup?isrc={}&entity=song"),
                           deezer=server.url("/track/isrc:{}"))   # opener left as the real urllib one
        preview = f.fetch(track(KEY, isrc="QZTEST260000"))
        self.assertEqual(preview.provider, "spotify")
        self.assertEqual(preview.path.name, f"{KEY}.spotify.mp3")
        self.assertTrue(preview.path.read_bytes().startswith(b"\xff\xfb"))
        self.assertIn(f"/embed/track/{KEY}", server.requests)
        self.assertIn(f"/preview/{KEY}.mp3", server.requests)
        before = len(server.requests)
        self.assertEqual(f.fetch(track(KEY, isrc="QZTEST260000")), preview)
        self.assertEqual(len(server.requests), before)   # the cache answered: the server saw nothing more


if __name__ == "__main__":
    unittest.main()
