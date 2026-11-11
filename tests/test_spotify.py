"""The Spotify adapter against tests/fake_spotify.py: the gate, PKCE sign-in, refresh, paging, the
renamed and older playlist API, mapping, failures, and that tokens stay out of URLs and that no audio
is ever read. No request reaches Spotify.
"""
import contextlib
import io
import socket
import unittest
import urllib.error
import urllib.request
from unittest import mock

from live.sources import AuthExpired, Availability, Operation, Policy, PolicyDenied, SourceError, SourceTimeout, TrackId
from live.sources import spotify
from live.sources.pool import candidate_pool
from live.sources.spotify import __main__ as cli
from live.sources.spotify import auth
from live.sources.spotify.api import Forbidden, NotFound

from fake_spotify import FakeSpotify, sid


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def follow(url: str):
    """Plays the browser: opens the consent link and follows Spotify's redirect back to disko."""
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()


class Base(unittest.TestCase):
    renamed_api = True

    def setUp(self):
        self.redirect = f"http://127.0.0.1:{free_port()}/callback"
        self.fake = FakeSpotify(redirect_uris=(self.redirect,), renamed_api=self.renamed_api).start()
        self.addCleanup(self.fake.stop)
        self.store = spotify.MemoryStore({"client_id": self.fake.client_id})
        self.where = {"accounts": self.fake.accounts, "redirect_uri": self.redirect, "env": {}}

    def sign_in(self):
        spotify.login(store=self.store, open_browser=follow, **self.where)

    def source(self, **options):
        self.sign_in()
        return spotify.connect(store=self.store, api=self.fake.api, **self.where,
                               sleep=lambda s: None, **options)


class GateTest(Base):
    def test_default_policy_browses_and_never_decodes_or_feeds_kev(self):
        policy = spotify.default_policy()
        self.assertTrue(policy.allows(Operation.BROWSE) and policy.allows(Operation.METADATA))
        for op in (Operation.DECODE, Operation.ANALYZE, Operation.MIX, Operation.RECORD, Operation.SEND_TO_MODEL,
                   Operation.PERSIST_AUDIO):
            self.assertFalse(policy.allows(op), op)

    def test_denied_policy_is_refused_before_any_request(self):
        denied = Policy.deny_all("spotify", "test: nothing recorded")
        with self.assertRaises(PolicyDenied):
            spotify.connect(policy=denied, store=self.store, api=self.fake.api, **self.where)
        with self.assertRaises(PolicyDenied):
            spotify.login(policy=denied, store=self.store, open_browser=follow, **self.where)
        self.assertEqual(self.fake.requests, [])
        self.assertFalse(spotify.status(denied, self.store, env={}).authorized)

    def test_status_steps(self):
        self.assertEqual(spotify.status(store=spotify.MemoryStore(), env={}).configured, False)
        self.assertIn("not signed in", spotify.status(store=self.store, env={}).reason)
        self.sign_in()
        self.assertTrue(spotify.status(store=self.store, env={}).usable)

    def test_client_id_from_env_wins(self):
        self.assertEqual(auth.client_id(self.store, {"DISKO_SPOTIFY_CLIENT_ID": " env-id "}), "env-id")
        with self.assertRaises(spotify.NotConfigured):
            auth.client_id(spotify.MemoryStore(), {})

    def test_connect_without_sign_in(self):
        with self.assertRaises(AuthExpired):
            spotify.connect(store=self.store, api=self.fake.api, **self.where)

    def test_localhost_redirect_refused(self):
        oauth = auth.OAuth(self.fake.client_id, self.fake.accounts, "http://localhost:8766/callback")
        with self.assertRaises(spotify.NotConfigured):
            oauth.login(open_browser=follow)


class SignInTest(Base):
    def test_pkce_sign_in_saves_tokens(self):
        self.sign_in()
        tokens = spotify.Tokens.load(self.store)
        self.assertTrue(tokens.access_token and tokens.refresh_token)

    def test_wrong_client_is_refused(self):
        store = spotify.MemoryStore({"client_id": "someone-else"})
        with self.assertRaises(urllib.error.HTTPError) as caught:  # the fake consent page refuses an unknown app
            spotify.login(store=store, open_browser=follow, **{**self.where, "env": {}})
        caught.exception.close()

    def test_expired_access_token_refreshes_and_keeps_refresh_token(self):
        src = self.source()
        before = spotify.Tokens.load(self.store)
        self.fake.expire_all()
        self.assertEqual(src.account()["name"], "Test Listener")
        after = spotify.Tokens.load(self.store)
        self.assertNotEqual(before.access_token, after.access_token)
        self.assertEqual(before.refresh_token, after.refresh_token)  # Spotify didn't rotate it

    def test_revoked_refresh_token_signs_out(self):
        src = self.source()
        self.fake.refresh.clear()
        self.fake.expire_all()
        src.session.tokens.expires_at = 0
        with self.assertRaises(AuthExpired):
            src.playlists()
        self.assertIsNone(self.store.get("tokens"))

    def test_logout_forgets_tokens_but_keeps_client(self):
        self.sign_in()
        spotify.logout(self.store)
        self.assertIsNone(self.store.get("tokens"))
        self.assertEqual(self.store.get("client_id"), self.fake.client_id)

    def test_tokens_never_in_urls(self):
        src = self.source()
        src.playlists()
        token = spotify.Tokens.load(self.store).access_token
        self.assertTrue(all(token not in path for _, path, _ in self.fake.requests))
        self.assertTrue(all(bearer for method, _, bearer in self.fake.requests if method == "GET"))
        self.assertNotIn(token, repr(spotify.Tokens.load(self.store)))


class BrowseTest(Base):
    def test_playlists_start_with_liked_songs(self):
        playlists = self.source().playlists()
        self.assertEqual(playlists[0].id, spotify.LIKED)
        self.assertEqual([(p.name, p.track_count) for p in playlists[1:]], [("Warm-up", 34), ("Empty", 0)])

    def test_playlist_tracks_page_and_map(self):
        src = self.source()
        first = src.tracks(sid(1, "p"), 0, 60)
        self.assertEqual(first.total, 34)
        # 30 tracks + the repeat + the local file; the removed entry and the episode aren't tracks
        self.assertEqual(len(first.items), 32)
        info = first.items[0]
        self.assertEqual((str(info.id), info.title, info.artist, info.duration, info.isrc),
                         (f"spotify:{sid(0)}", "Fake Groove 1", "Test Artist A", 360.0, "QZTEST260000"))
        self.assertEqual(info.availability, Availability.AVAILABLE)
        self.assertEqual(info.features, {})
        by_key = {i.id.key: i for i in first.items}
        self.assertEqual(by_key[sid(2)].availability, Availability.REGION_RESTRICTED)
        self.assertEqual(by_key[sid(4)].availability, Availability.ENTITLEMENT_REQUIRED)
        self.assertIsNone(by_key[sid(3)].isrc)
        local = by_key["spotify:local:Someone:Demo:Bedroom+Tape:200"]
        self.assertEqual(local.availability, Availability.UNAVAILABLE)
        later = src.tracks(sid(1, "p"), 30, 10)
        self.assertEqual(later.offset, 30)

    def test_links_and_uris_name_playlists(self):
        src = self.source()
        pid = sid(1, "p")
        for spelling in (pid, f"spotify:playlist:{pid}", f"https://open.spotify.com/playlist/{pid}?si=abc"):
            self.assertEqual(spotify.parse_id(spelling, "playlist"), pid)
        with self.assertRaises(SourceError):
            src.tracks("not a playlist")

    def test_liked_songs(self):
        page = self.source().tracks(spotify.LIKED, 0, 100)
        self.assertEqual(len(page.items), 12)
        self.assertEqual(page.total, 12)

    def test_info_and_raw(self):
        src = self.source()
        tid = TrackId("spotify", sid(5))
        self.assertEqual(src.info(tid).title, "Fake Groove 6")
        self.assertEqual(src.raw_track(tid)["id"], sid(5))
        with self.assertRaises(NotFound):
            src.info(TrackId("spotify", sid(500)))

    def test_pool_lists_every_track_as_short_of_the_mixing_minimum(self):
        pool = candidate_pool(self.source(), sid(1, "p"), limit=40)
        self.assertEqual(pool.tracks, [])
        reasons = {why for _, why in pool.skipped}
        self.assertIn("unknown bpm and camelot", reasons)
        self.assertIn("region restricted", reasons)

    def test_no_audio_is_ever_read(self):
        src = self.source()
        with self.assertRaises(PolicyDenied):
            src.open_audio(TrackId("spotify", sid(0)))
        anything = Policy.from_matrix({"provider": "spotify", "agreement": {"reference": "test"},
                                       "allow": [op.name for op in Operation]})
        src.policy = anything
        with self.assertRaises(SourceError):
            src.open_audio(TrackId("spotify", sid(0)))


class OlderApiTest(Base):
    renamed_api = False

    def test_falls_back_to_tracks_path_and_track_entries(self):
        src = self.source()
        page = src.tracks(sid(1, "p"), 0, 50)
        self.assertEqual(len(page.items), 32)
        self.assertEqual([p.track_count for p in src.playlists()[1:]], [34, 0])
        paths = [p for _, p, _ in self.fake.requests if "/playlists/" in p]
        self.assertEqual(sum("/items" in p for p in paths), 1)  # tried once, then remembered


class FailureTest(Base):
    def test_rate_limit_retries_then_gives_up(self):
        src = self.source()
        self.fake.fail_next = [429]
        self.assertTrue(src.playlists())
        self.fake.fail_next = [503, 503, 503]
        with self.assertRaises(SourceTimeout):
            src.playlists()

    def test_forbidden_and_missing(self):
        src = self.source()
        self.fake.fail_next = [403]
        with self.assertRaises(Forbidden):
            src.tracks(sid(1, "p"))
        with self.assertRaises(NotFound):
            src.tracks(sid(9, "p"))

    def test_unreachable(self):
        self.sign_in()
        src = spotify.connect(store=self.store, api=f"http://127.0.0.1:{free_port()}/v1", **self.where,
                              sleep=lambda s: None)
        with self.assertRaises(SourceTimeout):
            src.playlists()


class CliTest(Base):
    def run_cli(self, *args) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "KeychainStore", lambda: self.store), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(auth, "parse_env", lambda path: {}), \
                mock.patch.dict("os.environ", {}, clear=False):
            code = cli.main(["--accounts", self.fake.accounts, "--api", self.fake.api, "--redirect", self.redirect,
                             *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_playlists_tracks_track(self):
        self.sign_in()
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("Test Listener", out)
        self.assertIn("BROWSE, METADATA", out)
        code, out, _ = self.run_cli("playlists")
        self.assertIn("Liked Songs", out)
        self.assertIn("Warm-up", out)
        code, out, _ = self.run_cli("tracks", f"https://open.spotify.com/playlist/{sid(1, 'p')}", "--limit", "5")
        self.assertIn("5 of 34 tracks", out)
        code, out, _ = self.run_cli("track", f"spotify:{sid(0)}")
        self.assertIn("QZTEST260000", out)

    def test_errors_go_to_stderr(self):
        code, _, err = self.run_cli("playlists")
        self.assertEqual(code, 1)
        self.assertIn("not signed in", err)


if __name__ == "__main__":
    unittest.main()
