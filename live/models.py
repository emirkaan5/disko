"""The DJ's decision model: which System One model makes the calls, and getting it ready from the app.

Three kinds, all speaking the same /v1/systemone protocol (live/kev.py has the wire format):

    kev     Kev-4B on this machine (external/kev; MLX on a Mac, PyTorch on Linux). Free and private, a
            few GB to download once, and the app starts and stops its server itself. The default.
    jev     Jev, hosted by TypeSafe (api.typesafe.ai). Needs an API key and is billed per input token;
            nothing to install, and what the DJ is deciding about is sent to TypeSafe.
    custom  any server that answers /v1/systemone: Laya, a Kev on another machine, your own fine-tune.

The choice is a small file, ~/.disko/model.json ({"kind": "jev", "url": ..., "model": ...}); an API key
goes in the Keychain (service disko.model), never in that file. The welcome screen writes both, and
`make_model()` reads them when a set opens, so a change there applies to the next set.

    model = make_model()          # Kev, or a SystemOne client for Jev / a custom server
    model.start(); model.ask(state, questions); model.choose(state, qid, question)
"""
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path

import httpx

from .host import MAC
from .kev import KEV_DIR, LOG as KEV_LOG, RUN as KEV_RUN, URL as KEV_URL, Kev
from .sources.secrets import KeychainStore
from .steps import Cancelled, Say, SetupFailed, run, stop

SETTINGS = Path.home() / ".disko" / "model.json"
KEYCHAIN_SERVICE = "disko.model"
KINDS = ("kev", "jev", "custom")
JEV_URL = "https://api.typesafe.ai"
JEV_MODEL = "jev-latest"
JEV_KEYS = "https://console.typesafe.ai/keys"
JEV_ENV = "TYPESAFE_API_KEY"           # TypeSafe's own SDKs read this; a key here wins over the Keychain
KEV_ARCHIVE = "https://github.com/jaredpalmer/kev/archive/HEAD.tar.gz"
KEV_REPO = "https://github.com/jaredpalmer/kev.git"
TITLES = {"kev": "Kev", "jev": "Jev", "custom": "Custom server"}


# ------------------------------------------------------------------ the choice


@dataclass(frozen=True)
class ModelChoice:
    kind: str = "kev"
    url: str = ""        # custom only: the server's base URL (Jev's and Kev's are fixed)
    model: str = ""      # the "model" field sent with each request ("" = the kind's default)

    @property
    def title(self) -> str:
        return TITLES.get(self.kind, self.kind)

    @property
    def base_url(self) -> str:
        return {"kev": KEV_URL, "jev": JEV_URL}.get(self.kind, self.url).rstrip("/")

    @property
    def model_name(self) -> str:
        return self.model or {"kev": "kev-latest", "jev": JEV_MODEL}.get(self.kind, "default")


class ModelStore:
    """The stored choice (a JSON file) and API keys (the Keychain). A file that can't be read is Kev."""

    def __init__(self, path: Path = SETTINGS, keys=None):
        self.path = Path(path)
        self._keys = keys

    @property
    def keys(self):
        if self._keys is None:
            self._keys = KeychainStore(KEYCHAIN_SERVICE)
        return self._keys

    def read(self) -> ModelChoice:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return ModelChoice()
        if not isinstance(data, dict) or data.get("kind") not in KINDS:
            return ModelChoice()
        text = lambda k: data.get(k) if isinstance(data.get(k), str) else ""
        return ModelChoice(data["kind"], text("url").strip(), text("model").strip())

    def write(self, choice: ModelChoice) -> ModelChoice:
        if choice.kind not in KINDS:
            raise ValueError(f"unknown model kind {choice.kind!r}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"kind": choice.kind, "url": choice.url, "model": choice.model},
                                        indent=1) + "\n")
        return choice

    def key(self, kind: str) -> str:
        if kind == "jev" and os.environ.get(JEV_ENV, "").strip():
            return os.environ[JEV_ENV].strip()
        try:
            return (self.keys.get(kind) or "").strip()
        except Exception:
            return ""

    def set_key(self, kind: str, value: str):
        value = value.strip()
        if value:
            self.keys.set(kind, value)
        else:
            self.keys.delete(kind)


# ------------------------------------------------------------------ hosted / custom models


class SystemOne:
    """A System One server reached over HTTP: Jev, or any custom one. The same face as live.kev.Kev
    (ping, start, stop, name, info, ask, choose), so the DJ doesn't know which it has."""

    RETRIES = 3          # 429 (rate limited) and 529 (overloaded) are retried with backoff

    def __init__(self, url: str, model: str, key: str = "", title: str = "Model", orders: int = 1,
                 timeout: float = 60):
        self.url, self.model, self.title, self.orders = url.rstrip("/"), model, title, max(1, orders)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self.http = httpx.Client(base_url=self.url, timeout=timeout, headers=headers)
        self.info = None
        self.proc = None

    @property
    def name(self) -> str:
        return f"{self.title} · {self.model}"

    def check(self) -> str:
        """Whether the server answers and takes the key: "" if so, else what went wrong, in words."""
        try:
            r = self.http.get("/v1/models", timeout=8)
        except httpx.HTTPError as e:
            return f"no answer from {self.url} ({type(e).__name__})"
        if r.status_code in (401, 403):
            return "the API key was refused"
        if r.status_code == 404:  # a server with no models list: one tiny question instead
            try:
                self.ask("ping", {"ok": {"type": "choice", "instructions": "Is this a test?",
                                         "criteria": {"yes": None, "no": None}}})
            except Exception as e:
                return f"{self.url} doesn't answer /v1/systemone ({e})"
            self.info = {"name": self.model}
            return ""
        if r.status_code >= 400:
            return f"{self.url} answered {r.status_code}"
        try:
            models = r.json().get("models") or []
        except ValueError:
            return f"{self.url} isn't a System One server (its /v1/models isn't JSON)"
        self.info = next((m for m in models if isinstance(m, dict) and m.get("name") == self.model),
                         models[0] if models and isinstance(models[0], dict) else {"name": self.model})
        return ""

    def ping(self) -> bool:
        return self.info is not None or not self.check()

    def start(self) -> bool:
        problem = self.check()
        if problem:
            raise RuntimeError(f"{self.title} isn't ready: {problem}. Choose the DJ model on the welcome screen.")
        return True

    def stop(self):
        pass  # nothing of ours is running

    def ask(self, state: str, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(self.RETRIES + 1):
            r = self.http.post("/v1/systemone", json=body)
            if r.status_code in (429, 529) and attempt < self.RETRIES:
                wait = r.headers.get("retry-after")
                time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 0.5 * 2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    def choose(self, state: str, qid: str, question: dict, orders: int | None = None) -> tuple:
        """A choice, asked under `self.orders` rotations of its options and averaged (Kev's server does
        this with /v1/systemone/permute; a hosted model is asked once unless set otherwise)."""
        keys = list(question["criteria"])
        n = min(self.orders, len(keys))
        total, start = {k: 0.0 for k in keys}, time.perf_counter()
        for i in range(n):
            order = keys[i:] + keys[:i]
            q = {**question, "criteria": {k: question["criteria"][k] for k in order}}
            probs = self.ask(state, {qid: q})["answers"][qid]["probabilities"]
            for k in keys:
                total[k] += float(probs.get(k, 0.0)) / n
        return total, (time.perf_counter() - start) * 1000


def make_model(store: ModelStore | None = None):
    """The model the listener chose, ready to hand to a set."""
    store = store or ModelStore()
    choice = store.read()
    if choice.kind == "kev":
        return Kev()
    if choice.kind == "jev":
        return SystemOne(JEV_URL, choice.model_name, store.key("jev"), "Jev")
    return SystemOne(choice.base_url, choice.model_name, store.key("custom"), "Custom model", orders=2)


# ------------------------------------------------------------------ Kev on this Mac


@dataclass(frozen=True)
class KevState:
    code: bool         # external/kev holds Kev's code
    environment: bool  # its uv environment is built
    weights: bool      # the run and its base model are in the Hugging Face cache (or can't be checked)

    @property
    def ready(self) -> bool:
        return self.code and self.environment and self.weights

    @property
    def summary(self) -> str:
        if self.ready:
            return "installed"
        left = [what for what, done in (("download Kev", self.code), ("build its environment", self.environment),
                                        ("download its weights (a few GB)", self.weights)) if not done]
        return ", ".join(left[:-1]) + (" and " if len(left) > 1 else "") + left[-1]


def _hub() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"


def cached_snapshot(repo: str, revision: str | None = None, hub: Path | None = None) -> Path | None:
    """A snapshot of a Hub repo in the cache that holds weights (the given commit's, if one is given):
    bin/disko doctor's check, in Python."""
    snaps = (hub or _hub()) / f"models--{repo.replace('/', '--')}" / "snapshots"
    candidates = [snaps / revision] if revision else sorted(snaps.glob("*"))
    return next((s for s in candidates if any(s.glob("*.safetensors"))), None)


def kev_weights(run: str = KEV_RUN, hub: Path | None = None) -> bool:
    if "/" not in run or Path(run).exists() or (KEV_DIR / run).exists():
        return True  # a local checkpoint: nothing in the cache to check
    snap = cached_snapshot(run.split("@")[0], hub=hub)
    if snap is None:
        return False
    try:
        provenance = json.loads((snap / "provenance.json").read_text())
    except (OSError, ValueError):
        return True  # no record of a base model to look for
    base = provenance.get("base")
    return not base or cached_snapshot(base, provenance.get("base_revision"), hub=hub) is not None


def kev_state(kev_dir: Path = KEV_DIR, run: str = KEV_RUN) -> KevState:
    return KevState((kev_dir / "pyproject.toml").exists(), (kev_dir / ".venv" / "bin" / "python").exists(),
                    kev_weights(run))


def _uv() -> str:
    from .sources.spotify.helper import find_uv
    uv = find_uv()
    if not uv:
        raise SetupFailed("installing Kev needs uv: install it from https://docs.astral.sh/uv/ and try again")
    return uv


def _has_git() -> bool:
    """git that works without a dialog: macOS's /usr/bin/git asks to install the developer tools."""
    git = shutil.which("git")
    if not git:
        return False
    if MAC and git == "/usr/bin/git":  # on Linux /usr/bin/git is just git
        return subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True).returncode == 0
    return True


def _download_code(kev_dir: Path, say: Say, cancelled: Cancelled):
    if _has_git():
        run(["git", "clone", "--depth", "1", KEV_REPO, kev_dir], say, cancelled, 600, "downloading Kev")
        return
    say("Downloading Kev's code…")
    with tempfile.TemporaryDirectory(prefix="disko-kev-") as tmp:
        archive = Path(tmp) / "kev.tar.gz"
        try:
            with urllib.request.urlopen(urllib.request.Request(KEV_ARCHIVE, headers={"User-Agent": "disko"}),
                                        timeout=60) as r, open(archive, "wb") as out:
                shutil.copyfileobj(r, out)
            with tarfile.open(archive) as tar:
                tar.extractall(tmp, filter="data")
        except Exception as e:
            raise SetupFailed(f"downloading Kev failed: {e}") from None
        if cancelled():
            raise SetupFailed("downloading Kev was stopped")
        top = next(p for p in Path(tmp).iterdir() if p.is_dir())
        kev_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(top), str(kev_dir))


def _download_weights(kev_dir: Path, run_name: str, say: Say, cancelled: Cancelled, timeout: float):
    """Start Kev's server once with the network on (the app starts it offline), so it fetches its
    weights, and stop it as soon as it answers: scripts/setup.sh --kev-weights, from the app."""
    probe = Kev()
    if probe.ping():
        return  # a server is already answering: its weights are in place
    port = str(urlparse(KEV_URL).port or 8009)
    say("Downloading Kev's weights (a few GB, once)…")
    KEV_LOG.parent.mkdir(exist_ok=True)
    proc = subprocess.Popen([_uv(), "run", "--extra", "serve", "python", "-m", "kev.serve", "--run", run_name,
                             "--port", port], cwd=kev_dir, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
                            env={k: v for k, v in os.environ.items() if k != "HF_HUB_OFFLINE"})
    tail = []

    def read():
        with open(KEV_LOG, "a") as log:
            for line in proc.stdout:
                log.write(line)
                line = line.rstrip().rsplit("\r", 1)[-1].strip()
                if line:
                    tail.append(line)
                    say(line)
    threading.Thread(target=read, daemon=True, name="kev-weights").start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if probe.ping():
                say("Kev answered: its weights are in place.")
                return
            if proc.poll() is not None:
                raise SetupFailed(f"Kev's server stopped before it answered: {tail[-1] if tail else proc.returncode} "
                                  f"(log: {KEV_LOG})")
            if cancelled():
                raise SetupFailed("downloading Kev's weights was stopped")
            if time.monotonic() > deadline:
                raise SetupFailed(f"downloading Kev's weights took longer than {timeout / 60:.0f} minutes")
            time.sleep(1)
    finally:
        if proc.poll() is None:
            stop(proc)


def install_kev(say: Say = lambda _: None, cancelled: Cancelled = lambda: False, kev_dir: Path = KEV_DIR,
                run_name: str = KEV_RUN, weights_timeout: float = 3 * 3600) -> KevState:
    """Whatever Kev still needs, in order: its code, its environment, its weights. Safe to run again."""
    state = kev_state(kev_dir, run_name)
    if not state.code:
        if kev_dir.exists() and any(kev_dir.iterdir()):
            raise SetupFailed(f"{kev_dir} exists but isn't a Kev checkout: move it aside and try again")
        if kev_dir.exists():
            kev_dir.rmdir()
        _download_code(kev_dir, say, cancelled)
    if not kev_state(kev_dir, run_name).environment:
        say("Building Kev's environment…")
        run([_uv(), "sync", "--extra", "serve"], say, cancelled, 1800, "building Kev's environment", cwd=kev_dir)
    if not kev_state(kev_dir, run_name).weights:
        _download_weights(kev_dir, run_name, say, cancelled, weights_timeout)
    return kev_state(kev_dir, run_name)


def check(choice: ModelChoice, store: ModelStore | None = None) -> str:
    """Whether `choice` can make calls right now: "" if so, else what is missing (blocking: a request)."""
    store = store or ModelStore()
    if choice.kind == "kev":
        state = kev_state()
        return "" if state.ready else f"still to do: {state.summary}"
    if choice.kind == "jev" and not store.key("jev"):
        return "no API key yet"
    if choice.kind == "custom" and not choice.url:
        return "no server address yet"
    return SystemOne(choice.base_url, choice.model_name, store.key(choice.kind), choice.title).check()
