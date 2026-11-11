"""The DJ's model (live/models.py): the stored choice and its keys, the System One client for Jev and
custom servers against a stand-in server, Kev's readiness from the files it leaves, and its install
steps with stand-ins for uv. Nothing reaches TypeSafe, GitHub or Hugging Face.
"""
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live import models  # noqa: E402
from live.kev import Kev  # noqa: E402
from live.sources.secrets import MemoryStore  # noqa: E402


class FakeServer:
    """/v1/models and /v1/systemone, with a key to check and answers that depend on option order."""

    def __init__(self, key="sk-test", models_list=True, busy=0):
        self.key, self.models_list, self.busy, self.requests = key, models_list, busy, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, body=None, headers=()):
                data = json.dumps(body or {}).encode()
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def authorized(self):
                return not fake.key or self.headers.get("Authorization") == f"Bearer {fake.key}"

            def do_GET(self):
                if self.path != "/v1/models" or not fake.models_list:
                    return self.reply(404, {"error": "not found"})
                if not self.authorized():
                    return self.reply(401, {"error": "bad key"})
                self.reply(200, {"models": [{"name": "jev-latest", "description": "x", "release_date": "2026-09-15"}]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append(body)
                if not self.authorized():
                    return self.reply(401, {"error": "bad key"})
                if fake.busy:
                    fake.busy -= 1
                    return self.reply(429, {"error": "slow down"}, [("retry-after", "0")])
                answers = {}
                for qid, q in body["questions"].items():
                    first = next(iter(q["criteria"]))   # an order-biased model: the first option gets more
                    n = len(q["criteria"])
                    probs = {k: (0.5 if k == first else 0.5 / (n - 1)) for k in q["criteria"]}
                    answers[qid] = {"type": "choice", "choice": first, "probabilities": probs}
                self.reply(200, {"model": body["model"], "answers": answers})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = models.ModelStore(self.root / "model.json", MemoryStore())
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(models.JEV_ENV, None)

    def serve(self, **kw) -> FakeServer:
        server = FakeServer(**kw)
        self.addCleanup(server.close)
        return server


class ChoiceTest(Base):
    def test_no_choice_or_a_broken_file_is_kev(self):
        self.assertEqual(self.store.read(), models.ModelChoice("kev"))
        self.store.path.write_text("{not json")
        self.assertEqual(self.store.read().kind, "kev")
        self.store.path.write_text(json.dumps({"kind": "gpt"}))
        self.assertEqual(self.store.read().kind, "kev")

    def test_a_choice_round_trips_and_keys_stay_out_of_the_file(self):
        self.store.set_key("custom", " sk-secret ")
        self.store.write(models.ModelChoice("custom", "http://box:8010", "laya-en"))
        self.assertEqual(self.store.read(), models.ModelChoice("custom", "http://box:8010", "laya-en"))
        self.assertNotIn("sk-secret", self.store.path.read_text())
        self.assertEqual(self.store.key("custom"), "sk-secret")
        self.store.set_key("custom", "")
        self.assertEqual(self.store.key("custom"), "")

    def test_typesafe_s_own_variable_wins_for_jev(self):
        self.store.set_key("jev", "from-keychain")
        os.environ[models.JEV_ENV] = "from-env"
        self.assertEqual(self.store.key("jev"), "from-env")

    def test_make_model_gives_each_kind_its_client(self):
        self.assertIsInstance(models.make_model(self.store), Kev)
        self.store.set_key("jev", "sk-j")
        self.store.write(models.ModelChoice("jev"))
        jev = models.make_model(self.store)
        self.assertEqual((jev.url, jev.model, jev.orders), (models.JEV_URL, "jev-latest", 1))
        self.assertEqual(jev.http.headers["Authorization"], "Bearer sk-j")
        self.store.write(models.ModelChoice("custom", "http://box:8010/"))
        custom = models.make_model(self.store)
        self.assertEqual((custom.url, custom.model), ("http://box:8010", "default"))


class SystemOneTest(Base):
    def test_check_says_whether_the_key_is_taken(self):
        server = self.serve()
        self.assertEqual(models.SystemOne(server.url, "jev-latest", "sk-test").check(), "")
        self.assertIn("refused", models.SystemOne(server.url, "jev-latest", "wrong").check())
        self.assertIn("no answer", models.SystemOne("http://127.0.0.1:9", "m").check())

    def test_a_server_without_a_models_list_is_asked_a_question_instead(self):
        server = self.serve(key="", models_list=False)
        model = models.SystemOne(server.url, "laya")
        self.assertEqual(model.check(), "")
        self.assertEqual(server.requests[-1]["model"], "laya")

    def test_start_explains_what_to_do_when_the_model_isn_t_ready(self):
        server = self.serve()
        with self.assertRaisesRegex(RuntimeError, "welcome screen"):
            models.SystemOne(server.url, "jev-latest", "wrong", "Jev").start()

    def test_choose_averages_over_rotations_of_the_options(self):
        server = self.serve(key="")
        question = {"type": "choice", "instructions": "?", "criteria": {"a": None, "b": None, "c": None}}
        once, _ = models.SystemOne(server.url, "m").choose("s", "q", question)
        self.assertAlmostEqual(once["a"], 0.5)              # asked once: the order bias stays
        spread, _ = models.SystemOne(server.url, "m", orders=3).choose("s", "q", question)
        for k in "abc":
            self.assertAlmostEqual(spread[k], 1 / 3)       # each option led once: the bias cancels
        self.assertEqual([list(r["questions"]["q"]["criteria"]) for r in server.requests[-3:]],
                         [["a", "b", "c"], ["b", "c", "a"], ["c", "a", "b"]])

    def test_rate_limits_are_waited_out(self):
        server = self.serve(key="", busy=2)
        reply = models.SystemOne(server.url, "m").ask("s", {"q": {"type": "choice", "criteria": {"x": None, "y": None}}})
        self.assertIn("q", reply["answers"])
        self.assertEqual(len(server.requests), 3)

    def test_check_of_a_choice_needs_its_key_or_address_first(self):
        self.assertEqual(models.check(models.ModelChoice("jev"), self.store), "no API key yet")
        self.assertEqual(models.check(models.ModelChoice("custom"), self.store), "no server address yet")
        server = self.serve(key="")
        self.assertEqual(models.check(models.ModelChoice("custom", server.url), self.store), "")


class KevTest(Base):
    def snapshot(self, hub, repo, rev="abc", provenance=None):
        snap = hub / f"models--{repo.replace('/', '--')}" / "snapshots" / rev
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_text("")
        if provenance:
            (snap / "provenance.json").write_text(json.dumps(provenance))
        return snap

    def test_weights_need_the_run_and_the_base_it_names(self):
        hub = self.root / "hub"
        self.assertFalse(models.kev_weights("jaredpalmer/kev-4b", hub))
        self.snapshot(hub, "jaredpalmer/kev-4b", provenance={"base": "Qwen/Base", "base_revision": "1001bb4"})
        self.assertFalse(models.kev_weights("jaredpalmer/kev-4b", hub))
        self.snapshot(hub, "Qwen/Base", rev="other")
        self.assertFalse(models.kev_weights("jaredpalmer/kev-4b", hub))   # the wrong commit of the base
        self.snapshot(hub, "Qwen/Base", rev="1001bb4")
        self.assertTrue(models.kev_weights("jaredpalmer/kev-4b", hub))
        self.assertTrue(models.kev_weights("my-local-run", hub))          # a local checkpoint: can't check

    def test_state_and_what_is_left(self):
        kev = self.root / "kev"
        with mock.patch.object(models, "kev_weights", return_value=False):
            state = models.kev_state(kev)
            self.assertEqual(state.summary, "download Kev, build its environment and download its weights (a few GB)")
            (kev / ".venv" / "bin").mkdir(parents=True)
            (kev / ".venv" / "bin" / "python").write_text("")
            (kev / "pyproject.toml").write_text("")
            self.assertEqual(models.kev_state(kev).summary, "download its weights (a few GB)")
        with mock.patch.object(models, "kev_weights", return_value=True):
            self.assertTrue(models.kev_state(kev).ready)

    def test_install_does_only_what_is_missing_in_order(self):
        kev = self.root / "kev"
        calls = []

        def download(kev_dir, say, cancelled):
            calls.append("code")
            kev_dir.mkdir()
            (kev_dir / "pyproject.toml").write_text("")

        def run(command, say, cancelled, timeout, what, cwd=None, env=None):
            calls.append(" ".join(str(c) for c in command[1:]))
            (Path(cwd) / ".venv" / "bin").mkdir(parents=True)
            (Path(cwd) / ".venv" / "bin" / "python").write_text("")
            return []

        weights = {"done": False}

        def fetch_weights(kev_dir, run_name, say, cancelled, timeout):
            calls.append("weights")
            weights["done"] = True

        with mock.patch.object(models, "_download_code", download), mock.patch.object(models, "run", run), \
                mock.patch.object(models, "_download_weights", fetch_weights), \
                mock.patch.object(models, "_uv", return_value="uv"), \
                mock.patch.object(models, "kev_weights", side_effect=lambda run: weights["done"]):
            state = models.install_kev(kev_dir=kev)
            self.assertTrue(state.ready)
            self.assertEqual(calls, ["code", "sync --extra serve", "weights"])
            calls.clear()
            models.install_kev(kev_dir=kev)
            self.assertEqual(calls, [])                                   # nothing left to do

    def test_a_folder_in_the_way_is_left_alone(self):
        kev = self.root / "kev"
        kev.mkdir()
        (kev / "notes.txt").write_text("mine")
        with mock.patch.object(models, "kev_weights", return_value=False):
            with self.assertRaisesRegex(models.SetupFailed, "isn't a Kev checkout"):
                models.install_kev(kev_dir=kev)
        self.assertTrue((kev / "notes.txt").exists())


if __name__ == "__main__":
    unittest.main()
