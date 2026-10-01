"""Decks: a track's audio decoded, gain-matched, time-stretched to the set tempo, plus its waveform."""
from dataclasses import dataclass
from functools import cached_property, lru_cache

import numpy as np
import pedalboard
import soundfile as sf
import soxr
from scipy.signal import butter, sosfilt

from .library import Track

SR = 44100
TARGET_LUFS = -11.0  # every deck is gain-matched to this short-term loudness
WAVE_RATE = 150      # waveform envelope points per second

_LOW = butter(4, 200, "lowpass", fs=SR, output="sos")
_HIGH = butter(4, 2500, "highpass", fs=SR, output="sos")


@lru_cache(maxsize=3)
def native_audio(track: Track) -> np.ndarray:
    """(n, 2) float32 at SR, gain-matched to TARGET_LUFS."""
    x, sr = sf.read(str(track.path), dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        x = np.repeat(x, 2, axis=1)
    x = x[:, :2]
    if sr != SR:
        x = soxr.resample(x, sr, SR)
    # Match loudness, but never push the peaks past 0.98: raw playback has no limiter.
    gain = min(10 ** (np.clip(TARGET_LUFS - track.loudness, -9.0, 6.0) / 20), 0.98 / max(float(np.abs(x).max()), 1e-6))
    return np.ascontiguousarray(x * gain, dtype=np.float32)


_KICK = butter(2, 150, "lowpass", fs=SR, output="sos")


def kick_onsets(x: np.ndarray) -> np.ndarray:
    """Rises in low-band energy at 1 ms resolution: where the kicks hit."""
    e = np.abs(sosfilt(_KICK, x.mean(axis=1)))
    n = len(e) // 44
    e = e[:n * 44].reshape(n, 44).mean(axis=1)
    return np.maximum(np.diff(e, prepend=e[0]), 0)


@lru_cache(maxsize=16)
def beat_period(track: Track) -> float:
    """Seconds per beat at native tempo, measured from the audio: the kick pattern is compared
    with itself 64 beats later and the exact lag found (to ~0.1 ms). allin1's beats sit on a
    10 ms grid, which alone would let two '128 BPM' tracks drift apart within a minute."""
    coarse = track.beat_period
    env = kick_onsets(native_audio(track))
    beats = 64 if len(env) > coarse * 1000 * 128 else 16
    center = int(round(coarse * beats * 1000))
    lags = np.arange(center - 40, center + 41)
    corr = np.array([np.dot(env[:-lag], env[lag:]) / (len(env) - lag) for lag in lags])
    i = int(np.argmax(corr))
    frac = 0.0
    if 0 < i < len(corr) - 1:
        denom = corr[i - 1] - 2 * corr[i] + corr[i + 1]
        frac = 0.5 * (corr[i - 1] - corr[i + 1]) / denom if denom else 0.0
    period = float((lags[i] + frac) / 1000 / beats)
    return period if abs(period / coarse - 1) < 0.01 else float(coarse)


def stretch(x: np.ndarray, factor: float) -> np.ndarray:
    """Time-stretch without changing pitch; factor > 1 plays faster."""
    if abs(factor - 1) < 1e-4:
        return x
    y = pedalboard.time_stretch(x.T.copy(), SR, stretch_factor=factor, high_quality=False)
    return np.ascontiguousarray(y.T, dtype=np.float32)


@dataclass
class Waveform:
    """Envelopes per 1/WAVE_RATE s at native tempo, 0-1: RMS of the lows, mids, highs and full band
    (RMS shows the kicks pulsing; peaks of a loud master are a flat brick), plus the full-band peak."""
    low: np.ndarray
    mid: np.ndarray
    high: np.ndarray
    full: np.ndarray
    peak: np.ndarray

    @property
    def seconds(self) -> float:
        return len(self.full) / WAVE_RATE


@lru_cache(maxsize=8)
def waveform(track: Track) -> Waveform:
    mono = native_audio(track).mean(axis=1)
    low = sosfilt(_LOW, mono)
    high = sosfilt(_HIGH, mono)
    mid = mono - low - high
    hop = SR // WAVE_RATE
    n = len(mono) // hop
    frames = lambda s: s[: n * hop].reshape(n, hop)
    rms = lambda s: np.sqrt(np.mean(frames(s) ** 2, axis=1)).astype(np.float32)
    full = rms(mono)
    scale = max(float(np.percentile(full, 99.5)), 1e-6)
    norm = lambda e: np.clip(e / scale, 0, 1) ** 0.8  # a gentle curve so quiet parts stay visible
    peak = np.abs(frames(mono)).max(axis=1)
    return Waveform(norm(rms(low)), norm(rms(mid)), norm(rms(high)), norm(full),
                    np.clip(peak / max(float(np.percentile(peak, 99.5)), 1e-6), 0, 1).astype(np.float32))


@dataclass(eq=False)
class Deck:
    """A track ready to play: `audio` runs at `ratio` x its native tempo."""
    track: Track
    audio: np.ndarray
    ratio: float

    @property
    def wave(self) -> Waveform:
        return waveform(self.track)

    @property
    def period(self) -> float:
        """Seconds per beat as played."""
        return beat_period(self.track) / self.ratio

    @property
    def bpm(self) -> float:
        return 60.0 / self.period

    @property
    def spb(self) -> float:
        """Samples per beat as played."""
        return SR * self.period

    @property
    def length(self) -> int:
        return len(self.audio)

    @cached_property
    def beats(self) -> np.ndarray:
        return self.track.beats / self.ratio

    @cached_property
    def downbeats(self) -> np.ndarray:
        return self.track.downbeats / self.ratio

    @cached_property
    def sections(self) -> list:
        return [(s.label, s.start / self.ratio, s.end / self.ratio) for s in self.track.sections]

    def bar_sample(self, bar: int) -> int:
        """Sample where 1-based `bar` starts."""
        return int(round(self.downbeats[min(max(bar, 1), len(self.downbeats)) - 1] * SR))

    def where(self, sample: int) -> tuple:
        """(bar, beat in bar, section label) at a sample; bar 0 = before the first downbeat."""
        t = sample / SR
        bar = int(np.searchsorted(self.downbeats, t + 1e-4, side="right"))
        beat = 1
        if bar:  # beats from this bar's downbeat up to t
            beat = int(np.searchsorted(self.beats, t + 1e-4, side="right")
                       - np.searchsorted(self.beats, self.downbeats[bar - 1] - 1e-3, side="left"))
        label = next((lab for lab, a, b in self.sections if a <= t < b), "")
        return bar, max(1, beat), label


def sync_ratio(track: Track, master_period: float) -> float:
    """Stretch that puts the track on the set's beat period (allowing half/double time)."""
    native = beat_period(track)
    return min((native / (master_period * m) for m in (0.5, 1.0, 2.0)), key=lambda f: abs(f - 1))


def load_deck(track: Track, ratio: float = 1.0) -> Deck:
    waveform(track)  # warm the caches off the UI thread
    beat_period(track)
    return Deck(track, stretch(native_audio(track), ratio), ratio)
