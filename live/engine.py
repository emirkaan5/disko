"""Live playback: the current deck, then a pre-rendered transition, then the next deck.

The audio callback only copies samples. It also logs what it wrote and when, so the UI can show
what is being *heard* (Bluetooth outputs lag by a few hundred ms) rather than what was queued.
"""
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from .audio import SR, Deck
from .mixes import Scheduled


@dataclass
class Snapshot:
    deck: Deck | None          # deck playing (deck A)
    pos: int                   # heard sample in deck A
    sched: Scheduled | None    # the transition being heard, if any
    tpos: int                  # heard sample inside the transition buffer
    playing: bool
    level: float               # recent output peak, 0-1

    @property
    def b_pos(self) -> int | None:
        return None if self.sched is None else self.sched.b_offset + self.tpos


class Engine:
    def __init__(self, audio: bool = True, blocksize: int = 2048):
        self.deck = None
        self.pos = 0
        self.pending = None   # Scheduled waiting for deck A to reach its start
        self.in_mix = False
        self.tpos = 0
        self.playing = False
        self.volume = 0.85
        self.level = 0.0
        self._clock = 0       # samples written since start
        self._log = deque(maxlen=256)  # (clock, deck, pos, sched, tpos) at every run of copied samples
        self._lock = threading.Lock()
        self.stream = None
        if audio:
            self.stream = sd.OutputStream(samplerate=SR, blocksize=blocksize, channels=2, dtype="float32",
                                          latency="high", callback=self._callback)
            self.stream.start()

    # ------------------------------------------------------------ control (UI thread)

    def load(self, deck: Deck, pos: int = 0):
        with self._lock:
            self.deck, self.pos, self.pending, self.in_mix = deck, pos, None, False
            self._log.append((self._clock, deck, pos, None, 0))

    def play(self, on: bool = True):
        with self._lock:
            self.playing = on and self.deck is not None and self.pos < self.deck.length

    def toggle(self):
        self.play(not self.playing)

    def seek(self, pos: int) -> bool:
        """Jump within deck A; refused during a transition, or past (or too close to) a queued one."""
        with self._lock:
            if self.deck is None or self.in_mix:
                return False
            if self.pending is not None and pos > self.pending.start - SR:
                return False
            self.pos = int(np.clip(pos, 0, self.deck.length - 1))
            self._log.append((self._clock, self.deck, self.pos, None, 0))
            return True

    def schedule(self, s: Scheduled) -> bool:
        """Queue a transition; refused if deck A has already passed its start."""
        with self._lock:
            if self.deck is None or s.start < self.pos + SR // 4:
                return False
            self.pending = s
            return True

    def cancel(self) -> bool:
        with self._lock:
            if self.in_mix:
                return False
            self.pending = None
            return True

    def close(self):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    # ------------------------------------------------------------ audio thread

    def _callback(self, out, frames, time_info, status):
        with self._lock:
            if not self.playing or self.deck is None:
                out.fill(0)
                self.level *= 0.8
                return
            filled = 0
            while filled < frames:
                need = frames - filled
                if self.in_mix:
                    s = self.pending
                    self._log.append((self._clock + filled, self.deck, s.start + self.tpos, s, self.tpos))
                    n = min(need, len(s.buffer) - self.tpos)
                    out[filled:filled + n] = s.buffer[self.tpos:self.tpos + n]
                    filled += n
                    self.tpos += n
                    if self.tpos >= len(s.buffer):  # transition done: deck B takes over
                        self.deck, self.pos, self.pending, self.in_mix = s.deck, s.b_after, None, False
                    continue
                self._log.append((self._clock + filled, self.deck, self.pos, None, 0))
                stop = self.deck.length if self.pending is None else self.pending.start
                n = max(0, min(need, stop - self.pos))
                out[filled:filled + n] = self.deck.audio[self.pos:self.pos + n]
                filled += n
                self.pos += n
                if self.pending is not None and self.pos >= self.pending.start:
                    self.in_mix, self.tpos = True, 0
                elif self.pos >= self.deck.length:
                    out[filled:] = 0
                    self.playing = False
                    break
            out *= self.volume
            np.clip(out, -1, 1, out=out)
            self.level = max(float(np.abs(out).max()), self.level * 0.85)
            self._clock += frames

    def advance(self, frames: int):
        """Run the callback without a sound card (tests and screenshots)."""
        buf = np.zeros((frames, 2), np.float32)
        for i in range(0, frames, 2048):
            n = min(2048, frames - i)
            self._callback(buf[i:i + n], n, None, None)

    # ------------------------------------------------------------ what is heard (UI thread)

    def snapshot(self) -> Snapshot:
        with self._lock:
            lag = int((self.stream.latency if self.stream else 0.0) * SR)
            heard = self._clock - lag
            entry = next((e for e in reversed(self._log) if e[0] <= heard), self._log[0] if self._log else None)
            if entry is None:
                return Snapshot(self.deck, self.pos, None, 0, self.playing, self.level)
            clock, deck, pos, sched, tpos = entry
            dt = max(0, heard - clock) if self.playing else 0
            if not self.playing:  # paused: show where playback stopped
                return Snapshot(self.deck, self.pos if not self.in_mix else self.pending.start + self.tpos,
                                self.pending if self.in_mix else None, self.tpos, False, self.level)
            if sched is not None:
                t = min(tpos + dt, len(sched.buffer) - 1)
                return Snapshot(deck, sched.start + t, sched, t, True, self.level)
            return Snapshot(deck, min(pos + dt, deck.length - 1), None, 0, True, self.level)
