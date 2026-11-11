"""Stands in for live/workers/spotify_fetch.py in tests: the same protocol (JSON lines in and out),
made-up audio instead of librespot, no network and no Spotify account. It writes the WAV the request
asked for and records every request in the caller's folder, so a test can check what was fetched and
how often.

Conventions a test uses:
- a track id containing "fail" answers {"event": "failed", "kind": "unavailable"}
- a track id containing "denied" answers kind "auth" (a sign-in that no longer works)
- a track id containing "slow" sleeps past the caller's timeout
- --credentials pointing at a file that isn't there makes the helper exit fatal, as the real one does
- STUB_DIE=1 in the environment makes it exit right after saying ready
"""
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

SR = 44100
SECONDS = 6.0


def tone(seconds: float = SECONDS, bpm: float = 124.0) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    x = 0.15 * np.sin(2 * np.pi * 220 * t)
    click_len = int(0.05 * SR)
    k = np.arange(click_len) / SR
    click = 0.6 * np.sin(2 * np.pi * 880 * k) * np.exp(-k * 40)
    for start in (np.arange(0.0, seconds, 60.0 / bpm) * SR).astype(int):
        end = min(start + click_len, n)
        x[start:end] += click[:end - start]
    return np.ascontiguousarray(np.stack([x, x], axis=1), dtype=np.float32)


def main(argv) -> int:
    args = dict(zip(argv[::2], argv[1::2])) if "--credentials" in argv else {}
    credentials = args.get("--credentials")
    say = lambda **m: print(json.dumps(m), flush=True)
    if credentials and not Path(credentials).exists():
        say(event="fatal", error=f"stub: no sign-in for the fetcher at {credentials}")
        return 1
    out_dir = Path(os.environ.get("STUB_FETCH_OUT", "."))
    seen = out_dir / "seen.jsonl"
    say(event="ready", backend="stub")
    if os.environ.get("STUB_DIE"):
        return 1
    for line in sys.stdin:
        if not line.strip():
            continue
        request = json.loads(line)
        track = request.get("track") or ""
        with open(seen, "a") as f:
            f.write(json.dumps({"id": request.get("id"), "track": track, "path": request.get("path")}) + "\n")
        if "denied" in track:
            say(event="failed", id=request.get("id"), kind="auth", error="stub: the sign-in is gone")
            continue
        if "fail" in track:
            say(event="failed", id=request.get("id"), kind="unavailable", error="stub: not playable")
            continue
        if "slow" in track:
            time.sleep(30)
        dest = Path(request["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        sf.write(dest, tone(), SR, subtype="FLOAT")
        say(event="done", id=request.get("id"), path=str(dest), seconds=0.1, bytes=dest.stat().st_size)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
