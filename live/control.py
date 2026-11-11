"""Local control of a running set: start, status, pause, resume, stop, energy, pick again
(BEATPORT_INTEGRATION_PLAN.md, Milestone 4).

The interface is small HTTP+JSON over a Unix socket in ~/.disko (or $DISKO_CONTROL_DIR): the
folder is the user's alone (0700) and the socket is 0600. On top of the file modes, every
connection is checked against the kernel's record of who opened it (LOCAL_PEERCRED on macOS,
SO_PEERCRED on Linux): only a process running as the same user is served, and if the kernel
can't say, the request is refused. No token is used on the socket.

Nothing listens on the network unless a TCP address is given explicitly (for a controller in a
container, which can't reach a Unix socket on the Mac). TCP has no peer identity, so there every
request carries a bearer token, written to control.token (0600) only when TCP is on. A fresh token
is made each time disko starts, so a stale one stops working after a restart.

Each start also records where disko is installed (install.json), so tools can find and launch it.

Requests are handled one at a time and every state change runs on the session's owner thread
(Session.call), so two controllers can't interleave half a command.

    GET  /status                      what is playing, what's next, the set so far
    GET  /tracks?q=QUERY&limit=20     playable tracks to start from
    POST /start      {"track": "..."} start the set (Kev picks the opener if no track is given)
    POST /pause  /resume  /stop  /pick-again
    POST /energy     {"energy": "down" | "hold" | "up"}
"""
import hmac
import http.client
import json
import os
import secrets
import socket
import socketserver
import struct
import sys
import threading
from concurrent.futures import TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .session import SessionError

CONTROL_DIR = Path(os.environ.get("DISKO_CONTROL_DIR", Path.home() / ".disko"))
SOCKET_NAME, TOKEN_NAME, INSTALL_NAME = "control.sock", "control.token", "install.json"
MAX_BODY = 4096
MAX_SOCKET_PATH = 103  # macOS limits a Unix socket's path to 104 bytes, the terminating zero included
TIMEOUT_S = 15.0

ROUTES = {  # (method, path) -> (session command, how its arguments come from the request)
    ("GET", "/status"): ("status", lambda q, b: ()),
    ("GET", "/tracks"): ("tracks", lambda q, b: (q.get("q", [""])[0], int(q.get("limit", ["20"])[0]))),
    ("POST", "/start"): ("start", lambda q, b: (b.get("track") or None,)),
    ("POST", "/pause"): ("pause", lambda q, b: ()),
    ("POST", "/resume"): ("resume", lambda q, b: ()),
    ("POST", "/stop"): ("stop", lambda q, b: ()),
    ("POST", "/pick-again"): ("pick_again", lambda q, b: ()),
    ("POST", "/energy"): ("energy", lambda q, b: (b.get("energy"),)),
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "disko-control/1"

    def log_message(self, fmt, *args):  # no per-request logging (and Unix sockets have no client address)
        pass

    def _reply(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str):
        server = self.server.control
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self._reply(413, {"error": "request too large"})
        raw = self.rfile.read(length) if length else b""  # read it all before answering, whatever the answer
        if self.server.local:  # who is on the other end, from the kernel; any Authorization header is ignored
            uid = peer_uid(self.connection)
            if uid is None:
                return self._reply(401, {"error": "can't tell which user is connecting, so the request is refused"})
            if uid != os.getuid():
                return self._reply(401, {"error": "only the user running disko can control it"})
        else:
            sent = self.headers.get("Authorization", "")
            if not hmac.compare_digest(sent.encode(), f"Bearer {server.token}".encode()):
                return self._reply(401, {"error": "missing or wrong token"})
        url = urlparse(self.path)
        route = ROUTES.get((method, url.path))
        if route is None:
            return self._reply(404, {"error": f"no {method} {url.path}"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("the body must be a JSON object")
            command, parse = route
            args = parse(parse_qs(url.query), body)
        except (ValueError, TypeError) as e:
            return self._reply(400, {"error": f"bad request: {e}"})
        try:
            result = server.session.call(command, *args).result(timeout=TIMEOUT_S)
        except SessionError as e:
            return self._reply(409, {"error": str(e)})
        except FutureTimeout:
            return self._reply(504, {"error": "disko didn't answer in time (is its window frozen?)"})
        except Exception as e:  # report it, keep serving
            return self._reply(500, {"error": f"{type(e).__name__}: {e}"})
        self._reply(200, result)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def peer_uid(sock) -> int | None:
    """The effective uid of the process at the other end of a Unix socket, or None if unknown."""
    if sock.family != socket.AF_UNIX:  # level 0 means IPPROTO_IP on other sockets
        return None
    try:
        if sys.platform == "darwin":  # struct xucred: u_int cr_version, uid_t cr_uid, short cr_ngroups, gid_t[16]
            version, uid = struct.unpack_from("=II", sock.getsockopt(0, 0x001, 76))  # SOL_LOCAL, LOCAL_PEERCRED
            return uid if version == 0 else None  # XUCRED_VERSION
        if hasattr(socket, "SO_PEERCRED"):  # struct ucred: pid_t pid, uid_t uid, gid_t gid
            _, uid, _ = struct.unpack("=iIi", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid
    except (OSError, struct.error):
        pass
    return None


class _UnixServer(socketserver.UnixStreamServer):
    local = True  # requests are checked by peer uid

    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)  # BaseHTTPRequestHandler expects a (host, port) client address


class _TCPServer(socketserver.TCPServer):
    local = False  # no peer identity over TCP: requests need the token


class ControlServer:
    def __init__(self, session, folder: Path | None = None, tcp: tuple | None = None):
        self.session = session
        self.folder = Path(folder or CONTROL_DIR)
        self.tcp = tcp
        self.token = secrets.token_urlsafe(32)
        self._servers = []

    @property
    def socket_path(self) -> Path:
        return self.folder / SOCKET_NAME

    def start(self):
        if len(str(self.socket_path).encode()) > MAX_SOCKET_PATH:
            raise RuntimeError(f"the control socket path is too long for macOS ({self.socket_path}); "
                               f"set DISKO_CONTROL_DIR to a shorter folder")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        if self.socket_path.exists():
            if _alive(self.socket_path):
                raise RuntimeError(f"another disko is already listening on {self.socket_path}")
            self.socket_path.unlink()  # left behind by a crash
        old = os.umask(0o177)  # the socket is created 0600
        try:
            unix = _UnixServer(str(self.socket_path), _Handler)
        finally:
            os.umask(old)
        unix.control = self
        self._servers.append(unix)
        try:
            token_file = self.folder / TOKEN_NAME
            if self.tcp is not None:
                tcp = _TCPServer(self.tcp, _Handler)
                tcp.control = self
                self._servers.append(tcp)
                _write_private(token_file, self.token)
            else:
                token_file.unlink(missing_ok=True)  # an older run's: nothing here uses it any more
        except Exception:
            for server in self._servers:
                server.server_close()
            self._servers.clear()
            self.socket_path.unlink(missing_ok=True)
            raise
        for server in self._servers:
            threading.Thread(target=server.serve_forever, daemon=True, name="disko-control").start()
        return self

    @property
    def tcp_address(self) -> tuple | None:
        return self._servers[1].server_address if len(self._servers) > 1 else None

    def close(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        self._servers.clear()
        self.socket_path.unlink(missing_ok=True)
        token_file = self.folder / TOKEN_NAME
        try:
            if token_file.read_text() == self.token:  # don't remove a newer disko's token
                token_file.unlink()
        except OSError:
            pass


def _write_private(path: Path, text: str):
    """Write a 0600 file atomically: readers see the old content or the new, never half of it."""
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def write_install_record(folder: Path | None = None) -> Path | None:
    """Record where this disko lives and which python runs it, for tools that launch it. Never raises:
    a missing record mustn't stop the app."""
    folder = Path(folder or CONTROL_DIR)
    record = {"version": 1, "root": str(Path(__file__).resolve().parent.parent),  # the folder holding live/
              "python": sys.executable}  # not resolved: a venv's python is a symlink to the base one
    try:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / INSTALL_NAME
        _write_private(path, json.dumps(record, indent=2) + "\n")
        return path
    except OSError as e:
        print(f"note: couldn't record the install in {folder} ({e})", file=sys.stderr)
        return None


def _alive(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ---------------------------------------------------------------- client


class ControlUnavailable(Exception):
    """disko isn't running (or its control interface is off)."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Client:
    """Talks to a running disko. Errors come back as (status code, {"error": ...}). Over the local
    socket no token is sent (the server checks the user); over TCP (url=) the token is needed."""

    def __init__(self, folder: Path | None = None, url: str | None = None, token: str | None = None,
                 timeout: float = TIMEOUT_S + 5):
        self.folder = Path(folder or CONTROL_DIR)
        self.url, self._token, self.timeout = url, token, timeout

    def _connection(self):
        if self.url:
            u = urlparse(self.url)
            return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=self.timeout)
        path = self.folder / SOCKET_NAME
        if not path.exists():
            raise ControlUnavailable(f"disko isn't running with control on (no {path})")
        return _UnixConnection(str(path), self.timeout)

    def request(self, method: str, path: str, body: dict | None = None) -> tuple:
        conn = self._connection()  # first, so "not running" wins over "no token"
        headers = {}
        if self.url:
            token = self._token
            if token is None:
                try:
                    token = (self.folder / TOKEN_NAME).read_text().strip()
                except OSError:
                    conn.close()
                    raise ControlUnavailable(f"no control token in {self.folder}") from None
            headers["Authorization"] = f"Bearer {token}"
        try:
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        except (ConnectionRefusedError, FileNotFoundError) as e:
            raise ControlUnavailable(f"disko isn't answering ({e})") from None
        finally:
            conn.close()
