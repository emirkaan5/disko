"""Hermes plugin: control a disko set through disko's local control interface (live/control.py).

disko must be running with control on (`python -m live --control ...` or `--headless`). The
plugin reads the socket and token from ~/.disko, or $DISKO_CONTROL_DIR. When Hermes runs where
that folder isn't visible (a container, another host), set DISKO_CONTROL_URL (http://host:port of
disko's --control-tcp address) and DISKO_CONTROL_TOKEN. "localhost" inside a container is the
container, not the Mac. Music only ever plays on the Mac running disko; nothing is streamed here.

Standalone on purpose: Hermes runs in its own Python, so this file depends on the standard
library alone. Handlers never raise; they return JSON, with {"error": ...} on failure.
"""
import http.client
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlencode, urlparse

TIMEOUT_S = 25.0


def _folder() -> Path:
    return Path(os.environ.get("DISKO_CONTROL_DIR") or Path.home() / ".disko")


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=TIMEOUT_S)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def _request(method: str, path: str, body: dict | None = None) -> str:
    try:
        url = os.environ.get("DISKO_CONTROL_URL")
        if url:
            token = os.environ.get("DISKO_CONTROL_TOKEN")
            if not token:
                return json.dumps({"error": "DISKO_CONTROL_URL is set but DISKO_CONTROL_TOKEN isn't"})
            u = urlparse(url)
            conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=TIMEOUT_S)
        else:
            folder = _folder()
            sock = folder / "control.sock"
            if not sock.exists():
                return json.dumps({"error": "disko isn't running with control on. Start it on the Mac with "
                                            "`python -m live --control` (or `--headless`)."})
            token = (folder / "control.token").read_text().strip()
            conn = _UnixConnection(str(sock))
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"Authorization": f"Bearer {token}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            payload = json.loads(response.read() or b"{}")
        finally:
            conn.close()
        if response.status != 200 and "error" not in payload:
            payload = {"error": f"disko answered {response.status}", **payload}
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:  # never raise into Hermes
        return json.dumps({"error": f"couldn't reach disko: {type(e).__name__}: {e}"})


# ---------------------------------------------------------------- handlers

def status(args: dict, **kwargs) -> str:
    return _request("GET", "/status")


def find_tracks(args: dict, **kwargs) -> str:
    query = {"q": str(args.get("query") or ""), "limit": int(args.get("limit") or 20)}
    return _request("GET", "/tracks?" + urlencode(query))


def start(args: dict, **kwargs) -> str:
    return _request("POST", "/start", {"track": args.get("track") or None})


def set_energy(args: dict, **kwargs) -> str:
    return _request("POST", "/energy", {"energy": args.get("energy")})


def pick_again(args: dict, **kwargs) -> str:
    return _request("POST", "/pick-again", {})


def pause(args: dict, **kwargs) -> str:
    return _request("POST", "/pause", {})


def resume(args: dict, **kwargs) -> str:
    return _request("POST", "/resume", {})


def stop(args: dict, **kwargs) -> str:
    return _request("POST", "/stop", {})


NO_ARGS = {"type": "object", "properties": {}}
TOOLS = [
    ("disko_status", status, {
        "description": "What the disko DJ set is doing now: state, the track playing and what is queued next, the "
                       "energy direction, which music sources the library uses and what disko may do with them, "
                       "and where the music plays. Read this before and after changing anything.",
        "parameters": NO_ARGS}),
    ("disko_find_tracks", find_tracks, {
        "description": "Search the playable tracks in disko's library (title or artist), e.g. to choose an opener.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "words from the title or artist; empty lists the first tracks"},
            "limit": {"type": "integer", "description": "at most this many (1-50, default 20)"}}}}),
    ("disko_start", start, {
        "description": "Start a DJ set on the Mac running disko; Kev, its AI DJ, picks and mixes every track after "
                       "the first. Plays out loud on that Mac. Confirm with the user first, and tell them the "
                       "sources and playback location from the reply.",
        "parameters": {"type": "object", "properties": {
            "track": {"type": "string",
                      "description": "opening track: its id or part of its title; omit to open on the calmest track"}}}}),
    ("disko_set_energy", set_energy, {
        "description": "Steer the set's energy: Kev rethinks its next pick in that direction.",
        "parameters": {"type": "object", "properties": {
            "energy": {"type": "string", "enum": ["down", "hold", "up"]}}, "required": ["energy"]}}),
    ("disko_pick_again", pick_again, {
        "description": "Ask Kev for a different next track (drops a queued mix that hasn't started).",
        "parameters": NO_ARGS}),
    ("disko_pause", pause, {"description": "Pause playback.", "parameters": NO_ARGS}),
    ("disko_resume", resume, {"description": "Resume playback (also resumes a stopped set's current track).",
                              "parameters": NO_ARGS}),
    ("disko_stop", stop, {"description": "Stop the set: playback stops and Kev stops picking.", "parameters": NO_ARGS}),
]


def register(ctx):
    for name, handler, schema in TOOLS:
        ctx.register_tool(name=name, toolset="disko", schema={"name": name, **schema}, handler=handler)
    skills = Path(__file__).parent / "skills"
    for child in sorted(skills.iterdir()):
        if child.is_dir() and (child / "SKILL.md").exists():
            ctx.register_skill(child.name, child / "SKILL.md")
