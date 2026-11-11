"""A stand-in for the three places a preview can come from, on 127.0.0.1: Spotify's embed page, the
iTunes lookup by ISRC and Deezer's track-by-ISRC call, plus the audio files they point at. It lets one
preview path run through the real urllib opener without touching the internet. Synthetic data only.

    .venv-test/bin/python tests/fake_previews.py      # prints the base url
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUDIO = b"\xff\xfb\x90\x00" + b"a preview, not really audio"
HTML = b"<!doctype html><body>not audio at all</body>"


class FakePreviews:
    """Answers the three preview endpoints from small dicts a test fills in:

        .spotify      track id -> the preview path its embed page points at (absent: no preview)
        .itunes       isrc -> preview url (absent: resultCount 0)
        .deezer       isrc -> preview url (absent: an error body)
        .not_audio    preview paths served as HTML instead of audio (to test the sanity check)
    """

    def __init__(self):
        self.spotify = {}
        self.itunes = {}
        self.deezer = {}
        self.not_audio = set()
        self.requests = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def start(self):
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True,
                         name="fake-previews").start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def url(self, path: str) -> str:
        return self.base + path

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code: int, body: bytes, content_type: str):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, body):
                self._send(200, json.dumps(body).encode(), "application/json")

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(url.query)
                with fake.lock:
                    fake.requests.append(self.path)
                if url.path.startswith("/embed/track/"):
                    return self._embed(url.path.rsplit("/", 1)[1])
                if url.path == "/lookup":
                    return self._itunes((q.get("isrc") or [""])[0])
                if url.path.startswith("/track/isrc:"):
                    return self._deezer(url.path.split("isrc:", 1)[1])
                if url.path.startswith("/preview/"):
                    return self._send(200, HTML if url.path.rsplit("/", 1)[1] in fake.not_audio else AUDIO,
                                      "audio/mpeg")
                return self._send(404, b'{"error": "not found"}', "application/json")

            def _embed(self, key):
                target = fake.spotify.get(key)
                blob = f'"audioPreview":{{"url":"{fake.base}{target}"}}' if target else '"audioPreview":null'
                return self._send(200, f"<!doctype html><script>{blob}</script>".encode(), "text/html")

            def _itunes(self, isrc):
                url = fake.itunes.get(isrc)
                if not url:
                    return self._json({"resultCount": 0, "results": []})
                return self._json({"resultCount": 1, "results": [{"previewUrl": url}]})

            def _deezer(self, isrc):
                url = fake.deezer.get(isrc)
                if not url:
                    return self._json({"error": {"type": "DataException", "message": "no track"}})
                return self._json({"preview": url})

        return Handler


if __name__ == "__main__":
    server = FakePreviews().start()
    print(f"base {server.base}")
    threading.Event().wait()
