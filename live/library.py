"""The track library: tracks_top20.csv, each track's allin1 beat grid, and next-track ranking."""
import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent / "data" / "house"
MAX_STRETCH = 0.08  # beyond this tempo change, beatmatched blends are off the table


@dataclass(frozen=True)
class Section:
    label: str
    start: float  # seconds at the track's native tempo
    end: float


def parse_sections(text) -> list:
    if not isinstance(text, str):
        return []
    out = []
    for part in text.split(" | "):
        label, span = part.rsplit(" ", 1)
        start, end = span.split("-")
        out.append(Section(label, float(start), float(end)))
    return out


@dataclass(eq=False)
class Track:
    uid: str
    path: Path
    title: str
    artist: str
    bpm: float
    camelot: str
    key_strength: float
    bass_in_bars: int      # bars before the bass first plays
    outro_bars: int        # bars in allin1's closing outro section
    vocals_in_bars: int    # bars before vocals first play
    vocals_out_bars: int   # bars after vocals last play
    sections: list
    first_drop_bar: int    # bar of the first 'chorus' (usually the drop); 0 if none
    bass_bars: str         # one digit per bar, 1 = bass playing
    vocal_bars: str
    energy: float          # arousal, 1-9
    mood: float            # valence, 1-9
    danceability: float
    loudness: float        # LUFS
    busyness: float        # onsets per second
    brightness: float      # spectral centroid, Hz
    vocals: float          # share of bars with vocals
    drums: float           # drums' share of the stem energy
    genres: str

    @cached_property
    def grid(self) -> tuple:
        """(beats, downbeats) in native seconds, from allin1."""
        source, stem = self.uid.split("_", 1)
        d = json.loads((ROOT / "analysis" / "allin1" / source / f"{stem}.json").read_text())
        return np.array(d["beats"], dtype=float), np.array(d["downbeats"], dtype=float)

    @property
    def beats(self):
        return self.grid[0]

    @property
    def downbeats(self):
        return self.grid[1]

    @cached_property
    def beat_period(self) -> float:
        """Seconds per beat: the median over 32-beat spans, which averages away allin1's 10 ms
        frame grid and ignores a stray missed or extra beat."""
        b = self.beats
        span = 32 if len(b) > 64 else 4
        if len(b) <= span:
            return 60.0 / self.bpm
        return float(np.median((b[span:] - b[:-span]) / span))

    @property
    def grid_bpm(self) -> float:
        return 60.0 / self.beat_period

    @property
    def n_bars(self) -> int:
        return len(self.downbeats)

    @cached_property
    def genre_list(self) -> list:
        return [g for g in self.genres.split("; ") if g] if self.genres else []

    @property
    def display(self) -> str:
        return f"{self.title} — {self.artist}"


def _int(v, default=0) -> int:
    return default if pd.isna(v) else int(v)


def load_tracks(csv=ROOT / "tracks_top20.csv") -> list:
    df = pd.read_csv(csv)
    tracks = []
    for r in df.itertuples(index=False):
        tracks.append(Track(
            uid=r.track_uid, path=ROOT / r.file_path,
            title=r.title if isinstance(r.title, str) else "Untitled",
            artist=r.artist if isinstance(r.artist, str) else "Unknown artist",
            bpm=float(r.dj_bpm), camelot=r.camelot, key_strength=float(r.ess_tonal_key_edma_strength),
            bass_in_bars=_int(r.intro_bars_before_bass), outro_bars=_int(r.a1_outro_bars),
            vocals_in_bars=_int(r.intro_bars_before_vocals), vocals_out_bars=_int(r.outro_bars_after_vocals),
            sections=parse_sections(r.a1_sections), first_drop_bar=_int(r.a1_first_chorus_bar),
            bass_bars=r.stem_bass_bars if isinstance(r.stem_bass_bars, str) else "",
            vocal_bars=r.stem_vocals_bars if isinstance(r.stem_vocals_bars, str) else "",
            energy=float(r.ml_arousal), mood=float(r.ml_valence), danceability=float(r.ml_danceability),
            loudness=float(r.ess_lowlevel_loudness_ebu128_short_term_mean), busyness=float(r.ess_rhythm_onset_rate),
            brightness=float(r.ess_lowlevel_spectral_centroid_mean), vocals=float(r.stem_vocals_present_frac),
            drums=float(r.stem_drums_energy_share),
            genres=r.source_genres if isinstance(r.source_genres, str) else ""))
    return tracks


# ---------------------------------------------------------------- compatibility

KEY_NAMES = {
    "1A": "A♭ minor", "2A": "E♭ minor", "3A": "B♭ minor", "4A": "F minor", "5A": "C minor", "6A": "G minor",
    "7A": "D minor", "8A": "A minor", "9A": "E minor", "10A": "B minor", "11A": "F♯ minor", "12A": "D♭ minor",
    "1B": "B major", "2B": "F♯ major", "3B": "D♭ major", "4B": "A♭ major", "5B": "E♭ major", "6B": "B♭ major",
    "7B": "F major", "8B": "C major", "9B": "G major", "10B": "D major", "11B": "A major", "12B": "E major",
}


def camelot_parts(code: str) -> tuple:
    return int(code[:-1]), code[-1]


def key_relation(a: str, b: str) -> tuple:
    """(score 0-1, label) for mixing key b after key a on the Camelot wheel."""
    (na, la), (nb, lb) = camelot_parts(a), camelot_parts(b)
    step = (nb - na) % 12
    if la == lb:
        if step == 0:
            return 1.0, "same key"
        if step in (1, 11):
            return 0.9, "next door"
        if step == 2:
            return 0.6, "energy boost"
        if step == 10:
            return 0.55, "energy dip"
    elif step == 0:
        return 0.85, "relative key"
    elif step in (1, 11):
        return 0.45, "diagonal"
    return 0.15, "key clash"


def tempo_match(master_bpm: float, bpm: float) -> tuple:
    """(stretch factor to play at master tempo, deviation), allowing half/double time."""
    factor = min((master_bpm / (bpm * m) for m in (0.5, 1.0, 2.0)), key=lambda f: abs(f - 1))
    return factor, abs(factor - 1)


@dataclass
class Candidate:
    track: Track
    score: float
    stretch: float     # factor B would be time-stretched by
    deviation: float   # |stretch - 1|
    key_score: float
    key_label: str
    energy_delta: float


class Library:
    FEATURES = ("energy", "mood", "danceability", "busyness", "brightness", "drums", "vocals")

    def __init__(self, tracks=None):
        self.tracks = tracks or load_tracks()
        self.by_uid = {t.uid: t for t in self.tracks}
        raw = np.array([[getattr(t, f) for f in self.FEATURES] for t in self.tracks], dtype=float)
        raw[:, 4] = np.log(raw[:, 4])  # brightness on a log scale
        self._raw = raw
        self._z = (raw - raw.mean(0)) / (raw.std(0) + 1e-9)
        self._index = {t.uid: i for i, t in enumerate(self.tracks)}

    def spread(self, feature: str) -> float:
        """The library's standard deviation for a feature."""
        return float(self._raw[:, self.FEATURES.index(feature)].std())

    def percentile(self, track: Track, feature: str) -> float:
        """Where the track sits in the library for a feature, 0-1."""
        j = self.FEATURES.index(feature)
        col = self._raw[:, j]
        return float((col < self._raw[self._index[track.uid], j]).mean())

    def rank(self, current: Track, master_bpm: float, played: set, lift: int = 0) -> list:
        """Every unplayed track scored as the next one after `current`, best first.

        lift: -1 to cool the energy down, 0 to hold it, +1 to lift it."""
        zc = self._z[self._index[current.uid]]
        out = []
        for i, t in enumerate(self.tracks):
            if t is current or t.uid in played:
                continue
            stretch, dev = tempo_match(master_bpm, t.bpm)
            s_tempo = max(0.0, 1 - dev / MAX_STRETCH)
            k, label = key_relation(current.camelot, t.camelot)
            trust = float(np.clip((min(current.key_strength, t.key_strength) - 0.3) / 0.5, 0.2, 1.0))
            s_key = 1 - trust * (1 - k)
            z = self._z[i]
            s_energy = float(np.exp(-((z[0] - zc[0] - 0.75 * lift) ** 2) / (2 * 0.8 ** 2)))
            s_vibe = float(np.exp(-np.mean((z[1:] - zc[1:]) ** 2) / 2))
            s_runway = min(t.bass_in_bars / 16, 1.0)
            g1, g2 = set(current.genre_list), set(t.genre_list)
            s_genre = len(g1 & g2) / len(g1 | g2) if g1 | g2 else 0.0
            score = 0.30 * s_tempo + 0.22 * s_key + 0.18 * s_energy + 0.18 * s_vibe + 0.06 * s_runway + 0.06 * s_genre
            out.append(Candidate(t, score, stretch, dev, k, label, t.energy - current.energy))
        out.sort(key=lambda c: -c.score)
        return out
