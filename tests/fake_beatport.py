"""A stand-in for Beatport's v4 API on 127.0.0.1: the documented OAuth endpoints and the catalog,
playlist and chart lists disko reads, with synthetic tracks and switchable failures.

Nothing in it comes from Beatport but the paths and the documented error bodies; the track JSON
uses the field names live/sources/beatport/source.py reads. Run it alone to try the CLI against it:

    .venv-live/bin/python tests/fake_beatport.py            # prints the API root and a client ID
    .venv-live/bin/python -m live.sources.beatport --api http://127.0.0.1:PORT/v4 status
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEYS = [(8, "A"), (9, "A"), (8, "B"), (7, "A"), (10, "A"), (5, "A"), (11, "B"), (6, "A"), (12, "A"), (3, "B")]


def make_track(i: int, **overrides) -> dict:
    number, letter = KEYS[i % len(KEYS)]
    track = {"id": 1000 + i, "name": f"Fake Groove {i + 1}", "mix_name": "Original Mix",
             "artists": [{"id": 50 + i, "name": f"Test Artist {chr(65 + i % 26)}"}],
             "bpm": 120 + i % 7, "key": {"id": i, "name": "x", "camelot_number": number, "camelot_letter": letter},
             "genre": {"id": 5, "name": "House"}, "sub_genre": None, "length_ms": 360_000 + i * 1000,
             "is_available_for_streaming": True}
    track.update(overrides)
    return track


class FakeBeatport:
    def __init__(self, client_id: str = "disko-test-client", client_secret: str | None = None,
                 redirect_uris=("http://127.0.0.1:8765/callback",), n_tracks: int = 30, token_ttl: float = 36000):
        self.client_id, self.client_secret = client_id, client_secret
        self.redirect_uris = set(redirect_uris)
        self.token_ttl = token_ttl
        self.tracks = {1000 + i: make_track(i) for i in range(n_tracks)}
        self.tracks[1002]["is_available_for_streaming"] = False
        del self.tracks[1003]["bpm"]
        self.tracks[1004]["key"] = None
        self.hidden = {1005}                      # region-restricted: 404 when asked for directly
        self.playlists = {7: ("Warm-up", list(range(1000, 1000 + n_tracks)) + [1000]),  # lists one track twice
                          8: ("Empty", [])}
        self.charts = {42: ("Fake Top 10", list(range(1000, 1010)))}
        self.codes, self.access, self.refresh = {}, {}, {}
        self.revoked = []
        self.fail_next = []                       # status codes to answer the next API calls with
        self.requests = []                        # (method, path with query, had a bearer header)
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v4"

    def start(self):
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True, name="fake-beatport").start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def expire_access_tokens(self):
        with self.lock:
            for token in self.access:
                self.access[token] = 0

    def revoke_everything(self):
        with self.lock:
            self.access.clear()
            self.refresh.clear()

    def _issue(self) -> dict:
        access, refresh = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        self.access[access] = time.time() + self.token_ttl
        self.refresh[refresh] = True
        return {"access_token": access, "refresh_token": refresh, "expires_in": self.token_ttl,
                "scope": "app:locker user:dj", "token_type": "Bearer"}

    def _client_ok(self, form: dict) -> bool:
        if form.get("client_id") != self.client_id:
            return False
        return self.client_secret is None or form.get("client_secret") == self.client_secret

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code: int, payload, headers=None):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _form(self) -> dict:
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
                bearer = self.headers.get("Authorization", "")
                with fake.lock:
                    fake.requests.append(("GET", self.path, bearer.startswith("Bearer ")))
                if url.path == "/v4/auth/o/authorize/":
                    return self._authorize(q)
                with fake.lock:
                    token = bearer.removeprefix("Bearer ")
                    if fake.access.get(token, 0) < time.time():
                        return self._json(401, {"detail": "Authentication credentials were not provided."})
                    if fake.fail_next:
                        code = fake.fail_next.pop(0)
                        return self._json(code, {"detail": "busy"}, {"Retry-After": "0"} if code == 429 else None)
                self._api(url.path, q)

            def _authorize(self, q):
                if q.get("client_id") != fake.client_id or q.get("redirect_uri") not in fake.redirect_uris:
                    return self._json(400, {"error": "invalid_request"})
                if q.get("response_type") != "code" or q.get("code_challenge_method") != "S256":
                    return self._json(400, {"error": "invalid_request"})
                code = secrets.token_urlsafe(16)
                with fake.lock:
                    fake.codes[code] = (q["redirect_uri"], q.get("code_challenge"))
                where = q["redirect_uri"] + "?" + urllib.parse.urlencode({"code": code, "state": q.get("state", "")})
                self.send_response(302)
                self.send_header("Location", where)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                url = urllib.parse.urlparse(self.path)
                form = self._form()
                with fake.lock:
                    fake.requests.append(("POST", self.path, False))
                    if url.path == "/v4/auth/o/revoke/":
                        if fake._client_ok(form):
                            fake.revoked.append(form.get("token"))
                            fake.access.pop(form.get("token"), None)
                            fake.refresh.pop(form.get("token"), None)
                        return self._json(200, {})
                    if url.path != "/v4/auth/o/token/":
                        return self._json(404, {"detail": "Not found."})
                    if not fake._client_ok(form):
                        return self._json(401, {"error": "invalid_client"})
                    grant = form.get("grant_type")
                    if grant == "authorization_code":
                        redirect, challenge = fake.codes.pop(form.get("code"), (None, None))
                        verifier = form.get("code_verifier", "")
                        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
                        if redirect is None or redirect != form.get("redirect_uri") or expected.decode() != challenge:
                            return self._json(400, {"error": "invalid_grant"})
                        return self._json(200, fake._issue())
                    if grant == "refresh_token":
                        if not fake.refresh.pop(form.get("refresh_token"), None):
                            return self._json(400, {"error": "invalid_grant"})
                        return self._json(200, fake._issue())
                    return self._json(400, {"error": "unsupported_grant_type"})

            def _page(self, items, q):
                page, per_page = int(q.get("page", 1)), int(q.get("per_page", 10))
                if per_page > 100:
                    return self._json(400, {"per_page": ["Ensure this value is less than or equal to 100."]})
                start = (page - 1) * per_page
                return self._json(200, {"results": items[start:start + per_page], "count": len(items),
                                        "page": f"{page}/{-(-len(items) // per_page) or 1}", "per_page": per_page})

            def _api(self, path, q):
                parts = [p for p in path.split("/") if p][1:]  # drop "v4"
                if parts == ["my", "account"]:
                    return self._json(200, {"id": 1, "username": "test-listener"})
                if parts == ["my", "playlists"]:
                    return self._page([{"id": pid, "name": name, "track_count": len(ids)}
                                       for pid, (name, ids) in sorted(fake.playlists.items())], q)
                if len(parts) == 4 and parts[:2] == ["my", "playlists"] and parts[3] == "tracks":
                    entry = fake.playlists.get(int(parts[2])) if parts[2].isdigit() else None
                    if entry is None:
                        return self._json(404, {"detail": "Not found."})
                    return self._page([{"id": 9000 + n, "position": n + 1, "track": fake.tracks[t]}
                                       for n, t in enumerate(entry[1])], q)
                if len(parts) == 4 and parts[:2] == ["catalog", "charts"] and parts[3] == "tracks":
                    entry = fake.charts.get(int(parts[2])) if parts[2].isdigit() else None
                    if entry is None:
                        return self._json(404, {"detail": "Not found."})
                    return self._page([fake.tracks[t] for t in entry[1]], q)
                if len(parts) == 3 and parts[:2] == ["catalog", "tracks"] and parts[2].isdigit():
                    tid = int(parts[2])
                    if tid not in fake.tracks or tid in fake.hidden:
                        return self._json(404, {"detail": "Not found."})
                    return self._json(200, fake.tracks[tid])
                return self._json(404, {"detail": "Not found."})

        return Handler


if __name__ == "__main__":
    fake = FakeBeatport().start()
    print(f"fake Beatport API at {fake.url}")
    print(f"client ID: {fake.client_id} (no secret); redirect URI: http://127.0.0.1:8765/callback")
    print("Ctrl-C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        fake.close()
