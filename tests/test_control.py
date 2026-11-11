"""The control interface and the Hermes plugin, against a real socket and a ticking session."""
import importlib.util
import io
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import live
from live.control import Client, ControlServer, ControlUnavailable, peer_uid, write_install_record
from live.sources.pool import candidate_pool
from live.sources.base import Page, Source
from live.sources.fake import FakeSource
from live.sources.policy import Policy

from helpers import fake_session, run_until

PLUGIN = Path(__file__).resolve().parent.parent / "integrations" / "hermes" / "disko" / "__init__.py"


class ControlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "ctl"
        self.session, self.engine, self.src, _ = fake_session()
        self.server = ControlServer(self.session, folder=self.folder).start()
        self.client = Client(folder=self.folder)
        self.pool = ThreadPoolExecutor(2)

    def tearDown(self):
        self.server.close()
        self.pool.shutdown()
        self.tmp.cleanup()

    def call(self, client, *args):
        """Make a request from another thread while this one (the session's owner) keeps ticking."""
        f = self.pool.submit(client.request, *args)
        run_until(self.session, self.engine, f.done, timeout=30)
        return f.result()

    def raw(self, headers=""):
        """A hand-written request on the socket, so the headers are exactly what the test says."""
        def go():
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(str(self.folder / "control.sock"))
                s.sendall(f"GET /status HTTP/1.0\r\n{headers}\r\n".encode())
                data = b""
                while chunk := s.recv(65536):
                    data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            return int(head.split()[1]), json.loads(body)
        f = self.pool.submit(go)
        run_until(self.session, self.engine, f.done, timeout=30)
        return f.result()

    def restart(self, **kwargs):
        self.server.close()
        self.server = ControlServer(self.session, folder=self.folder, **kwargs).start()

    def test_files_are_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder / "control.sock").st_mode), 0o600)

    def test_socket_needs_no_token(self):
        self.assertFalse((self.folder / "control.token").exists())
        self.assertEqual(self.raw()[0], 200)
        self.assertEqual(self.raw("Authorization: Bearer wrong\r\n")[0], 200)  # ignored on the socket
        self.assertEqual(self.raw("Authorization: garbage\x01\r\n")[0], 200)
        self.assertEqual(self.call(Client(folder=self.folder, token="wrong"), "GET", "/status")[0], 200)

    def test_stale_token_is_removed(self):
        self.server.close()
        (self.folder / "control.token").write_text("left by an older disko")
        self.server = ControlServer(self.session, folder=self.folder).start()
        self.assertFalse((self.folder / "control.token").exists())

    def test_peer_uid(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with a, b:
            self.assertEqual(peer_uid(a), os.getuid())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:  # not a Unix socket: unknown
            self.assertIsNone(peer_uid(s))

    def test_other_or_unknown_user_is_refused(self):
        for uid in (None, os.getuid() + 1):
            with mock.patch("live.control.peer_uid", return_value=uid):
                code, body = self.raw()
            self.assertEqual(code, 401)
            self.assertIn("error", body)
        self.assertEqual(self.raw()[0], 200)

    def test_start_status_energy_pause_stop(self):
        code, body = self.call(self.client, "GET", "/status")
        self.assertEqual((code, body["state"]), (200, "start"))
        code, body = self.call(self.client, "POST", "/start", {})
        self.assertEqual(code, 200)
        self.assertIn("playback", body)
        self.assertTrue(run_until(self.session, self.engine, lambda: self.session.state == "queued"))
        code, body = self.call(self.client, "POST", "/energy", {"energy": "down"})
        self.assertEqual((code, body["energy"]), (200, "cool down"))
        code, body = self.call(self.client, "POST", "/pause", {})
        self.assertEqual((code, body["playing"]), (200, False))
        code, body = self.call(self.client, "POST", "/resume", {})
        self.assertEqual((code, body["playing"]), (200, True))
        code, body = self.call(self.client, "POST", "/stop", {})
        self.assertEqual((code, body["state"]), (200, "stopped"))

    def test_errors(self):
        self.assertEqual(self.call(self.client, "GET", "/nope")[0], 404)
        self.assertEqual(self.call(self.client, "POST", "/status", {})[0], 404)
        self.assertEqual(self.call(self.client, "POST", "/energy", {"energy": "sideways"})[0], 409)
        self.assertEqual(self.call(self.client, "POST", "/pause", {})[0], 409)  # nothing playing yet
        self.assertEqual(self.call(self.client, "POST", "/start", {"track": "nothing like this"})[0], 409)
        code, body = self.call(self.client, "GET", "/tracks?q=tone%203&limit=5")
        self.assertEqual((code, [t["title"] for t in body["tracks"]]), (200, ["Test Tone 3"]))

    def test_restart_is_safe(self):
        with self.assertRaises(RuntimeError):  # a second disko can't take over a live socket
            ControlServer(self.session, folder=self.folder).start()
        self.server.close()
        self.assertFalse((self.folder / "control.sock").exists())
        with self.assertRaises(ControlUnavailable):
            self.client.request("GET", "/status")
        # a crash leaves the socket file behind: the next start replaces it
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.folder / "control.sock"))
        s.close()
        self.server = ControlServer(self.session, folder=self.folder).start()
        self.assertEqual(self.call(self.client, "GET", "/status")[0], 200)

    def test_tcp_needs_the_token(self):
        self.restart(tcp=("127.0.0.1", 0))
        token_file = self.folder / "control.token"
        self.assertEqual(token_file.read_text(), self.server.token)
        self.assertEqual(stat.S_IMODE(os.stat(token_file).st_mode), 0o600)
        host, port = self.server.tcp_address
        url = f"http://{host}:{port}"
        self.assertEqual(self.call(Client(url=url, token="x"), "GET", "/status")[0], 401)
        with socket.create_connection((host, port)) as s:  # no Authorization header at all
            s.sendall(b"GET /status HTTP/1.0\r\n\r\n")
            self.assertIn(b" 401 ", s.recv(65536))
        self.assertEqual(self.call(Client(url=url, token=self.server.token), "GET", "/status")[0], 200)
        self.assertEqual(self.call(Client(folder=self.folder, url=url), "GET", "/status")[0], 200)  # from the file
        self.assertEqual(self.call(self.client, "GET", "/status")[0], 200)  # the socket still needs none
        # a restart makes a new token; the old one stops working, and close() leaves a newer run's file alone
        old = self.server.token
        self.restart(tcp=("127.0.0.1", 0))
        host, port = self.server.tcp_address
        self.assertNotEqual(token_file.read_text(), old)
        self.assertEqual(self.call(Client(url=f"http://{host}:{port}", token=old), "GET", "/status")[0], 401)
        token_file.write_text("newer")
        self.server.close()
        self.assertEqual(token_file.read_text(), "newer")
        self.server = ControlServer(self.session, folder=self.folder).start()

    def test_tcp_client_without_a_token(self):
        self.server.close()
        with self.assertRaises(ControlUnavailable):  # not running wins: no socket
            self.client.request("GET", "/status")
        self.server = ControlServer(self.session, folder=self.folder).start()
        with self.assertRaises(ControlUnavailable):  # TCP with no token given and no token file
            Client(folder=self.folder, url="http://127.0.0.1:9").request("GET", "/status")

    def test_requests_are_serialized(self):
        futures = [self.pool.submit(self.client.request, "POST", "/energy", {"energy": e}) for e in ("up", "down")]
        run_until(self.session, self.engine, lambda: all(f.done() for f in futures), timeout=30)
        self.assertEqual({f.result()[0] for f in futures}, {200})
        self.assertIn(self.session.dj.lift, (-1, 1))


class InstallRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name) / "d"

    def tearDown(self):
        self.tmp.cleanup()

    def test_contents_and_mode(self):
        path = write_install_record(self.folder)
        self.assertEqual(path, self.folder / "install.json")
        self.assertEqual(json.loads(path.read_text()), {
            "version": 1, "root": str(Path(live.__file__).resolve().parent.parent), "python": sys.executable})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)
        self.assertEqual(os.listdir(self.folder), ["install.json"])  # no temporary file left over

    def test_written_atomically(self):
        write_install_record(self.folder)
        with mock.patch("live.control.os.replace", wraps=os.replace) as replace:
            write_install_record(self.folder)
        (src, dst), _ = replace.call_args
        self.assertEqual((Path(src).parent, Path(dst)), (self.folder, self.folder / "install.json"))
        self.assertNotEqual(Path(src), Path(dst))

    def test_failure_never_raises(self):
        blocker = Path(self.tmp.name) / "file"
        blocker.write_text("not a folder")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            self.assertIsNone(write_install_record(blocker / "d"))
        self.assertIn("note:", err.getvalue())


class HermesPlugin(unittest.TestCase):
    def load(self):
        spec = importlib.util.spec_from_file_location("disko_hermes_plugin", PLUGIN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_registers_its_tools_and_skill(self):
        plugin = self.load()

        class Ctx:
            tools, skills = {}, {}

            def register_tool(self, name, toolset, schema, handler):
                self.tools[name] = (toolset, schema, handler)

            def register_skill(self, name, path):
                self.skills[name] = path

        ctx = Ctx()
        plugin.register(ctx)
        manifest = (PLUGIN.parent / "plugin.yaml").read_text()
        for name, (toolset, schema, handler) in ctx.tools.items():
            self.assertEqual(toolset, "disko")
            self.assertEqual(schema["name"], name)
            self.assertIn(f"  - {name}\n", manifest)
        self.assertEqual(len(ctx.tools), manifest.count("  - disko_"))
        self.assertTrue(Path(ctx.skills["disko-dj"]).read_text().startswith("---\nname: disko-dj\n"))

    def test_handlers_never_raise(self):
        plugin = self.load()
        with tempfile.TemporaryDirectory() as d:
            os.environ["DISKO_CONTROL_DIR"] = d
            try:
                reply = json.loads(plugin.status({}))
            finally:
                del os.environ["DISKO_CONTROL_DIR"]
        self.assertIn("isn't running", reply["error"])


class Pool(unittest.TestCase):
    def test_bounded_with_reasons(self):
        src = FakeSource(n=30, failures={"fake:t02": "region"}, missing={"fake:t03": ("bpm",)})
        pool = candidate_pool(src, "all", limit=10, page_size=4)
        self.assertEqual(len(pool.tracks) + len(pool.skipped), 10)
        self.assertTrue(pool.truncated)
        reasons = {str(info.id): why for info, why in pool.skipped}
        self.assertEqual(reasons, {"fake:t02": "region restricted", "fake:t03": "unknown bpm"})
        self.assertTrue(all(t.mixable and t.source is src for t in pool.tracks))
        whole = candidate_pool(src, "all", limit=100)
        self.assertFalse(whole.truncated)
        self.assertEqual(len(whole.tracks) + len(whole.skipped), 30)
        with self.assertRaises(ValueError):
            candidate_pool(src, "all", limit=10_000)

    def test_a_source_that_never_ends_is_cut_off(self):
        class Endless(Source):
            provider = "endless"

            def __init__(self):
                super().__init__(Policy("endless", frozenset({__import__("live.sources", fromlist=["Operation"]).Operation.BROWSE}),
                                        agreement={"reference": "test"}))
                self.pages = 0

            def _tracks(self, playlist_id, offset, limit):
                self.pages += 1
                ids = FakeSource(n=1).library_tracks()  # always the same track: duplicates and no total
                from live.sources.base import TrackInfo, TrackId, Availability
                return Page((TrackInfo(TrackId("endless", "x"), "x", availability=Availability.AVAILABLE),), offset, None)

        src = Endless()
        pool = candidate_pool(src, "p", limit=20, page_size=10)
        self.assertLessEqual(src.pages, 3)
        self.assertEqual(len(pool.skipped), 1)  # one unique track, below the minimum


if __name__ == "__main__":
    unittest.main()
