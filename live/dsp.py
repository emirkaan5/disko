"""Offline DSP for rendering a transition: kill EQ, DJ filter, echo and a limiter.

A transition is rendered ahead of time (well under a second for a long blend), so the live
audio callback only ever copies samples.
"""
import math

import numpy as np
from numba import njit
from scipy.signal import butter, sosfilt

from .audio import SR

LOW_X, HIGH_X = 200.0, 2500.0  # isolator crossovers (Hz)
EDGE = 2048                     # samples crossfaded between raw and processed audio at a filtered edge


def _lr4(kind: str, fc: float) -> np.ndarray:
    s = butter(2, fc, kind, fs=SR, output="sos")
    return np.vstack([s, s])  # two Butterworth-2 sections make a Linkwitz-Riley 4


LR_LOW, LR_HIGH = _lr4("lowpass", LOW_X), _lr4("highpass", HIGH_X)


def isolator(x: np.ndarray, low: np.ndarray, mid: np.ndarray, high: np.ndarray) -> np.ndarray:
    """3-band kill EQ; with every gain at 1 the bands sum back to the input exactly."""
    lo = sosfilt(LR_LOW, x, axis=0)
    hi = sosfilt(LR_HIGH, x, axis=0)
    md = x - lo - hi
    return (lo * low[:, None] + md * mid[:, None] + hi * high[:, None]).astype(np.float32)


@njit(cache=True, nogil=True)
def _svf(fc, q, sr):
    g = math.tan(math.pi * min(fc, 0.45 * sr) / sr)
    k = 1.0 / q
    a1 = 1.0 / (1.0 + g * (g + k))
    return a1, g * a1, g * g * a1, k


@njit(cache=True, nogil=True)
def dj_filter(x, knob, resonance, sr):
    """One-knob DJ filter: knob < 0 sweeps a low-pass down to 150 Hz, > 0 a high-pass up to 5 kHz.

    A low-pass then a high-pass state-variable filter (Zavalishin's TPT form, which stays clean
    under fast modulation); each parks at the edge of the audio band when not in use."""
    n = x.shape[0]
    y = np.empty_like(x)
    l1 = np.zeros(2)
    l2 = np.zeros(2)
    h1 = np.zeros(2)
    h2 = np.zeros(2)
    for i in range(n):
        v = knob[i]
        q = 0.707 + (resonance - 0.707) * min(abs(v) * 4.0, 1.0)  # resonance fades in off-center
        f_lp = 20000.0 * (150.0 / 20000.0) ** (-v) if v < 0 else 20000.0
        f_hp = 20.0 * (5000.0 / 20.0) ** v if v > 0 else 20.0
        a1, a2, a3, _ = _svf(f_lp, q if v < 0 else 0.707, sr)
        b1, b2, b3, kb = _svf(f_hp, q if v > 0 else 0.707, sr)
        for c in range(2):
            v3 = x[i, c] - l2[c]
            v1 = a1 * l1[c] + a2 * v3
            v2 = l2[c] + a2 * l1[c] + a3 * v3
            l1[c] = 2 * v1 - l1[c]
            l2[c] = 2 * v2 - l2[c]
            w3 = v2 - h2[c]
            w1 = b1 * h1[c] + b2 * w3
            w2 = h2[c] + b2 * h1[c] + b3 * w3
            h1[c] = 2 * w1 - h1[c]
            h2[c] = 2 * w2 - h2[c]
            y[i, c] = v2 - kb * w1 - w2
    return y


@njit(cache=True, nogil=True)
def echo(x, send, feedback, delay, sr):
    """Feedback delay of `delay` samples; returns only the echoes. Each repeat loses some bass."""
    n = x.shape[0]
    y = np.zeros_like(x)
    buf = np.zeros((delay, 2), dtype=x.dtype)
    c = 1.0 / (1.0 + 2.0 * math.pi * 300.0 / sr)  # one-pole high-pass at ~300 Hz on the feedback path
    hy = np.zeros(2)
    hx = np.zeros(2)
    w = 0
    for i in range(n):
        for ch in range(2):
            d = buf[w, ch]
            y[i, ch] = d
            h = c * (hy[ch] + d - hx[ch])
            hx[ch] = d
            hy[ch] = h
            buf[w, ch] = x[i, ch] * send[i] + h * feedback[i]
        w += 1
        if w == delay:
            w = 0
    return y


@njit(cache=True, nogil=True)
def limiter(x, ceiling, release_s, sr):
    """Peak limiter with instant attack, so nothing passes above `ceiling`."""
    n = x.shape[0]
    y = np.empty_like(x)
    r = 1.0 - math.exp(-1.0 / (release_s * sr))
    g = 1.0
    for i in range(n):
        p = max(abs(x[i, 0]), abs(x[i, 1]))
        t = ceiling / p if p > ceiling else 1.0
        if t < g:
            g = t
        else:
            g += (t - g) * r
        y[i, 0] = x[i, 0] * g
        y[i, 1] = x[i, 1] * g
    return y


def _edges(raw: np.ndarray, done: np.ndarray, start: bool, end: bool) -> np.ndarray:
    """Crossfade from/to the untouched audio at the buffer's edges, so the hand-off to the raw
    track never clicks even after phase-shifting filters."""
    n = min(EDGE, len(raw) // 4)
    ramp = np.linspace(0, 1, n, dtype=np.float32)[:, None]
    if start:
        done[:n] = raw[:n] * (1 - ramp) + done[:n] * ramp
    if end:
        done[-n:] = done[-n:] * (1 - ramp) + raw[-n:] * ramp
    return done


def render(a: np.ndarray, b: np.ndarray, auto, spb: float) -> np.ndarray:
    """Mix deck segments a and b (same length, sample-aligned) through `auto`'s automation."""
    n = len(a)
    mix = np.zeros((n, 2), np.float32)
    delay = max(256, int(round(auto.echo_beats * spb)))
    for deck, x in (("A", a), ("B", b)):
        lane = lambda name: auto.curve(deck, name, n, spb)
        fader = lane("fader")
        y = isolator(x, lane("low"), lane("mid"), lane("high"))
        send = lane("echo")
        wet = echo(y, send, lane("feedback"), delay, SR) if send.any() else 0.0
        y = y * lane("dry")[:, None] + wet
        knob = lane("filter")
        if knob.any():
            y = dj_filter(np.ascontiguousarray(y), knob.astype(np.float64), float(auto.resonance), SR)
            y = _edges(x, y, start=deck == "A" and knob[0] == 0, end=deck == "B" and knob[-1] == 0)
        mix += y * fader[:, None]
    return limiter(mix, 0.98, 0.12, SR)  # decks peak at 0.98 alone, so only overlaps get limited


def warmup():
    """Compile the numba kernels once (cached on disk after the first run)."""
    x = np.zeros((512, 2), np.float32)
    ones = np.ones(512, np.float32)
    dj_filter(x, np.full(512, 0.3), 1.5, SR)
    echo(x, ones, ones * 0.5, 64, SR)
    limiter(x, 0.9, 0.1, SR)
