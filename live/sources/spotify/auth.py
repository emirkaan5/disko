"""Signing in to Spotify: OAuth2 authorization code + PKCE through a loopback redirect, with the
listener's own Spotify developer app, and every token kept in the macOS Keychain.

Spotify's documented flow for apps that can't keep a secret
(https://developer.spotify.com/documentation/web-api/tutorials/code-pkce-flow):

    browser  GET  {accounts}/authorize?client_id&response_type=code&redirect_uri&state&scope&code_challenge…
    disko    POST {accounts}/api/token   grant_type=authorization_code, code, redirect_uri, client_id, code_verifier
    disko    POST {accounts}/api/token   grant_type=refresh_token, refresh_token, client_id

The client ID is the listener's own, from a Spotify developer app registered with the redirect URI
below (Spotify accepts loopback redirects only as an IP literal, so 127.0.0.1, not localhost). It
comes from DISKO_SPOTIFY_CLIENT_ID in the environment or .env, else from the Keychain
(`python -m live.sources.spotify set-client`). PKCE needs no client secret, so none is kept.

Spotify has no endpoint to revoke a token: signing out forgets it here, and the listener can remove
disko's access at https://www.spotify.com/account/apps/.
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ..base import AuthExpired, SourceError, SourceTimeout
from ..secrets import KeychainStore as _KeychainStore, MemoryStore, parse_env, pkce

KEYCHAIN_SERVICE = "disko.spotify"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
REFRESH_MARGIN_S = 120
LOGIN_TIMEOUT_S = 300
HTTP_TIMEOUT_S = 15

ENV_FILE = Path(os.environ.get("DISKO_ENV_FILE", Path(__file__).resolve().parents[3] / ".env"))
ENV_CLIENT_ID = "DISKO_SPOTIFY_CLIENT_ID"
# The client ID a release build of disko.app carries (scripts/make_app.sh bakes it into the bundle and
# its launcher sets this). Only a fallback: a listener's own client ID, in .env or the Keychain, wins.
ENV_APP_CLIENT_ID = "DISKO_SPOTIFY_APP_CLIENT_ID"


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or parse_env(ENV_FILE).get(name) or default


ACCOUNTS = _setting("DISKO_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com").rstrip("/")
API = _setting("DISKO_SPOTIFY_API", "https://api.spotify.com/v1").rstrip("/")
REDIRECT_URI = _setting("DISKO_SPOTIFY_REDIRECT", "http://127.0.0.1:8766/callback")


class NotConfigured(SourceError):
    """Something disko needs before it can sign in is missing; the message says what to do."""


class KeychainStore(_KeychainStore):
    def __init__(self, service: str = KEYCHAIN_SERVICE):
        super().__init__(service)


def client_id(store, env: dict | None = None) -> str:
    """The Spotify app client ID: the environment / .env first, then the Keychain (the listener's own),
    then the one a release build of disko.app carries."""
    env = {**parse_env(ENV_FILE), **os.environ} if env is None else env
    value = ((env.get(ENV_CLIENT_ID) or "").strip() or (store.get("client_id") or "").strip()
             or (env.get(ENV_APP_CLIENT_ID) or "").strip())
    if not value:
        raise NotConfigured(f"no Spotify client ID: create an app at https://developer.spotify.com/dashboard "
                            f"with the redirect URI {REDIRECT_URI}, then paste its client ID on disko's Spotify "
                            f"page, put {ENV_CLIENT_ID}=... in {ENV_FILE}, or run "
                            f"`python -m live.sources.spotify set-client`")
    return value


@dataclass
class Tokens:
    access_token: str
    refresh_token: str | None
    expires_at: float          # wall-clock seconds
    scope: str = ""

    def __repr__(self):
        return f"Tokens(expires in {self.expires_at - time.time():.0f} s, scope={self.scope!r})"

    def fresh(self, now: float | None = None) -> bool:
        return (now or time.time()) < self.expires_at - REFRESH_MARGIN_S

    @classmethod
    def from_response(cls, data: dict, now: float | None = None, previous: "Tokens | None" = None) -> "Tokens":
        if not isinstance(data, dict) or not data.get("access_token"):
            raise AuthExpired("Spotify's sign-in answered without an access token")
        if str(data.get("token_type", "Bearer")).lower() != "bearer":
            raise AuthExpired(f"unexpected token type {data.get('token_type')!r}")
        expires_in = data.get("expires_in")
        expires_in = float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600.0
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else None)  # Spotify may rotate it
        scope = str(data.get("scope", previous.scope if previous else ""))
        return cls(data["access_token"], refresh, (now or time.time()) + expires_in, scope)

    def save(self, store):
        store.set("tokens", json.dumps(asdict(self)))

    @classmethod
    def load(cls, store) -> "Tokens | None":
        raw = store.get("tokens")
        if not raw:
            return None
        try:
            return cls(**json.loads(raw))
        except (ValueError, TypeError):
            store.delete("tokens")  # unreadable: sign in again rather than guess
            return None


def _post_form(url: str, form: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json", "User-Agent": "disko"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
            why = detail.get("error_description") or detail.get("error") or ""
        except ValueError:
            why = ""
        finally:
            e.close()
        if e.code in (400, 401, 403):
            raise AuthExpired(f"Spotify refused the sign-in ({e.code}{': ' + why if why else ''})") from None
        raise SourceTimeout(f"Spotify's sign-in service failed ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceTimeout(f"couldn't reach Spotify's sign-in service ({getattr(e, 'reason', e)})") from None


class OAuth:
    def __init__(self, client_id: str, accounts: str = ACCOUNTS, redirect_uri: str = REDIRECT_URI):
        self.client_id, self.accounts, self.redirect_uri = client_id, accounts.rstrip("/"), redirect_uri

    def authorize_url(self, state: str, challenge: str) -> str:
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
                  "state": state, "scope": SCOPES, "code_challenge_method": "S256", "code_challenge": challenge}
        return f"{self.accounts}/authorize?{urllib.parse.urlencode(params)}"

    def exchange(self, code: str, verifier: str) -> Tokens:
        return Tokens.from_response(_post_form(f"{self.accounts}/api/token", {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier}))

    def refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise AuthExpired("the Spotify sign-in expired and can't be renewed: sign in again")
        data = _post_form(f"{self.accounts}/api/token", {"grant_type": "refresh_token",
                                                         "refresh_token": tokens.refresh_token,
                                                         "client_id": self.client_id})
        return Tokens.from_response(data, previous=tokens)

    def login(self, open_browser=webbrowser.open, timeout: float = LOGIN_TIMEOUT_S) -> Tokens:
        """Open Spotify's consent page and wait, on a one-shot loopback server, for the browser to
        come back with the code."""
        redirect = urllib.parse.urlparse(self.redirect_uri)
        if not (redirect.scheme == "http" and redirect.hostname in ("127.0.0.1", "::1") and redirect.port):
            raise NotConfigured(f"the redirect URI must be http://127.0.0.1:PORT/path on this Mac "
                                f"(Spotify doesn't accept 'localhost'), not {self.redirect_uri}")
        state = secrets.token_urlsafe(24)
        verifier, challenge = pkce()
        result, done = {}, threading.Event()

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(url.query)
                if url.path != (redirect.path or "/") or q.get("state", [""])[0] != state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Not a sign-in this disko started.")
                    return
                result.update(code=q.get("code", [None])[0], error=q.get("error", [None])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                ok = result["code"] and not result["error"]
                self.wfile.write(("<p>Signed in to Spotify. You can close this tab and go back to disko.</p>" if ok
                                  else "<p>The Spotify sign-in didn't go through. Go back to disko.</p>").encode())
                done.set()

        try:
            server = HTTPServer((redirect.hostname, redirect.port), Callback)
        except OSError as e:
            raise NotConfigured(f"can't listen for the sign-in on {redirect.hostname}:{redirect.port} ({e})") from None
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True, name="spotify-login").start()
        try:
            open_browser(self.authorize_url(state, challenge))
            if not done.wait(timeout):
                raise AuthExpired("the Spotify sign-in timed out")
        finally:
            server.shutdown()
            server.server_close()
        if result.get("error") or not result.get("code"):
            raise AuthExpired(f"Spotify didn't sign you in ({result.get('error') or 'no code came back'})")
        return self.exchange(result["code"], verifier)


class Session:
    """A signed-in account: hands out a valid access token, refreshing and saving it as needed."""

    def __init__(self, oauth: OAuth, store, tokens: Tokens | None = None, clock=time.time):
        self.oauth, self.store, self.clock = oauth, store, clock
        self.tokens = tokens or Tokens.load(store)
        self._lock = threading.Lock()

    @property
    def signed_in(self) -> bool:
        return self.tokens is not None

    def access_token(self, force_refresh: bool = False) -> str:
        with self._lock:
            if self.tokens is None:
                raise AuthExpired("not signed in to Spotify: run `python -m live.sources.spotify login`")
            if force_refresh or not self.tokens.fresh(self.clock()):
                try:
                    self.tokens = self.oauth.refresh(self.tokens)
                except AuthExpired:
                    self.tokens = None
                    self.store.delete("tokens")
                    raise
                self.tokens.save(self.store)
            return self.tokens.access_token

    def logout(self):
        """Forget the tokens here (Spotify has no revocation endpoint)."""
        with self._lock:
            self.tokens = None
            self.store.delete("tokens")


__all__ = ["ACCOUNTS", "API", "KeychainStore", "MemoryStore", "NotConfigured", "OAuth", "REDIRECT_URI", "SCOPES",
           "Session", "Tokens", "client_id"]
