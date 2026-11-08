"""Signing in to Beatport with disko's own client ID: OAuth2 authorization code + PKCE, a loopback
redirect, and every secret kept in the macOS Keychain.

Following the documented v4 flow (https://api.beatport.com/v4/docs/, "OAuth2 Authentication"):

    browser  GET  {api}/auth/o/authorize/?client_id&response_type=code&redirect_uri&state&code_challenge…
    disko    POST {api}/auth/o/token/    grant_type=authorization_code, code, redirect_uri, client_id[, secret]
    disko    POST {api}/auth/o/token/    grant_type=refresh_token, refresh_token, client_id[, secret]
    disko    POST {api}/auth/o/revoke/   token, client_id            (form body: a token never goes in a URL)

The client ID and secret come from the environment or a `.env` file at the repository root
(DISKO_BEATPORT_CLIENT_ID, DISKO_BEATPORT_CLIENT_SECRET; see .env.example), else from the Keychain
(`python -m live.sources.beatport set-client`). The client ID must be the one Beatport issued for disko, registered with the redirect URI below.
The docs page's own client ID, which some community tools copy, is refused (BEATPORT_INTEGRATION_PLAN.md,
Gate 0: "do not reuse another application's credentials").

Tokens live in memory and in the Keychain, never in files, logs, URLs or messages: errors say what
went wrong, not what was sent.
"""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import stat
import sys
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from ..base import AuthExpired, SourceError, SourceTimeout

KEYCHAIN_SERVICE = "disko.beatport"
REFRESH_MARGIN_S = 120      # refresh a token this long before it runs out
LOGIN_TIMEOUT_S = 300
HTTP_TIMEOUT_S = 15

# SHA-256 of client IDs that belong to other applications (the v4 docs page's own, copied by many
# community tools). Hashes, so the repository doesn't redistribute them.
BORROWED_CLIENT_IDS = frozenset({"a4538c20f31a1c93362843ef19159eb08d689fbca509fc5a5a5fc187b1d9c843"})


ENV_FILE = Path(os.environ.get("DISKO_ENV_FILE", Path(__file__).resolve().parents[3] / ".env"))
ENV_CLIENT_ID, ENV_CLIENT_SECRET = "DISKO_BEATPORT_CLIENT_ID", "DISKO_BEATPORT_CLIENT_SECRET"


def read_env_file(path: Path = ENV_FILE) -> dict:
    """KEY=VALUE lines from a .env file (comments, blank lines and `export ` allowed; quotes stripped).
    A missing file is empty. Warns once if the file holds a secret others can read."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    if values.get(ENV_CLIENT_SECRET) and stat.S_IMODE(path.stat().st_mode) & 0o077 and path not in _warned:
        _warned.add(path)
        print(f"disko: {path} holds the Beatport client secret and others can read it; run chmod 600 {path}",
              file=sys.stderr)
    return values


_warned = set()


def env_values() -> dict:
    """The Beatport settings from the .env file, with the process environment taking precedence."""
    values = read_env_file()
    values.update({k: v for k, v in os.environ.items() if k in (ENV_CLIENT_ID, ENV_CLIENT_SECRET)})
    return values


def _setting(name: str, default: str) -> str:
    return os.environ.get(name) or read_env_file().get(name) or default


API = _setting("DISKO_BEATPORT_API", "https://api.beatport.com/v4").rstrip("/")
REDIRECT_URI = _setting("DISKO_BEATPORT_REDIRECT", "http://127.0.0.1:8765/callback")


class NotConfigured(SourceError):
    """Something disko needs before it can sign in is missing; the message says what to do."""


# ---------------------------------------------------------------- secret storage


class MemoryStore:
    """Secrets in a dict: for tests, and nothing else."""

    def __init__(self, items: dict | None = None):
        self.items = dict(items or {})

    def get(self, name: str) -> str | None:
        return self.items.get(name)

    def set(self, name: str, value: str):
        self.items[name] = value

    def delete(self, name: str):
        self.items.pop(name, None)


class KeychainStore:
    """Generic passwords in the login Keychain under one service name, through /usr/bin/security.

    Values are written through `security -i`'s stdin (hex-encoded with -X), so they never appear in
    a process's command line."""

    def __init__(self, service: str = KEYCHAIN_SERVICE):
        if not service.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"bad Keychain service name {service!r}")
        self.service = service

    @staticmethod
    def _check(name: str):
        if not name.replace("_", "").isalnum():
            raise ValueError(f"bad secret name {name!r}")

    def get(self, name: str) -> str | None:
        self._check(name)
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", name, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None  # not there (exit 44), or the Keychain is locked and the listener said no
        return r.stdout.rstrip("\n")

    def set(self, name: str, value: str):
        self._check(name)
        command = (f"add-generic-password -U -s {self.service} -a {name} -l {self.service}.{name} "
                   f"-X {value.encode().hex()}\n")
        r = subprocess.run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True)
        if r.returncode != 0 or "error" in r.stderr.lower():
            raise SourceError(f"couldn't save {name} in the Keychain ({r.stderr.strip() or r.returncode})")

    def delete(self, name: str):
        self._check(name)
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", name],
                       capture_output=True)


# ---------------------------------------------------------------- credentials and tokens


def borrowed(client_id: str) -> bool:
    return hashlib.sha256(client_id.strip().encode()).hexdigest() in BORROWED_CLIENT_IDS


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str | None = None
    origin: str = field(default="Keychain", compare=False)   # where they were read from

    def __repr__(self):  # never print the secret
        return f"Credentials(client_id={self.client_id[:4]}…, secret={'set' if self.client_secret else 'none'})"

    def form(self) -> dict:
        return {"client_id": self.client_id, **({"client_secret": self.client_secret} if self.client_secret else {})}

    @classmethod
    def load(cls, store, env: dict | None = None) -> "Credentials":
        """From the environment / .env if it names a client ID (the secret then comes from there too),
        else from the Keychain."""
        env = env_values() if env is None else env
        client_id = (env.get(ENV_CLIENT_ID) or "").strip()
        if client_id:
            secret, origin = (env.get(ENV_CLIENT_SECRET) or "").strip() or None, ".env / environment"
        else:
            client_id = (store.get("client_id") or "").strip()
            secret, origin = (store.get("client_secret") or "").strip() or None, "Keychain"
        if not client_id:
            raise NotConfigured(f"no Beatport client ID: put {ENV_CLIENT_ID}=... in {ENV_FILE} (see .env.example) "
                                f"or run `python -m live.sources.beatport set-client`")
        return cls(client_id, secret, origin)

    def save(self, store):
        store.set("client_id", self.client_id.strip())
        if self.client_secret:
            store.set("client_secret", self.client_secret.strip())
        else:
            store.delete("client_secret")


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
            raise AuthExpired("Beatport's sign-in answered without an access token")
        if str(data.get("token_type", "Bearer")).lower() != "bearer":
            raise AuthExpired(f"unexpected token type {data.get('token_type')!r}")
        expires_in = data.get("expires_in")
        expires_in = float(expires_in) if isinstance(expires_in, (int, float)) and expires_in > 0 else 3600.0
        refresh = data.get("refresh_token") or (previous.refresh_token if previous else None)
        return cls(data["access_token"], refresh, (now or time.time()) + expires_in, str(data.get("scope", "")))

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


# ---------------------------------------------------------------- the OAuth endpoints


def _post_form(url: str, form: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded",
                                          "Accept": "application/json", "User-Agent": "disko"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read() or b"{}")
            why = detail.get("error_description") or detail.get("error") or detail.get("detail") or ""
        except ValueError:
            why = ""
        finally:
            e.close()
        if e.code in (400, 401, 403):
            raise AuthExpired(f"Beatport refused the sign-in ({e.code}{': ' + why if why else ''})") from None
        raise SourceTimeout(f"Beatport's sign-in service failed ({e.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SourceTimeout(f"couldn't reach Beatport's sign-in service ({getattr(e, 'reason', e)})") from None


class OAuth:
    def __init__(self, credentials: Credentials, api: str = API, redirect_uri: str = REDIRECT_URI):
        self.credentials, self.api, self.redirect_uri = credentials, api.rstrip("/"), redirect_uri

    def authorize_url(self, state: str, challenge: str) -> str:
        query = urllib.parse.urlencode({"client_id": self.credentials.client_id, "response_type": "code",
                                        "redirect_uri": self.redirect_uri, "state": state,
                                        "code_challenge": challenge, "code_challenge_method": "S256"})
        return f"{self.api}/auth/o/authorize/?{query}"

    def exchange(self, code: str, verifier: str) -> Tokens:
        data = _post_form(f"{self.api}/auth/o/token/",
                          {**self.credentials.form(), "grant_type": "authorization_code", "code": code,
                           "redirect_uri": self.redirect_uri, "code_verifier": verifier})
        return Tokens.from_response(data)

    def refresh(self, tokens: Tokens) -> Tokens:
        if not tokens.refresh_token:
            raise AuthExpired("the Beatport sign-in expired and can't be renewed: sign in again")
        data = _post_form(f"{self.api}/auth/o/token/",
                          {**self.credentials.form(), "grant_type": "refresh_token",
                           "refresh_token": tokens.refresh_token})
        return Tokens.from_response(data, previous=tokens)

    def revoke(self, tokens: Tokens):
        for token in filter(None, (tokens.refresh_token, tokens.access_token)):
            try:
                _post_form(f"{self.api}/auth/o/revoke/", {**self.credentials.form(), "token": token})
            except SourceError:
                pass  # signing out locally still happens; the server forgets it when the token expires

    def login(self, open_browser=webbrowser.open, timeout: float = LOGIN_TIMEOUT_S) -> Tokens:
        """Open Beatport's sign-in page and wait for it to send the browser back to disko."""
        redirect = urllib.parse.urlparse(self.redirect_uri)
        if redirect.scheme != "http" or redirect.hostname not in ("127.0.0.1", "localhost", "::1") or not redirect.port:
            raise NotConfigured(f"the redirect URI must be http://127.0.0.1:PORT/path on this Mac, not {self.redirect_uri}")
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        result = {}
        done = threading.Event()

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
                self.wfile.write(("<p>Signed in to Beatport. You can close this tab and go back to disko.</p>" if ok
                                  else "<p>The Beatport sign-in didn't go through. Go back to disko.</p>").encode())
                done.set()

        try:
            server = HTTPServer((redirect.hostname, redirect.port), Callback)
        except OSError as e:
            raise NotConfigured(f"can't listen for the sign-in on {redirect.hostname}:{redirect.port} ({e})") from None
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True, name="beatport-login")
        thread.start()
        try:
            open_browser(self.authorize_url(state, challenge))
            if not done.wait(timeout):
                raise AuthExpired("the Beatport sign-in timed out")
        finally:
            server.shutdown()
            server.server_close()
        if result.get("error") or not result.get("code"):
            raise AuthExpired(f"Beatport didn't sign you in ({result.get('error') or 'no code came back'})")
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
                raise AuthExpired("not signed in to Beatport: run `python -m live.sources.beatport login`")
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
        with self._lock:
            if self.tokens is not None:
                self.oauth.revoke(self.tokens)
            self.tokens = None
            self.store.delete("tokens")
