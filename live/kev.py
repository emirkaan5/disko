"""Kev, the local System One model (external/kev), served on TypeSafe's /v1/systemone protocol.

The app starts the server itself if none answers, and stops it again on exit.
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

KEV_DIR = Path(__file__).resolve().parent.parent / "external" / "kev"
LOG = Path(__file__).resolve().parent.parent / "results" / "kev_server.log"
URL = os.environ.get("KEV_URL", "http://127.0.0.1:8009")
RUN = os.environ.get("KEV_RUN", "jaredpalmer/kev-4b")


class Kev:
    def __init__(self, url: str = URL):
        self.url = url
        self.http = httpx.Client(base_url=url, timeout=120)
        self.proc = None
        self.info = None  # the server's /v1/models entry once it answers

    @property
    def name(self) -> str:
        if not self.info:
            return "Kev"
        return f"{self.info['run'].split('/')[-1]} · {self.info['backend']} {self.info['dtype']}"

    def ping(self) -> bool:
        try:
            r = self.http.get("/v1/models", timeout=2)
            r.raise_for_status()
            self.info = r.json()["models"][0]
            return True
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return False

    def start(self) -> bool:
        """Make sure a server is up, starting Kev-4B locally if needed (loads in ~30-60 s)."""
        if self.ping():
            return True
        if self.proc is None:
            uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
            LOG.parent.mkdir(exist_ok=True)
            self.proc = subprocess.Popen(
                [uv, "run", "--extra", "serve", "python", "-m", "kev.serve", "--run", RUN,
                 "--port", str(urlparse(self.url).port or 8009)],
                cwd=KEV_DIR, stdout=open(LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True,
                env={**os.environ, "HF_HUB_OFFLINE": "1"})
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.ping():
                return True
            if self.proc.poll() is not None:
                raise RuntimeError(f"Kev's server exited; see {LOG}")
            time.sleep(1)
        raise TimeoutError("Kev's server did not come up within 5 minutes")

    def stop(self):
        """Stop the server only if this app started it."""
        if self.proc and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
        self.proc = None

    def ask(self, state: str, questions: dict) -> dict:
        r = self.http.post("/v1/systemone", json={"model": "kev-latest", "state": state, "questions": questions})
        r.raise_for_status()
        return r.json()

    def choose(self, state: str, qid: str, question: dict, orders: int = 4) -> tuple:
        """A choice asked under several option orders and averaged, since Kev leans on order.
        Returns (probabilities, total latency in ms)."""
        r = self.http.post("/v1/systemone/permute", json={
            "request": {"model": "kev-latest", "state": state, "questions": {qid: question}},
            "question": qid, "n_perm": orders})
        r.raise_for_status()
        runs = r.json()["runs"]
        probs = {k: sum(run["probabilities"][k] for run in runs) / len(runs) for k in question["criteria"]}
        return probs, sum(run["latency_ms"] for run in runs)
