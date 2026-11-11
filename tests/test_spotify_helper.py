"""Setting up Spotify audio from the app (live/sources/spotify/helper.py): what counts as ready, the
build through uv and the helper's own sign-in, with stand-ins for uv and the helper so nothing is
installed and no browser opens.
"""
import ast
import os
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from live.sources.spotify import helper  # noqa: E402


def script(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class HelperTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.venv = self.root / ".venv-spotify"
        self.creds = self.root / "librespot" / "credentials.json"
        self.said = []

    def install(self, *packages):
        site = self.venv / "lib" / "python3.14" / "site-packages"
        (self.venv / "bin").mkdir(parents=True, exist_ok=True)
        (self.venv / "bin" / "python").write_text("")
        for p in packages:
            (site / p).mkdir(parents=True, exist_ok=True)
            (site / p / "__init__.py").write_text("")

    # ------------------------------------------------------------ readiness

    def test_ready_needs_every_package_and_the_sign_in(self):
        python = self.venv / "bin" / "python"
        state = helper.readiness(python, self.creds)
        self.assertEqual((state.environment, state.signed_in, state.ready), (False, False, False))
        self.assertEqual(state.summary, "install the audio helper and sign in to Spotify for audio")
        self.install("librespot")                   # a venv built before ffmpeg came with it
        self.assertFalse(helper.readiness(python, self.creds).environment)
        self.install(*helper.PACKAGES)
        self.assertTrue(helper.readiness(python, self.creds).environment)
        self.creds.parent.mkdir(parents=True)
        self.creds.write_text("{}")
        self.assertTrue(helper.readiness(python, self.creds).ready)

    def test_the_credentials_path_is_the_helper_s_own_default(self):
        tree = ast.parse((ROOT / "live" / "workers" / "spotify_fetch.py").read_text())
        source = next(ast.unparse(n.value) for n in tree.body
                      if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "CREDENTIALS")
        self.assertEqual(eval(source, {"Path": Path}), helper.CREDENTIALS)

    def test_the_requirements_bring_every_package_readiness_looks_for(self):
        text = helper.REQUIREMENTS.read_text().lower().replace("-", "_")
        for package in helper.PACKAGES:
            self.assertIn(package, text)

    # ------------------------------------------------------------ build

    def fake_uv(self) -> Path:
        log = self.root / "uv.log"
        return script(self.root / "uv", f"""
            import pathlib, sys
            with open({str(log)!r}, "a") as f:
                f.write(" ".join(sys.argv[1:]) + "\\n")
            if sys.argv[1] == "venv":
                p = pathlib.Path(sys.argv[2]) / "bin"
                p.mkdir(parents=True)
                (p / "python").write_text("")
            print("Resolved 3 packages")
            sys.exit(int(__import__("os").environ.get("FAKE_UV_EXIT", "0")))
        """)

    def test_build_makes_the_venv_then_installs_the_requirements(self):
        uv = self.fake_uv()
        helper.build(self.said.append, venv=self.venv, requirements=Path("req.txt"), uv=str(uv))
        calls = (self.root / "uv.log").read_text().splitlines()
        self.assertEqual(calls, [f"venv {self.venv} --python {helper.PYTHON_VERSION}",
                                 f"pip install --python {self.venv} -r req.txt"])
        self.assertIn("Resolved 3 packages", self.said)
        helper.build(self.said.append, venv=self.venv, requirements=Path("req.txt"), uv=str(uv))
        self.assertEqual(len((self.root / "uv.log").read_text().splitlines()), 3)   # no second venv

    def test_a_failed_step_says_what_it_printed_last(self):
        uv = self.fake_uv()
        os.environ["FAKE_UV_EXIT"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_UV_EXIT")
        with self.assertRaisesRegex(helper.SetupFailed, "creating the environment failed: Resolved 3 packages"):
            helper.build(venv=self.venv, uv=str(uv))

    def test_no_uv_is_a_message_not_a_traceback(self):
        with unittest.mock.patch.object(helper, "find_uv", return_value=None):
            with self.assertRaisesRegex(helper.SetupFailed, "needs uv"):
                helper.build(venv=self.venv)

    # ------------------------------------------------------------ sign-in

    def fake_helper(self, body: str) -> Path:
        return script(self.root / "spotify_fetch.py", body)

    def test_sign_in_runs_the_helper_s_login_and_needs_its_credentials(self):
        fetch = self.fake_helper("""
            import pathlib, sys
            assert "--login" in sys.argv and "--open-browser" in sys.argv
            print("Open this link in a browser and approve disko:", file=sys.stderr)
            print("https://accounts.spotify.com/authorize?x=1", file=sys.stderr)
            out = pathlib.Path(sys.argv[sys.argv.index("--credentials") + 1])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{}")
        """)
        helper.sign_in(self.said.append, python=Path(sys.executable), helper=fetch, credentials=self.creds)
        self.assertTrue(self.creds.exists())
        self.assertIn("https://accounts.spotify.com/authorize?x=1", self.said)

    def test_a_sign_in_that_keeps_nothing_fails(self):
        fetch = self.fake_helper("pass\n")
        with self.assertRaisesRegex(helper.SetupFailed, "without keeping a sign-in"):
            helper.sign_in(python=Path(sys.executable), helper=fetch, credentials=self.creds)

    def test_a_sign_in_can_be_stopped(self):
        fetch = self.fake_helper("import time\ntime.sleep(30)\n")
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        with self.assertRaisesRegex(helper.SetupFailed, "was stopped"):
            helper.sign_in(cancelled=stop.is_set, python=Path(sys.executable), helper=fetch, credentials=self.creds)

    def test_a_sign_in_nobody_finishes_times_out(self):
        fetch = self.fake_helper("import time\ntime.sleep(30)\n")
        with self.assertRaisesRegex(helper.SetupFailed, "took longer"):
            helper.sign_in(python=Path(sys.executable), helper=fetch, credentials=self.creds, timeout=0.5)


if __name__ == "__main__":
    unittest.main()
