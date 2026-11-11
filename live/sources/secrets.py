"""Where a source's secrets live: the system's secret store (the macOS Keychain; on Linux the Secret
Service, i.e. GNOME Keyring or KWallet, through the keyring package), or a dict in tests, and the
repository's .env file for settings a listener types in once. Shared by every provider that signs in.

Tokens go in a store, never in files, logs, URLs or messages.
"""
import base64
import hashlib
import secrets
import subprocess
import sys
from pathlib import Path

from .base import SourceError


def truthy(value) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def parse_env(path: Path) -> dict:
    """KEY=VALUE lines from a .env file (comments, blank lines and `export ` allowed; quotes stripped).
    A missing file is empty."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key.isidentifier():
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def pkce() -> tuple:
    """(verifier, S256 challenge) for an OAuth2 authorization-code sign-in."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class MemoryStore:
    """Secrets in a dict: for tests, and nothing else."""

    def __init__(self, items: dict | None = None):
        self.items = dict(items or {})

    def get(self, name: str) -> str | None:
        return self.items.get(name)

    def set(self, name: str, value: str):
        self.items[name] = value

    def delete(self, name: str):
        self.items.pop(name, None)


def KeychainStore(service: str):
    """The system's secret store under one service name: the Keychain on macOS, the Secret Service
    elsewhere. (The name is historical: every source asks for a "KeychainStore".)"""
    return MacKeychainStore(service) if sys.platform == "darwin" else SecretServiceStore(service)


def _check_service(service: str):
    if not service.replace(".", "").replace("-", "").isalnum():
        raise ValueError(f"bad secret service name {service!r}")


class SecretServiceStore:
    """Secrets in the desktop's Secret Service (GNOME Keyring, KWallet) through the keyring package.

    There is deliberately no plain-file fallback: without a keyring daemon, signing in fails with a
    message saying so, and nothing is written to disk."""

    HINT = ("no secret store is available: disko keeps tokens in the desktop keyring (GNOME Keyring or "
            "KWallet, through the Secret Service). Start one in this session (on a server: "
            "`gnome-keyring-daemon --unlock`), or use a source that doesn't sign in")

    def __init__(self, service: str):
        _check_service(service)
        self.service = service

    @staticmethod
    def _check(name: str):
        if not name.replace("_", "").isalnum():
            raise ValueError(f"bad secret name {name!r}")

    def _keyring(self):
        try:
            import keyring
            from keyring.backends.fail import Keyring as Fail
        except ImportError:
            raise SourceError(f"{self.HINT} (the keyring package isn't installed: run scripts/setup.sh --upgrade)")
        if isinstance(keyring.get_keyring(), Fail):
            raise SourceError(self.HINT)
        return keyring

    def get(self, name: str) -> str | None:
        self._check(name)
        try:
            return self._keyring().get_password(self.service, name)
        except SourceError:
            return None  # nothing stored is what a missing store means for reading
        except Exception:  # a locked or broken keyring: read as not there, like a refused Keychain
            return None

    def set(self, name: str, value: str):
        self._check(name)
        keyring = self._keyring()
        try:
            keyring.set_password(self.service, name, value)
        except Exception as e:
            raise SourceError(f"couldn't save {name} in the keyring ({type(e).__name__}: {e})")

    def delete(self, name: str):
        self._check(name)
        try:
            self._keyring().delete_password(self.service, name)
        except Exception:
            pass


class MacKeychainStore:
    """Generic passwords in the login Keychain under one service name, through /usr/bin/security.

    Values are written through `security -i`'s stdin (hex-encoded with -X), so they never appear in
    a process's command line."""

    def __init__(self, service: str):
        _check_service(service)
        self.service = service

    @staticmethod
    def _check(name: str):
        if not name.replace("_", "").isalnum():
            raise ValueError(f"bad secret name {name!r}")

    def get(self, name: str) -> str | None:
        self._check(name)
        r = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", self.service, "-a", name, "-w"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None  # not there (exit 44), or the Keychain is locked and the listener said no
        return r.stdout.rstrip("\n")

    def set(self, name: str, value: str):
        self._check(name)
        command = (f"add-generic-password -U -s {self.service} -a {name} -l {self.service}.{name} "
                   f"-X {value.encode().hex()}\n")
        r = subprocess.run(["/usr/bin/security", "-i"], input=command, capture_output=True, text=True)
        if r.returncode != 0 or "error" in r.stderr.lower():
            raise SourceError(f"couldn't save {name} in the Keychain ({r.stderr.strip() or r.returncode})")

    def delete(self, name: str):
        self._check(name)
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", self.service, "-a", name],
                       capture_output=True)
