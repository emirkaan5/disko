"""Running a setup step from the app: a command whose output the window shows as it goes, that the
listener can stop, and whose failure is a sentence rather than a traceback.

Used by everything the welcome screen installs itself: the Spotify audio helper
(live/sources/spotify/helper.py) and the DJ model (live/models.py).
"""
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

Say = Callable[[str], None]
Cancelled = Callable[[], bool]


class SetupFailed(Exception):
    """A step that didn't finish. The message is written for the listener."""


def run(command: list, say: Say, cancelled: Cancelled, timeout: float, what: str, cwd: Path | None = None,
        env: dict | None = None) -> list:
    """Run one step, passing each line it prints to `say`; stop it if `cancelled()` turns true.
    Returns its output lines; raises SetupFailed if it fails, is stopped or runs out of time."""
    try:
        proc = subprocess.Popen([str(c) for c in command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True,
                                cwd=cwd, env=env)
    except OSError as e:
        raise SetupFailed(f"{what} couldn't start: {e}") from None
    lines = []

    def read():
        for line in proc.stdout:
            # Progress bars redraw with carriage returns: the last state of the line is what counts.
            line = line.rstrip().rsplit("\r", 1)[-1].strip()
            if line:
                lines.append(line)
                say(line)

    reader = threading.Thread(target=read, daemon=True, name="setup-step")
    reader.start()
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancelled() or time.monotonic() > deadline:
            stop(proc)
            reader.join(timeout=1)
            raise SetupFailed(f"{what} was stopped" if cancelled() else f"{what} took longer than {timeout:.0f}s")
        time.sleep(0.1)
    reader.join(timeout=2)
    if proc.returncode != 0:
        detail = lines[-1] if lines else f"exit status {proc.returncode}"
        raise SetupFailed(f"{what} failed: {detail}")
    return lines


def stop(proc: subprocess.Popen):
    """Stop a step's process and anything it started (it runs in a session of its own)."""
    import os
    import signal
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=3)
    except (ProcessLookupError, PermissionError):
        pass
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
