"""The Spotify adapter against tests/fake_spotify.py: the gate, PKCE sign-in, refresh, paging, the
renamed and older playlist API, mapping, failures, and that tokens stay out of URLs. Plus the audio
side, which needs the listener's own choice and the fetch helper (SPOTIFY_INTEGRATION_PLAN.md,
Milestones 2 and 5), with tests/stub_workers/spotify_fetch.py standing in for librespot. No request
reaches Spotify, and nothing is ever decrypted.
"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from live import __main__ as live_main
from live import indexer
from live.sources import (AuthExpired, Availability, Operation, Policy, PolicyDenied, SourceError, SourceTimeout,
                          TrackId, TrackUnavailable)
from live.sources import spotify
from live.sources.pool import candidate_pool, preview_pool
from live.sources.spotify import __main__ as cli
from live.sources.spotify import auth, fetch
from live.sources.spotify.api import Forbidden, NotFound
from live.sources.spotify.preview import PreviewFetcher
from live.sources.store import SessionTemp, SourceStore

from fake_previews import FakePreviews
from fake_spotify import FakeSpotify, sid

REPO = Path(__file__).resolve().parent.parent
STUBS = Path(__file__).resolve().parent / "stub_workers"
STUB_FETCH = STUBS / "spotify_fetch.py"
STUB_WORKERS = STUBS


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def follow(url: str):
    """Plays the browser: opens the consent link and follows Spotify's redirect back to disko."""
    with urllib.request.urlopen(url, timeout=10) as r:
        r.read()


def setUpModule():
    from helpers import isolate_spotify_consent
    isolate_spotify_consent(unittest.addModuleCleanup)

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

    def test_the_client_id_an_app_carries_is_only_a_fallback(self):
        carried = {"DISKO_SPOTIFY_APP_CLIENT_ID": "release-id"}
        self.assertEqual(auth.client_id(spotify.MemoryStore(), carried), "release-id")
        self.assertEqual(auth.client_id(spotify.MemoryStore({"client_id": "own-id"}), carried), "own-id")
        self.assertEqual(auth.client_id(spotify.MemoryStore(), {**carried, "DISKO_SPOTIFY_CLIENT_ID": "env-id"}),
                         "env-id")

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

    def test_a_playlist_cover_is_the_smallest_image_still_big_enough(self):
        from live.sources.spotify.source import _cover
        big, mid, small = ({"url": f"https://i.scdn.co/image/{w}", "width": w, "height": w} for w in (640, 300, 60))
        self.assertEqual(_cover({"images": [big, mid, small]}), mid["url"])
        self.assertEqual(_cover({"images": [small]}), small["url"])                       # all there is
        self.assertEqual(_cover({"images": [{"url": "https://mosaic.scdn.co/a", "width": None}]}),
                         "https://mosaic.scdn.co/a")                                       # unsized mosaics
        self.assertIsNone(_cover({"images": None}))
        self.assertIsNone(_cover({"images": [{"url": "http://plain.example/a"}]}))        # https only

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

    def test_no_audio_is_ever_read_without_the_listener_turning_it_on(self):
        src = self.source()
        before = list(self.fake.requests)
        with self.assertRaises(PolicyDenied):
            src.open_audio(TrackId("spotify", sid(0)))
        self.assertEqual(self.fake.requests, before)  # refused before Spotify was asked for anything

    def test_a_capability_matrix_cannot_unlock_audio_on_its_own(self):
        """For this provider the recorded agreement is the listener's own choice, so a file in the
        repository can narrow a policy down but never turn audio on (see consent.py)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        matrix = Path(tmp.name) / "capabilities"
        matrix.mkdir()
        (matrix / "spotify.json").write_text(json.dumps({
            "provider": "spotify", "agreement": {"reference": "a file in the repository"},
            "allow": [op.name for op in Operation],
            "retention": {"audio_in_session_temp": True, "analysis_on_disk": True}}))
        consent = spotify.ConsentStore(Path(tmp.name) / "consent.json")
        self.sign_in()
        src = spotify.connect(store=self.store, api=self.fake.api, policy=spotify.load_policy(matrix, consent),
                              **self.where, sleep=lambda s: None)
        with self.assertRaises(PolicyDenied):
            src.open_audio(TrackId("spotify", sid(0)))
        spotify.set_audio(consent, audio=True, when="2026-11-11")
        src.policy = spotify.load_policy(matrix, consent)
        self.assertTrue(src.policy.allows(Operation.DECODE) and src.policy.allows(Operation.TEMP_AUDIO))
        with self.assertRaises(SourceError):  # allowed now, but no fetcher was wired in
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
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.consent = spotify.ConsentStore(Path(self.tmp.name) / "spotify" / "consent.json")

    def run_cli(self, *args) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "KeychainStore", lambda: self.store), \
                mock.patch.object(cli, "ConsentStore", lambda *a, **k: self.consent), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(auth, "parse_env", lambda path: {}), \
                mock.patch.dict("os.environ", {}, clear=False):
            code = cli.main(["--accounts", self.fake.accounts, "--api", self.fake.api, "--redirect", self.redirect,
                             *args])
        return code, out.getvalue(), err.getvalue()

    def test_audio_is_off_until_it_is_turned_on_here(self):
        code, out, _ = self.run_cli("audio")
        self.assertEqual(code, 0)
        self.assertIn("never been turned on", out)
        with mock.patch("builtins.input", lambda prompt="": "not really"):
            code, out, _ = self.run_cli("audio", "on")
        self.assertEqual(code, 1)                          # the warning was not accepted
        self.assertIn("turning on spotify audio means", out.lower())
        self.assertFalse(self.consent.enabled())           # and nothing changed
        code, out, _ = self.run_cli("audio", "on", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.consent.enabled())
        code, out, _ = self.run_cli("audio")
        self.assertIn("Spotify audio is on", out)
        code, out, _ = self.run_cli("status")
        self.assertIn("audio:        on", out)
        code, _, _ = self.run_cli("audio", "off")
        self.assertEqual(code, 0)
        self.assertFalse(self.consent.enabled())
        code, out, _ = self.run_cli("status")
        self.assertIn("off (browsing only)", out)

    def test_logout_takes_the_audio_choice_with_it(self):
        self.sign_in()
        self.run_cli("audio", "on", "--yes")
        code, out, _ = self.run_cli("logout")
        self.assertEqual(code, 0)
        self.assertFalse(self.consent.enabled())
        self.assertIn("audio choice is cleared", out)
        code, out, _ = self.run_cli("audio")
        self.assertIn("never been turned on", out)

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


class AudioChoiceTest(Base):
    """The listener's own choice about audio, and its limits (Milestone 2)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.consent = spotify.ConsentStore(Path(self.tmp.name) / "spotify" / "consent.json")

    def test_audio_is_off_until_the_listener_says_so(self):
        self.assertIsNone(spotify.audio_choice(self.consent))
        policy = spotify.load_policy(settings=self.consent)
        self.assertTrue(policy.allows(Operation.BROWSE))
        for op in (Operation.DECODE, Operation.TEMP_AUDIO, Operation.ANALYZE, Operation.MIX):
            self.assertFalse(policy.allows(op), op)
        self.assertIn("audio is off", policy.reason)

    def test_the_choice_is_dated_and_recorded_as_the_listener_s_own(self):
        choice = spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        self.assertTrue(choice.audio)
        self.assertEqual(choice.date, "2026-11-11")
        self.assertIn("listener's own choice", choice.reference)
        self.assertIn("unofficial", choice.reference)
        policy = spotify.load_policy(settings=self.consent)
        self.assertEqual(policy.agreement["reference"], choice.reference)
        for op in (Operation.DECODE, Operation.ANALYZE, Operation.TEMP_AUDIO, Operation.PERSIST_ANALYSIS,
                   Operation.MIX):
            self.assertTrue(policy.allows(op), op)
        self.assertFalse(policy.allows(Operation.RECORD))
        self.assertFalse(policy.allows(Operation.PERSIST_AUDIO))

    def test_the_choice_is_a_small_file_and_not_a_secret(self):
        spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        self.assertEqual(json.loads(self.consent.path.read_text()),
                         {"version": 1, "audio": True, "date": "2026-11-11"})
        self.assertEqual(self.store.get("audio"), None)  # not in the Keychain
        self.assertIsNone(self.store.get("consent"))
        off = spotify.set_audio(self.consent, audio=False)
        self.assertFalse(off.audio)
        self.assertEqual(off.date, "")                   # a plain 'off' carries no date to quote
        self.assertEqual(json.loads(self.consent.path.read_text())["audio"], False)
        self.assertFalse(spotify.audio_choice(self.consent).audio)
        self.assertIn("browse only", spotify.audio_choice(self.consent).summary)

    def test_no_environment_variable_unlocks_audio(self):
        loud = {"DISKO_SPOTIFY_AUDIO": "on", "DISKO_SPOTIFY_AUDIO_ON": "1", "DISKO_AUDIO": "yes"}
        with mock.patch.dict("os.environ", loud, clear=False):
            policy = spotify.load_policy(settings=self.consent)
        self.assertFalse(policy.allows(Operation.DECODE))

    def test_a_choice_that_cannot_be_read_counts_as_off(self):
        self.consent.path.parent.mkdir(parents=True, exist_ok=True)
        for text in ("", "{}", "[]", '{"audio": "yes"}', "not json at all"):
            self.consent.path.write_text(text)
            self.assertFalse(self.consent.enabled(), text)

    def test_signing_out_clears_the_choice(self):
        spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        self.sign_in()
        spotify.logout(self.store, self.consent)
        self.assertIsNone(self.store.get("tokens"))
        self.assertFalse(self.consent.enabled())
        self.assertFalse(spotify.load_policy(settings=self.consent).allows(Operation.DECODE))

    def test_status_reports_the_choice(self):
        self.sign_in()
        gate = spotify.status(store=self.store, settings=self.consent, env={})
        self.assertFalse(gate.audio)
        self.assertEqual(gate.audio_since, "")
        spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        gate = spotify.status(store=self.store, settings=self.consent, env={})
        self.assertTrue(gate.audio)
        self.assertEqual(gate.audio_since, "2026-11-11")
        self.assertIn("DECODE", gate.allowed)


class FetcherTest(Base):
    """The fetch helper as a process, with tests/stub_workers/spotify_fetch.py in its place
    (Milestone 5): one track at a time, into the session temp folder, nothing kept behind."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.consent = spotify.ConsentStore(self.root / "consent.json")
        spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        self.folder = self.root / "fetch"

    def fetcher(self, **options) -> fetch.Fetcher:
        environ = {**os.environ, "STUB_FETCH_OUT": str(self.root)}
        environ.update(options.pop("environ", {}))
        fetcher = fetch.Fetcher(self.folder, python=sys.executable, helper=STUB_FETCH, environ=environ, **options)
        self.addCleanup(fetcher.close)
        return fetcher

    def audio_source(self, fetcher, **options) -> spotify.SpotifySource:
        self.sign_in()
        return spotify.connect(store=self.store, api=self.fake.api, policy=spotify.load_policy(settings=self.consent),
                               fetcher=fetcher, **{**self.where, **options}, sleep=lambda s: None)

    def asked(self) -> list:
        seen = self.root / "seen.jsonl"
        return [json.loads(line) for line in seen.read_text().splitlines()] if seen.exists() else []

    def test_a_track_is_decoded_through_the_source(self):
        src = self.audio_source(self.fetcher())
        tid = TrackId("spotify", sid(0))
        samples, rate = src.read_audio(tid)
        self.assertEqual(rate, 44100)
        self.assertEqual(samples.shape[1], 2)
        self.assertGreater(len(samples), 44100)
        self.assertEqual(src.info(tid).title, "Fake Groove 1")
        self.assertEqual(len(self.asked()), 1)             # fetched once, and only when it was read
        self.assertEqual([p.name for p in self.folder.glob("*.wav")], [f"{sid(0)}.fetch.wav"])

    def test_the_same_track_is_not_fetched_twice(self):
        fetcher = self.fetcher()
        src = self.audio_source(fetcher)
        tid = TrackId("spotify", sid(0))
        for _ in range(3):
            src.read_audio(tid)
        self.assertEqual(len(self.asked()), 1)

    def test_only_the_last_tracks_stay_on_disk(self):
        fetcher = self.fetcher(keep=2)
        src = self.audio_source(fetcher)
        first = self.folder / f"{sid(0)}.fetch.wav"
        for n in range(3):
            src.read_audio(TrackId("spotify", sid(n)))
            if n == 0:
                self.assertTrue(first.exists())
        self.assertFalse(first.exists())                   # the oldest one goes as the third arrives
        self.assertEqual(len(fetcher.files()), 2)

    def test_a_disconnect_leaves_no_audio_behind(self):
        fetcher = self.fetcher()
        src = self.audio_source(fetcher)
        src.read_audio(TrackId("spotify", sid(0)))
        self.assertEqual(len(self.asked()), 1)
        src.close()
        self.assertEqual(fetcher.files(), [])
        self.assertEqual(list(self.folder.glob("*.wav")), [])

    def test_a_track_that_cannot_be_fetched_is_a_source_error(self):
        src = self.audio_source(self.fetcher())
        with self.assertRaises(TrackUnavailable):
            src.read_audio(TrackId("spotify", "fail" + sid(0)[4:]))
        with self.assertRaises(AuthExpired):
            src.read_audio(TrackId("spotify", "denied" + sid(0)[6:]))
        self.assertEqual(len(self.asked()), 2)

    def test_a_helper_that_stops_is_not_a_crash(self):
        fetcher = self.fetcher(environ={**os.environ, "STUB_DIE": "1"})
        with self.assertRaises(SourceError) as caught:
            fetcher.fetch(TrackId("spotify", sid(0)))
        self.assertIn("helper", str(caught.exception))
        self.assertTrue((self.folder / "fetch.log").exists())

    def test_a_helper_that_stops_answering_times_out(self):
        fetcher = self.fetcher(timeout=1.0)
        with self.assertRaises(SourceTimeout):
            fetcher.fetch(TrackId("spotify", "slow" + sid(0)[4:]))

    def test_a_missing_sign_in_for_the_fetcher_is_reported(self):
        fetcher = self.fetcher(credentials=self.root / "nowhere" / "credentials.json")
        with self.assertRaises(AuthExpired) as caught:
            fetcher.fetch(TrackId("spotify", sid(0)))
        self.assertIn("no sign-in for the fetcher", str(caught.exception))
        self.assertEqual(self.asked(), [])                 # it never got as far as fetching

    def test_the_helper_is_not_started_for_a_refused_track(self):
        src = self.audio_source(self.fetcher())
        spotify.set_audio(self.consent, audio=False)
        src.policy = spotify.load_policy(settings=self.consent)
        with self.assertRaises(PolicyDenied):
            src.read_audio(TrackId("spotify", sid(0)))
        self.assertEqual(self.asked(), [])
        self.assertFalse(self.folder.exists())             # the choice was off: nothing was started at all

    def test_a_track_that_never_gets_a_fetcher_is_reported_plainly(self):
        src = self.audio_source(None)
        with self.assertRaises(SourceError) as caught:
            src.read_audio(TrackId("spotify", sid(0)))
        self.assertIn("fetch helper", str(caught.exception))
        self.assertEqual(self.asked(), [])


class PreviewPoolTest(Base):
    """Milestone 4's done-when: a Spotify playlist reaches a pool with a tempo and a key on every
    track that has a clip, and not one full track is fetched to get there. The clips come from
    tests/fake_previews.py, the analysis from the fast stub, and the fetch helper's stub is watched
    to prove nothing was downloaded in full."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.consent = spotify.ConsentStore(self.root / "consent.json")
        spotify.set_audio(self.consent, audio=True, when="2026-11-11")
        self.clips = FakePreviews().start()
        self.addCleanup(self.clips.stop)
        for i in range(30):
            self.clips.itunes[f"QZTEST26{i:04d}"] = self.clips.url(f"/preview/{i}.mp3")
        self.previews = PreviewFetcher(self.root / "previews", embed=self.clips.url("/embed/track/{}"),
                                       itunes=self.clips.url("/lookup?isrc={}&entity=song"),
                                       deezer=self.clips.url("/track/isrc:{}"), gap=0.0, sleep=lambda s: None)
        for name, value in (("FAST_PYTHON", Path(sys.executable)), ("WORKERS", STUB_WORKERS)):
            patcher = mock.patch.object(indexer, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.temp = SessionTemp(parent=self.root)
        self.addCleanup(self.temp.close)

    def pool(self, progress=None) -> tuple:
        """(source, store, pool): the whole Milestone 4 path over the fake playlist."""
        self.sign_in()
        src = spotify.connect(store=self.store, api=self.fake.api, policy=spotify.load_policy(settings=self.consent),
                              **self.where, sleep=lambda s: None)
        store = SourceStore.for_source(src, self.temp, root=self.root / "sources")
        return src, store, preview_pool(src, sid(1, "p"), self.previews, store, limit=40, progress=progress)

    def test_every_track_with_a_clip_gets_tempo_and_key_without_fetching_a_track(self):
        _, store, pool = self.pool()
        self.assertEqual(len(pool.tracks), 27)             # 28 playable, one of them without an ISRC
        for track in pool.tracks:
            self.assertIsNotNone(track.bpm, track.title)
            self.assertIsNotNone(track.camelot, track.title)
            self.assertTrue(track.provenance["bpm"].startswith("30 s preview (itunes)"), track.title)
        reasons = [why for _, why in pool.skipped]
        self.assertEqual(reasons.count("no preview to analyse"), 1)
        self.assertEqual(reasons.count("region restricted"), 1)
        self.assertEqual(reasons.count("needs another subscription"), 1)
        asked = self.clips.requests
        self.assertEqual(sum("/embed/track/" in r for r in asked), 28)   # the embed page, once per track
        self.assertEqual(sum("/lookup" in r for r in asked), 27)         # then iTunes for the ones with an ISRC
        self.assertEqual(sum("/preview/" in r for r in asked), 27)       # and one download each at most
        self.assertFalse((self.root / "fetch").exists())   # no full track: no fetcher was even started
        self.assertFalse((self.temp.dir / "audio").exists())   # and nothing was decoded
        self.assertEqual(len(list(store.analysis.glob("*.preview.json"))), 27)
        self.assertEqual(list(store.analysis.glob("*.fast.json")), [])   # nothing claims the whole track

    def test_a_second_run_uses_the_clips_and_their_analysis_again(self):
        _, _, first = self.pool()
        downloads = lambda: [r for r in self.clips.requests if "/preview/" in r]
        asked, analyses = len(downloads()), len(list(self.previews.folder.iterdir()))
        _, _, again = self.pool()
        self.assertEqual(len(again.tracks), len(first.tracks))
        self.assertEqual(len(downloads()), asked)      # every clip came from the folder this time
        self.assertEqual(len(list(self.previews.folder.iterdir())), analyses)
        self.assertTrue(all(t.bpm == f.bpm for t, f in zip(again.tracks, first.tracks)))

    def test_the_pool_reports_what_the_window_shows_while_it_says_so(self):
        """The window's progress page reads these events: a start line with the clip count, then one
        message per clip carrying exactly what the feed shows. If this drifts, the page goes blank."""
        events = []
        self.pool(progress=lambda kind, msg: events.append((kind, msg)))
        first = events[0]
        self.assertEqual(first[0], "start")
        self.assertEqual(first[1]["tracks"], 27)
        self.assertEqual(first[1]["todo"], 27)
        done = [msg for kind, msg in events if kind == "done"]
        self.assertEqual(len(done), 27)
        for msg in done:
            self.assertTrue(msg["bpm"] and msg["key"] and msg["arousal"] is not None, msg)
            self.assertTrue(msg.get("title") or msg.get("rel"), msg)   # the artist may be empty



class SamplePoolTest(Base):
    """The pool a source can be built from when it does supply tempo and key: unchanged, and still
    judged on the source's own metadata."""

    def test_beatport_style_tracks_are_still_judged_on_metadata(self):
        pool = candidate_pool(self.source(), sid(1, "p"), limit=40)
        self.assertEqual(pool.tracks, [])
        self.assertIn("unknown bpm and camelot", {why for _, why in pool.skipped})


class NotASetTest(Base):
    """What a Spotify run says when it can't start one: the launch option is refused before anything
    is fetched, and it says how to turn audio on (Milestone 6)."""

    def test_the_launch_options_are_listed(self):
        out = subprocess.run([sys.executable, "-m", "live", "--help"], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("--spotify PLAYLIST", out.stdout)
        self.assertIn("--spotify-limit N", out.stdout)

    def test_audio_off_refuses_a_run_and_names_the_way_to_turn_it_on(self):
        real = spotify.ConsentStore   # the patch replaces the name the function looks up, not the class
        with mock.patch.object(spotify, "ConsentStore", lambda *a, **k: real(self.root / "consent.json")):
            with self.assertRaises(SourceError) as caught:
                live_main.spotify_pool("liked", 10)
        self.assertIn("audio is off", str(caught.exception))
        self.assertIn("audio on", str(caught.exception))
        self.assertEqual(self.fake.requests, [])   # refused before the playlist was even read

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)


if __name__ == "__main__":
    unittest.main()
