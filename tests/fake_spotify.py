"""A stand-in for Spotify's accounts service and Web API on 127.0.0.1: the PKCE sign-in, the account's
playlists and Liked Songs, catalog tracks, and switchable failures. Synthetic data only.

    .venv-live/bin/python tests/fake_spotify.py      # prints the roots and a client ID
    .venv-live/bin/python -m live.sources.spotify --accounts http://127.0.0.1:PORT --api http://127.0.0.1:PORT/v1 status
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sid(n: int, prefix: str = "t") -> str:
    """A 22-character base62 id, like Spotify's."""
    digits = ""
    while True:
        n, r = divmod(n, 62)
        digits = ALPHABET[r] + digits
        if not n:
            break
    return (prefix + "0" * 22)[:22 - len(digits)] + digits


def make_track(i: int, **overrides) -> dict:
    track = {"type": "track", "id": sid(i), "uri": f"spotify:track:{sid(i)}", "name": f"Fake Groove {i + 1}",
             "artists": [{"id": sid(i, "a"), "name": f"Test Artist {chr(65 + i % 26)}"}],
             "duration_ms": 360_000 + i * 1000, "external_ids": {"isrc": f"QZTEST26{i:04d}"},
             "is_local": False, "is_playable": True}
    track.update(overrides)
    return track


class FakeSpotify:
    def __init__(self, client_id: str = "disko-test-spotify", redirect_uris=("http://127.0.0.1:8766/callback",),
                 n_tracks: int = 30, token_ttl: float = 3600, renamed_api: bool = True):
        self.client_id, self.redirect_uris, self.token_ttl = client_id, set(redirect_uris), token_ttl
        self.renamed_api = renamed_api   # False: only the pre-2026 /playlists/{id}/tracks path and `track` entries
        self.tracks = {sid(i): make_track(i) for i in range(n_tracks)}
        self.tracks[sid(2)].update(is_playable=False, restrictions={"reason": "market"})
        self.tracks[sid(3)].pop("external_ids")
        self.tracks[sid(4)].update(is_playable=False, restrictions={"reason": "product"})
        self.local_file = {"type": "track", "id": None, "uri": "spotify:local:Someone:Demo:Bedroom+Tape:200",
                           "name": "Bedroom Tape", "artists": [{"name": "Someone"}], "duration_ms": 200_000,
                           "is_local": True}
        self.episode = {"type": "episode", "id": sid(99, "e"), "name": "A podcast"}
        ids = list(self.tracks)
        self.playlists = {sid(1, "p"): ("Warm-up", ids + [ids[0], None, "local", "episode"]),
                          sid(2, "p"): ("Empty", [])}
        self.liked = ids[:12]
        self.codes, self.access, self.refresh = {}, {}, {}
        self.fail_next = []       # status codes to answer the next API calls with
        self.requests = []        # (method, path with query, had a bearer header)
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.accounts = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.api = self.accounts + "/v1"

    def start(self):
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True, name="fake-spotify").start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------ tokens

    def _issue(self) -> dict:
        access, refresh = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        self.access[access] = time.time() + self.token_ttl
        self.refresh[refresh] = True
        return {"access_token": access, "token_type": "Bearer", "expires_in": self.token_ttl,
                "refresh_token": refresh, "scope": "playlist-read-private"}

    def expire_all(self):
        with self.lock:
            for k in self.access:
                self.access[k] = 0

    # ------------------------------------------------------------ catalog

    def _entry(self, key):
        if key is None:
            return {"added_at": "2026-01-01T00:00:00Z", "is_local": False, "item": None, "track": None}
        data = self.local_file if key == "local" else self.episode if key == "episode" else self.tracks[key]
        entry = {"added_at": "2026-01-01T00:00:00Z", "is_local": data.get("is_local", False)}
        entry["item" if self.renamed_api else "track"] = data
        return entry

    def _paged(self, items: list, query: dict, cap: int = 50) -> dict:
        offset = int(query.get("offset", ["0"])[0])
        limit = int(query.get("limit", ["20"])[0])
        if not 1 <= limit <= cap:
            return None
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body=None, headers=None):
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(url.query)
                if url.path == "/authorize":
                    return self._authorize(q)
                auth = self.headers.get("Authorization", "")
                with fake.lock:
                    fake.requests.append(("GET", self.path, auth.startswith("Bearer ")))
                    failure = fake.fail_next.pop(0) if fake.fail_next else None
                    valid = fake.access.get(auth.removeprefix("Bearer "), 0) > time.time()
                if failure:
                    return self._send(failure, {"error": {"status": failure, "message": "fake failure"}},
                                      {"Retry-After": "0"} if failure == 429 else None)
                if not valid:
                    return self._send(401, {"error": {"status": 401, "message": "The access token expired"}})
                path = url.path.removeprefix("/v1")
                if path == "/me":
                    return self._send(200, {"id": "tester", "display_name": "Test Listener"})
                if path == "/me/playlists":
                    items = [{"id": k, "name": n, ("items" if fake.renamed_api else "tracks"): {"total": len(t)}}
                             for k, (n, t) in fake.playlists.items()]
                    return self._page(items, q)
                if path == "/me/tracks":
                    return self._page([{"added_at": "2026-01-01T00:00:00Z", "track": fake.tracks[k]}
                                       for k in fake.liked], q)
                parts = path.strip("/").split("/")
                if len(parts) == 3 and parts[0] == "playlists" and parts[2] in ("items", "tracks"):
                    if parts[1] not in fake.playlists or (parts[2] == "items") != fake.renamed_api:
                        return self._send(404, {"error": {"status": 404, "message": "Not found."}})
                    return self._page([fake._entry(k) for k in fake.playlists[parts[1]][1]], q, cap=100)
                if len(parts) == 2 and parts[0] == "tracks":
                    track = fake.tracks.get(parts[1])
                    if track is None:
                        return self._send(404, {"error": {"status": 404, "message": "Not found."}})
                    return self._send(200, track)
                return self._send(404, {"error": {"status": 404, "message": "Service not found"}})

            def _page(self, items, q, cap=50):
                body = fake._paged(items, q, cap)
                if body is None:
                    return self._send(400, {"error": {"status": 400, "message": "Invalid limit"}})
                return self._send(200, body)

            def _authorize(self, q):
                one = lambda k: (q.get(k) or [None])[0]
                if one("client_id") != fake.client_id or one("redirect_uri") not in fake.redirect_uris:
                    return self._send(400, {"error": "INVALID_CLIENT"})
                if one("code_challenge_method") != "S256" or not one("code_challenge"):
                    return self._send(400, {"error": "code_challenge required"})
                code = secrets.token_urlsafe(12)
                with fake.lock:
                    fake.codes[code] = (one("code_challenge"), one("redirect_uri"))
                target = one("redirect_uri") + "?" + urllib.parse.urlencode({"code": code, "state": one("state")})
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in urllib.parse.parse_qs(self.rfile.read(length).decode()).items()}
                with fake.lock:
                    fake.requests.append(("POST", self.path, False))
                if url.path != "/api/token":
                    return self._send(404, {"error": "not_found"})
                if form.get("client_id") != fake.client_id:
                    return self._send(400, {"error": "invalid_client"})
                with fake.lock:
                    if form.get("grant_type") == "authorization_code":
                        challenge, redirect = fake.codes.pop(form.get("code"), (None, None))
                        verifier = form.get("code_verifier", "")
                        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                        if challenge is None or challenge != expected or redirect != form.get("redirect_uri"):
                            return self._send(400, {"error": "invalid_grant", "error_description": "Invalid authorization code"})
                        return self._send(200, fake._issue())
                    if form.get("grant_type") == "refresh_token":
                        if not fake.refresh.get(form.get("refresh_token")):
                            return self._send(400, {"error": "invalid_grant", "error_description": "Refresh token revoked"})
                        body = fake._issue()
                        body.pop("refresh_token")  # Spotify often keeps the old refresh token
                        return self._send(200, body)
                return self._send(400, {"error": "unsupported_grant_type"})

        return Handler


if __name__ == "__main__":
    fake = FakeSpotify().start()
    print(f"accounts {fake.accounts}\napi      {fake.api}\nclient   {fake.client_id}")
    threading.Event().wait()
