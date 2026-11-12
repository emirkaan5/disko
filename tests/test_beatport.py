"""The Beatport adapter against tests/fake_beatport.py: the gate, sign-in, refresh, revocation,
paging, mapping, failures, and that tokens stay out of URLs. No request reaches Beatport.

DISKO_TEST_KEYCHAIN=1 also runs the Keychain store against the real login Keychain (a throwaway
service name, removed afterwards).
"""
import contextlib
import hashlib
import io
import os
import socket
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

from live.sources import (AuthExpired, Availability, Operation, Policy, PolicyDenied, SourceTimeout, TrackId,
                          TrackUnavailable)
from live.host import SECRET_STORE
from live.sources import beatport
from live.sources.beatport import __main__ as cli
from live.sources.beatport import auth, source as bp_source
from live.sources.beatport.api import NotFound
from live.sources.pool import candidate_pool

from fake_beatport import FakeBeatport

AUTHORIZED = Policy.from_matrix({"provider": "beatport", "agreement": {"reference": "test: synthetic fake server"},
                                 "allow": ["BROWSE", "METADATA", "SEND_TO_MODEL"]})


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def follow(url: str):
    """Plays the browser: opens the sign-in link and follows Beatport's redirect back to disko."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            r.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise


def grab_code(url: str) -> str:
    """Plays the browser for a manual sign-in: opens the authorize link but does not follow the
    redirect, and reads the code out of it, the way the listener copies it out of the address bar."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    try:
        urllib.request.build_opener(NoRedirect).open(url, timeout=10)
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location", "")
        e.close()
        return auth.parse_code(location) or ""
    raise AssertionError("the authorize link didn't redirect")


_no_env = mock.patch.object(auth, "env_values", lambda: {})  # the developer's own .env stays out of these tests


def setUpModule():
    _no_env.start()


def tearDownModule():
    _no_env.stop()


class Tripwire:
    """A secret store that fails the test if anything touches it."""

    def __getattr__(self, name):
        raise AssertionError(f"the Keychain was touched ({name}) before the gate was checked")


class Gate(unittest.TestCase):
    def test_a_restrictive_file_stops_everything_first(self):
        denied = Policy.from_matrix({"provider": "beatport", "agreement": None, "allow": ["BROWSE"]})
        gate = beatport.status(denied, store=Tripwire())
        self.assertFalse(gate.authorized or gate.usable)
        with self.assertRaises(PolicyDenied):
            beatport.connect(denied, store=Tripwire(), api="http://127.0.0.1:9")
        with self.assertRaises(PolicyDenied):
            beatport.login(denied, store=Tripwire(), api="http://127.0.0.1:9")

    def test_status_steps(self):
        store = auth.MemoryStore()
        self.assertFalse(beatport.status(AUTHORIZED, store).configured)
        store.set("client_id", "abc")
        gate = beatport.status(AUTHORIZED, store)
        self.assertEqual((gate.authorized, gate.configured, gate.signed_in), (True, True, False))
        self.assertIn("login", gate.reason)

    def test_borrowed_client_ids_are_refused(self):
        fake_hash = hashlib.sha256(b"someone-elses-app").hexdigest()
        with mock.patch.object(auth, "BORROWED_CLIENT_IDS", frozenset({fake_hash})):
            store = auth.MemoryStore()
            with self.assertRaises(auth.NotConfigured):
                auth.Credentials("someone-elses-app").save(store)
            self.assertEqual(store.items, {})
            store.set("client_id", " someone-elses-app ")  # put there by hand
            with self.assertRaises(auth.NotConfigured):
                auth.Credentials.load(store)
        # and the docs page's real one is on the list
        self.assertEqual(len(auth.BORROWED_CLIENT_IDS), 1)

    def test_borrowed_client_ids_allowed_only_with_the_flag(self):
        fake_hash = hashlib.sha256(b"someone-elses-app").hexdigest()
        with mock.patch.object(auth, "BORROWED_CLIENT_IDS", frozenset({fake_hash})):
            allow = {auth.ENV_ALLOW_BORROWED: "1"}
            creds = auth.Credentials.load(auth.MemoryStore(),
                                          env={"DISKO_BEATPORT_CLIENT_ID": "someone-elses-app", **allow})
            self.assertEqual(creds.client_id, "someone-elses-app")
            store = auth.MemoryStore()
            auth.Credentials("someone-elses-app").save(store, allow_borrowed=True)
            self.assertEqual(store.get("client_id"), "someone-elses-app")

    def test_parse_code_accepts_a_url_or_a_bare_code(self):
        good = {"abc123": "abc123", "  abc123 ": "abc123",
                "https://api.beatport.com/v4/auth/o/post-message/?code=abc123&state=x": "abc123",
                "?code=abc123&state=x": "abc123", "code=abc123&state=x": "abc123"}
        for text, expected in good.items():
            self.assertEqual(auth.parse_code(text), expected)
        for empty in ("", "   ", "https://api.beatport.com/v4/auth/o/post-message/?state=x"):
            self.assertIsNone(auth.parse_code(empty))

    def test_secrets_stay_out_of_reprs(self):
        c = auth.Credentials("client-id-value", "very-secret")
        t = auth.Tokens("access-value", "refresh-value", 0)
        for text in (repr(c), repr(t)):
            for secret in ("very-secret", "access-value", "refresh-value", "client-id-value"):
                self.assertNotIn(secret, text)


class DotEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / ".env"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str, mode: int = 0o600):
        self.path.write_text(text)
        os.chmod(self.path, mode)

    def test_parsing(self):
        self.write("# a comment\n\nexport DISKO_BEATPORT_CLIENT_ID = 'abc123'\nDISKO_BEATPORT_CLIENT_SECRET=\"s e\"\n"
                   "OTHER=plain # trailing comment\nnot a line\n=novalue\n")
        self.assertEqual(auth.read_env_file(self.path), {"DISKO_BEATPORT_CLIENT_ID": "abc123",
                                                         "DISKO_BEATPORT_CLIENT_SECRET": "s e", "OTHER": "plain"})
        self.assertEqual(auth.read_env_file(self.path.with_name("missing")), {})

    def test_env_wins_over_keychain(self):
        store = auth.MemoryStore({"client_id": "from-keychain", "client_secret": "keychain-secret"})
        creds = auth.Credentials.load(store, env={"DISKO_BEATPORT_CLIENT_ID": "from-env"})
        self.assertEqual((creds.client_id, creds.client_secret, creds.origin), ("from-env", None, ".env / environment"))
        creds = auth.Credentials.load(store, env={"DISKO_BEATPORT_CLIENT_ID": " ", "OTHER": "x"})
        self.assertEqual((creds.client_id, creds.client_secret, creds.origin),
                         ("from-keychain", "keychain-secret", SECRET_STORE))
        with self.assertRaises(auth.NotConfigured) as err:
            auth.Credentials.load(auth.MemoryStore(), env={})
        self.assertIn("DISKO_BEATPORT_CLIENT_ID", str(err.exception))

    def test_borrowed_id_in_env_is_refused(self):
        fake_hash = hashlib.sha256(b"someone-elses-app").hexdigest()
        with mock.patch.object(auth, "BORROWED_CLIENT_IDS", frozenset({fake_hash})):
            with self.assertRaises(auth.NotConfigured):
                auth.Credentials.load(auth.MemoryStore(), env={"DISKO_BEATPORT_CLIENT_ID": "someone-elses-app"})

    def test_readable_secret_warns(self):
        self.write("DISKO_BEATPORT_CLIENT_SECRET=x\n", 0o644)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            auth.read_env_file(self.path)
        self.assertIn("chmod 600", err.getvalue())
        self.write("DISKO_BEATPORT_CLIENT_SECRET=x\n", 0o600)
        auth._warned.clear()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            auth.read_env_file(self.path)
        self.assertEqual(err.getvalue(), "")


class WithFake(unittest.TestCase):
    secret = None

    def setUp(self):
        self.redirect = f"http://127.0.0.1:{free_port()}/callback"
        self.fake = FakeBeatport(client_secret=self.secret, redirect_uris=(self.redirect,)).start()
        self.store = auth.MemoryStore()
        auth.Credentials(self.fake.client_id, self.secret).save(self.store)
        self.sleeps = []

    def tearDown(self):
        self.fake.close()

    def sign_in(self):
        return beatport.login(AUTHORIZED, self.store, api=self.fake.url, redirect_uri=self.redirect, open_browser=follow)

    def connect(self):
        return beatport.connect(AUTHORIZED, self.store, api=self.fake.url, redirect_uri=self.redirect,
                                sleep=self.sleeps.append)

    def assert_no_token_in_urls(self):
        tokens = set(self.fake.access) | set(self.fake.refresh) | set(self.fake.revoked)
        for _, path, _ in self.fake.requests:
            for token in tokens:
                self.assertNotIn(token, path)


class SignIn(WithFake):
    def test_login_saves_tokens_and_connect_uses_them(self):
        tokens = self.sign_in()
        self.assertTrue(tokens.fresh())
        self.assertIsNotNone(auth.Tokens.load(self.store))
        self.assertTrue(beatport.status(AUTHORIZED, self.store).usable)
        src = self.connect()
        self.assertEqual(src.account(), {"username": "test-listener"})
        self.assertTrue(all(bearer for method, path, bearer in self.fake.requests
                            if method == "GET" and "/auth/" not in path))
        self.assert_no_token_in_urls()

    def test_client_id_from_env(self):
        store = auth.MemoryStore()  # nothing in the Keychain
        env = {"DISKO_BEATPORT_CLIENT_ID": self.fake.client_id, "DISKO_BEATPORT_CLIENT_SECRET": self.secret or ""}
        with mock.patch.object(auth, "env_values", lambda: env):
            beatport.login(AUTHORIZED, store, api=self.fake.url, redirect_uri=self.redirect, open_browser=follow)
            src = beatport.connect(AUTHORIZED, store, api=self.fake.url, redirect_uri=self.redirect)
            self.assertEqual(len(src.playlists()), 2)
        self.assertEqual(set(store.items), {"tokens"})  # tokens in the Keychain, the client ID only in .env

    def test_connect_needs_a_sign_in(self):
        with self.assertRaises(AuthExpired):
            self.connect()

    def test_wrong_client_or_state_fails(self):
        self.store.set("client_id", "not-the-registered-one")
        with self.assertRaises(Exception):
            self.sign_in()  # the authorize step refuses an unknown client: nothing comes back
        self.assertIsNone(auth.Tokens.load(self.store))

    def test_callback_ignores_other_states(self):
        oauth = auth.OAuth(auth.Credentials.load(self.store), self.fake.url, self.redirect)

        def forged_then_real(url):
            with self.assertRaises(urllib.error.HTTPError) as err:
                follow(self.redirect + "?code=stolen&state=wrong")
            err.exception.close()
            follow(url)

        self.assertTrue(oauth.login(open_browser=forged_then_real, timeout=10).access_token)

    def test_manual_sign_in_reads_the_code_back(self):
        # The docs client's own redirect: no local port to listen on, so the code is pasted back.
        redirect = beatport.docs_redirect_uri(self.fake.url)
        self.fake.redirect_uris.add(redirect)
        seen = {}

        def browser(url):
            seen["url"], seen["code"] = url, grab_code(url)

        tokens = auth.OAuth(auth.Credentials.load(self.store), self.fake.url, redirect).login(
            open_browser=browser, prompt=lambda _: seen["code"], timeout=5)
        self.assertTrue(tokens.access_token)
        self.assertIn("/auth/o/post-message/", redirect)
        self.assertNotIn("code_challenge", seen["url"])       # the manual flow omits PKCE
        self.assertIn(urllib.parse.quote(redirect, safe=""), seen["url"])  # and sends the docs redirect

    def test_login_auto_selects_the_docs_redirect_for_a_borrowed_client(self):
        fake_hash = hashlib.sha256(self.fake.client_id.encode()).hexdigest()
        docs = beatport.docs_redirect_uri(self.fake.url)
        self.fake.redirect_uris.add(docs)
        store = auth.MemoryStore({"client_id": self.fake.client_id})
        if self.secret:
            store.set("client_secret", self.secret)
        seen = {}
        with mock.patch.object(auth, "BORROWED_CLIENT_IDS", frozenset({fake_hash})), \
                mock.patch.object(auth, "env_values", lambda: {auth.ENV_ALLOW_BORROWED: "1"}):
            tokens = beatport.login(AUTHORIZED, store, api=self.fake.url,
                                    open_browser=lambda url: seen.update(code=grab_code(url)),
                                    prompt=lambda _: seen["code"])
        self.assertTrue(tokens.fresh())

    def test_refresh_when_expired_and_signed_out_when_revoked(self):
        self.sign_in()
        src = self.connect()
        old = auth.Tokens.load(self.store).access_token
        self.fake.expire_access_tokens()
        self.assertEqual(len(src.playlists()), 2)  # a 401, a refresh, the same call again
        self.assertNotEqual(auth.Tokens.load(self.store).access_token, old)
        self.fake.revoke_everything()
        with self.assertRaises(AuthExpired):
            src.playlists()
        self.assertIsNone(auth.Tokens.load(self.store))
        self.assert_no_token_in_urls()

    def test_refresh_ahead_of_expiry(self):
        self.sign_in()
        tokens = auth.Tokens.load(self.store)
        tokens.expires_at = 0  # past the margin: refreshed before the call, no 401 needed
        tokens.save(self.store)
        self.connect().playlists()
        self.assertGreater(auth.Tokens.load(self.store).expires_at, 1000)

    def test_logout_revokes(self):
        self.sign_in()
        tokens = auth.Tokens.load(self.store)
        beatport.logout(self.store, api=self.fake.url, redirect_uri=self.redirect)
        self.assertIsNone(auth.Tokens.load(self.store))
        self.assertIn(tokens.refresh_token, self.fake.revoked)
        self.assertIn(tokens.access_token, self.fake.revoked)
        self.assertEqual(self.store.get("client_id"), self.fake.client_id)  # the client ID stays
        self.assert_no_token_in_urls()


class SignInWithSecret(SignIn):
    secret = "s3cret-for-tests"


class Catalog(WithFake):
    def setUp(self):
        super().setUp()
        self.sign_in()
        self.src = self.connect()

    def test_playlists(self):
        self.assertEqual([(p.id, p.name, p.track_count) for p in self.src.playlists()],
                         [("my:7", "Warm-up", 31), ("my:8", "Empty", 0)])

    def test_mapping(self):
        info = self.src.info(TrackId("beatport", "1000"))
        self.assertEqual(info.title, "Fake Groove 1 (Original Mix)")
        self.assertEqual(info.artist, "Test Artist A")
        self.assertEqual(info.features, {"bpm": 120.0, "camelot": "8A"})
        self.assertEqual((info.duration, info.genres, info.availability), (360.0, ("House",), Availability.AVAILABLE))
        self.assertEqual(info.provenance["bpm"], "beatport catalog")
        self.assertNotIn("energy", info.features)  # nothing beyond what the catalog says
        odd = bp_source.track_info({"id": 1, "name": "X", "bpm": 0, "key": {"camelot_number": 13, "camelot_letter": "C"}})
        self.assertEqual((odd.features, odd.availability, odd.artist, odd.duration), ({}, Availability.UNKNOWN, None, None))

    def test_pool_pages_and_skips(self):
        with mock.patch.object(bp_source, "PER_PAGE", 7):  # many Beatport pages, pool pages that straddle them
            pool = candidate_pool(self.src, "my:7", limit=40, page_size=5)
        self.assertEqual(len(pool.tracks), 27)
        self.assertEqual({str(i.id): why for i, why in pool.skipped},
                         {"beatport:1002": "unavailable", "beatport:1003": "unknown bpm",
                          "beatport:1004": "unknown camelot"})
        self.assertFalse(pool.truncated)
        self.assertEqual(len({t.id for t in pool.tracks}), 27)
        self.assertTrue(all(t.uid.startswith("beatport:") and t.source is self.src for t in pool.tracks))
        chart = candidate_pool(self.src, "chart:42", limit=5)
        self.assertEqual((len(chart.tracks) + len(chart.skipped), chart.truncated), (5, True))
        self.assertEqual(candidate_pool(self.src, "my:8", limit=5).tracks, [])

    def test_bad_ids_never_reach_the_api(self):
        before = len(self.fake.requests)
        for bad in ("my:../../x", "nope:1", "my:"):
            with self.assertRaises(beatport.source.SourceError):
                self.src.tracks(bad)
        with self.assertRaises(beatport.source.SourceError):
            self.src.info(TrackId("beatport", "1/../2"))
        self.assertEqual(len(self.fake.requests), before)

    def test_restricted_and_missing(self):
        with self.assertRaises(NotFound):
            self.src.info(TrackId("beatport", "1005"))
        with self.assertRaises(NotFound):
            self.src.tracks("my:99")

    def test_busy_is_retried_then_reported(self):
        self.fake.fail_next = [429, 503]
        self.assertEqual(len(self.src.playlists()), 2)
        self.assertEqual(len(self.sleeps), 2)
        self.fake.fail_next = [500, 500, 500]
        with self.assertRaises(SourceTimeout):
            self.src.playlists()

    def test_default_policy_denies_audio_before_any_request(self):
        before = len(self.fake.requests)
        with self.assertRaises(PolicyDenied):
            self.src.open_audio(TrackId("beatport", "1000"))
        self.assertEqual(len(self.fake.requests), before)  # refused before the network

    def _streaming_source(self):
        streaming = Policy.from_matrix({"provider": "beatport", "agreement": {"reference": "test"},
                                        "allow": ["BROWSE", "METADATA", "DECODE", "ANALYZE", "MIX"]})
        return beatport.BeatportSource(self.src.client, streaming)

    def test_streaming_audio_decodes(self):
        src = self._streaming_source()
        with src.open_audio(TrackId("beatport", "1000")) as handle:
            samples, sr = handle.read()
        self.assertEqual(sr, 44100)
        self.assertEqual(samples.shape[1], 2)          # stereo float32, ready for a deck
        self.assertGreater(len(samples), 1000)

    def test_streaming_without_a_plan_is_reported(self):
        self.fake.streaming = False  # the account holds no Beatport Streaming plan
        src = self._streaming_source()
        with self.assertRaises(TrackUnavailable) as err:
            src.open_audio(TrackId("beatport", "1000"))
        self.assertIn("Streaming plan", str(err.exception))

    def test_disconnect(self):
        self.src.close()
        with self.assertRaises(AuthExpired):
            self.src.playlists()


class CommandLine(WithFake):
    def run_cli(self, *args) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "KeychainStore", lambda: self.store), \
                mock.patch.object(Policy, "load", classmethod(lambda cls, provider, folder=None: AUTHORIZED)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--api", self.fake.url, "--redirect", self.redirect, *args])
        return code, out.getvalue(), err.getvalue()

    def test_status_login_browse_logout(self):
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("signed in:              no", out)
        with mock.patch("webbrowser.open", follow):
            self.assertEqual(self.run_cli("login")[0], 0)
        code, out, _ = self.run_cli("status")
        self.assertIn("account:                test-listener", out)
        self.assertIn("my:7", self.run_cli("playlists")[1])
        code, out, _ = self.run_cli("tracks", "chart:42", "--limit", "10")
        self.assertIn("left out: unavailable", out)
        self.assertIn("7 playable, 3 left out", out)
        self.assertIn('"camelot": "8A"', self.run_cli("track", "1000")[1])
        self.assertIn('"length_ms"', self.run_cli("track", "beatport:1000", "--raw")[1])
        code, _, err = self.run_cli("track", "1005")
        self.assertEqual(code, 1)
        self.assertIn("not available to this account", err)
        self.assertEqual(self.run_cli("logout")[0], 0)
        self.assertIsNone(self.store.get("tokens"))


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("DISKO_TEST_KEYCHAIN") == "1",
                     "set DISKO_TEST_KEYCHAIN=1 to use the real Keychain")
class Keychain(unittest.TestCase):
    def test_round_trip(self):
        store = auth.KeychainStore(f"disko.test.{uuid.uuid4().hex[:8]}")
        try:
            self.assertIsNone(store.get("client_id"))
            value = '{"a": "quotes \' and \\" spaces", "b": 1}'
            store.set("tokens", value)
            self.assertEqual(store.get("tokens"), value)
            store.set("tokens", "second")
            self.assertEqual(store.get("tokens"), "second")
        finally:
            store.delete("tokens")
        self.assertIsNone(store.get("tokens"))


if __name__ == "__main__":
    unittest.main()
