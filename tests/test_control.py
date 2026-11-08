"""The control interface and the Hermes plugin, against a real socket and a ticking session."""
import importlib.util
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from live.control import Client, ControlServer, ControlUnavailable
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

    def test_files_are_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder / "control.sock").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.folder / "control.token").st_mode), 0o600)

    def test_token_required(self):
        code, body = self.call(Client(folder=self.folder, token="wrong"), "GET", "/status")
        self.assertEqual(code, 401)
        code, body = self.call(Client(folder=self.folder, token=""), "POST", "/stop", {})
        self.assertEqual(code, 401)

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
        old_token = (self.folder / "control.token").read_text()
        self.server.close()
        self.assertFalse((self.folder / "control.sock").exists())
        with self.assertRaises(ControlUnavailable):
            self.client.request("GET", "/status")
        # a crash leaves the socket file behind: the next start replaces it, with a new token
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(self.folder / "control.sock"))
        s.close()
        self.server = ControlServer(self.session, folder=self.folder).start()
        self.assertNotEqual((self.folder / "control.token").read_text(), old_token)
        self.assertEqual(self.call(Client(folder=self.folder, token=old_token), "GET", "/status")[0], 401)
        self.assertEqual(self.call(self.client, "GET", "/status")[0], 200)

    def test_tcp_needs_the_token_too(self):
        self.server.close()
        self.server = ControlServer(self.session, folder=self.folder, tcp=("127.0.0.1", 0)).start()
        host, port = self.server.tcp_address
        url = f"http://{host}:{port}"
        self.assertEqual(self.call(Client(url=url, token="x"), "GET", "/status")[0], 401)
        self.assertEqual(self.call(Client(url=url, token=self.server.token), "GET", "/status")[0], 200)

    def test_requests_are_serialized(self):
        futures = [self.pool.submit(self.client.request, "POST", "/energy", {"energy": e}) for e in ("up", "down")]
        run_until(self.session, self.engine, lambda: all(f.done() for f in futures), timeout=30)
        self.assertEqual({f.result()[0] for f in futures}, {200})
        self.assertIn(self.session.dj.lift, (-1, 1))


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

    def test_drives_a_running_set(self):
        plugin = self.load()
        with tempfile.TemporaryDirectory() as d:
            session, engine, *_ = fake_session()
            server = ControlServer(session, folder=Path(d)).start()
            os.environ["DISKO_CONTROL_DIR"] = d
            try:
                with ThreadPoolExecutor(1) as pool:
                    def call(fn, args):
                        f = pool.submit(fn, args)
                        run_until(session, engine, f.done, timeout=30)
                        return json.loads(f.result())
                    self.assertEqual(call(plugin.start, {"track": "Test Tone 2"})["starting"]["title"], "Test Tone 2")
                    self.assertTrue(run_until(session, engine, lambda: session.state == "queued"))
                    self.assertEqual(call(plugin.set_energy, {"energy": "up"})["energy"], "lift")
                    self.assertEqual(call(plugin.status, {})["now"]["title"], "Test Tone 2")
                    self.assertEqual(call(plugin.stop, {})["state"], "stopped")
                    self.assertIn("error", call(plugin.pick_again, {}))
            finally:
                del os.environ["DISKO_CONTROL_DIR"]
                server.close()


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
