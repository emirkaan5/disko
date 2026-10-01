"""A playlist folder: its audio files, what disko learns about them, and the tracks that makes.

Everything disko keeps about a folder lives next to the music, in <folder>/disko/:

    tracks.csv                 one row per track: title, artist, file and the 20 columns the DJ uses
    analysis/<id>.fast.json    the fast pass (live/workers/fast.py), run when the folder is opened
    analysis/<id>.allin1.json  beats, bars and sections, and per-bar stem levels (live/workers/deep.py),
    analysis/<id>.stems.json   run the first time the track is about to play
    skipped.json               files that couldn't be read (tried again only if they change)

A track's id comes from its path inside the folder, and a file whose size changes is analyzed
again. The structure columns of tracks.csv stay blank until a track has been played once.
"""
import csv
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .library import Section, Track

AUDIO = {".mp3", ".wav", ".flac", ".aiff", ".aif", ".ogg", ".opus", ".m4a", ".aac"}
FOLDER = "disko"
FAST_VERSION = 1  # live/workers/fast.py's VERSION; results from another version are redone

PRESENT_DB = -18.0  # a stem is playing in a bar when it is within 18 dB of the full mix there...
SILENCE_DB = -60.0  # ...and the bar itself is louder than this (dBFS)
HOUSE_BPM = (100, 140)  # settles half/double-tempo disagreements between Essentia and allin1

PITCH = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
         "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
CAMELOT_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}

# tracks.csv: ids, then the top-20 columns of data/house/tracks_top20.csv (genres replaces
# source_genres: a folder of files has no uploader tags, so Essentia's Discogs styles stand in),
# then a few extras.
COLUMNS = [
    "track_uid", "file_path", "title", "artist",
    "dj_bpm", "camelot", "ess_tonal_key_edma_strength", "intro_bars_before_bass", "a1_outro_bars",
    "intro_bars_before_vocals", "outro_bars_after_vocals", "a1_sections", "a1_first_chorus_bar", "stem_bass_bars",
    "stem_vocals_bars", "ml_arousal", "ml_valence", "ml_danceability", "ess_lowlevel_loudness_ebu128_short_term_mean",
    "ess_rhythm_onset_rate", "ess_lowlevel_spectral_centroid_mean", "stem_vocals_present_frac",
    "stem_drums_energy_share", "genres",
    "key", "duration_s", "ml_voice", "tag_genre", "analyzed",
]
README = """This folder was made by disko live, the AI DJ app. It keeps what disko learned about the music
in the folder above, so the next time you open that folder the set can start right away.

tracks.csv   one row per track: tempo, key, energy, mood, vocals, style and more. The structure
             columns (bars, sections, where the bass and vocals come in) fill in the first time
             a track is played, because that analysis takes a while per track.
analysis/    the raw results behind tracks.csv.
skipped.json files that couldn't be read (tried again if they change).

Delete this folder to have everything analyzed again.
"""


def camelot(key: str, scale: str) -> str:
    pc = PITCH[key]
    if scale == "minor":
        return f"{CAMELOT_MAJOR[(pc + 3) % 12]}A"  # same number as its relative major
    return f"{CAMELOT_MAJOR[pc]}B"


def style_names(styles: dict) -> str:
    """The model's top Discogs styles, 'Electronic---Deep House' -> 'Deep House'."""
    ranked = sorted(styles.items(), key=lambda kv: -kv[1])
    if not ranked:
        return ""
    top = ranked[0][1]
    keep = [name.split("---")[-1] for name, p in ranked[:3] if p >= top * 0.4]
    return "; ".join(dict.fromkeys(keep))


def vocals_guess(voice: float) -> float:
    """Share of bars with vocals, guessed from the voice model's average (fit to the house set's stems;
    a rough guide, replaced by the stems once the track has been through allin1)."""
    return float(np.clip(0.7 * (voice - 0.05), 0.0, 1.0))


def guess_names(rel: str) -> tuple:
    """(title, artist) from a file name like '03 - Artist - Title.mp3'."""
    stem = re.sub(r"^\d{1,3}[\s._-]+", "", Path(rel).stem).strip() or Path(rel).stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return title.strip(), artist.strip()
    return stem, ""


def dj_bpm(ess: float, a1: float | None) -> float:
    """One BPM for DJing: if the trackers agree, allin1's; on a half/double split, the one in the house range."""
    if not a1:
        return ess
    inside = lambda b: HOUSE_BPM[0] <= b <= HOUSE_BPM[1]
    r = a1 / ess
    if abs(r - 1) <= 0.02:
        return a1
    if abs(r - 2) <= 0.04 or abs(r - 0.5) <= 0.01:
        return a1 if inside(a1) else ess
    return ess if inside(ess) and not inside(a1) else a1


def structure(a: dict, st: dict, essentia_bpm: float) -> dict:
    """Track fields from allin1's results and the stem levels (the rules of scripts/house/build_csv.py)."""
    beats, downbeats = np.array(a["beats"], dtype=float), np.array(a["downbeats"], dtype=float)
    bar_of = lambda t: int(np.searchsorted(downbeats, t + 1e-3, side="right"))  # 1-based; 0 = before bar 1
    merged = []
    for s in a["segments"]:
        if s["label"] in ("start", "end"):
            continue
        if merged and merged[-1]["label"] == s["label"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    chorus = next((s for s in merged if s["label"] == "chorus"), None)
    a1_bpm = 60 / float(np.median(np.diff(beats))) if len(beats) > 8 else None
    fields = {
        "sections": [Section(s["label"], float(s["start"]), float(s["end"])) for s in merged],
        "outro_bars": bar_of(merged[-1]["end"]) - bar_of(merged[-1]["start"]) if merged and merged[-1]["label"] == "outro" else 0,
        "first_drop_bar": bar_of(chorus["start"]) if chorus else 0,
        "bpm": round(dj_bpm(essentia_bpm, a1_bpm), 2),
    }
    if "db" in st:
        db = {k: np.array(v, dtype=float) for k, v in st["db"].items()}
        n = len(db["mix"])
        loud = db["mix"] > SILENCE_DB
        on = {s: (db[s] - db["mix"] > PRESENT_DB) & loud for s in ("bass", "vocals")}
        bass, vocals = np.flatnonzero(on["bass"]), np.flatnonzero(on["vocals"])
        fields.update({
            "bass_bars": "".join("1" if x else "0" for x in on["bass"]),
            "vocal_bars": "".join("1" if x else "0" for x in on["vocals"]),
            "bass_in_bars": int(bass[0]) if len(bass) else n,
            "vocals_in_bars": int(vocals[0]) if len(vocals) else n,
            "vocals_out_bars": n - 1 - int(vocals[-1]) if len(vocals) else n,
            "vocals": round(float(on["vocals"].mean()), 3) if n else 0.0,
            "drums": st["energy_share"].get("drums"),
        })
    return fields


def _json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class AudioFile:
    id: str
    path: Path   # absolute
    rel: str     # inside the playlist folder
    bytes: int


def scan(folder: Path) -> list:
    """Every audio file under the folder (hidden files and disko's own folder aside), by path."""
    out = []
    for root, dirs, files in os.walk(folder):
        top = Path(root) == folder
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (top and d == FOLDER))
        for name in sorted(files):
            if name.startswith(".") or Path(name).suffix.lower() not in AUDIO:
                continue
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:  # a broken link
                continue
            rel = path.relative_to(folder).as_posix()
            out.append(AudioFile(hashlib.sha1(rel.encode()).hexdigest()[:12], path, rel, size))
    return out


class Playlist:
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().resolve()
        self.dir = self.folder / FOLDER
        self.analysis = self.dir / "analysis"
        self.csv = self.dir / "tracks.csv"
        self.files = scan(self.folder)
        self.by_id = {f.id: f for f in self.files}
        self.tracks = []  # set by load()
        self.fast = {}    # id -> fast-pass results
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self.folder.name

    def _fast(self, f: AudioFile):
        d = _json(self.analysis / f"{f.id}.fast.json")
        return d if d and d.get("version") == FAST_VERSION and d.get("bytes") == f.bytes else None

    def pending(self) -> list:
        """Files the fast pass hasn't seen (or has only seen at another size), minus unreadable ones."""
        skipped = _json(self.dir / "skipped.json") or {}
        return [f for f in self.files
                if self._fast(f) is None and skipped.get(f.rel, {}).get("bytes") != f.bytes]

    def skip(self, failures: dict):
        """Remember files the fast pass couldn't read ({rel: {"bytes", "error"}}), so they aren't
        retried on every visit."""
        skipped = {**(_json(self.dir / "skipped.json") or {}), **failures}
        (self.dir / "skipped.json").write_text(json.dumps(skipped, indent=1, ensure_ascii=False))

    def prepare(self, ids):
        """Make the folders, and drop deep results that belong to an older version of these files."""
        self.analysis.mkdir(parents=True, exist_ok=True)
        if not (self.dir / "README.txt").exists():
            (self.dir / "README.txt").write_text(README)
        for i in ids:
            for kind in ("allin1", "stems"):
                (self.analysis / f"{i}.{kind}.json").unlink(missing_ok=True)

    def load(self) -> list:
        """A track for every file the fast pass has seen, with its structure where allin1 has run too."""
        self.prepare([])
        tracks = []
        for f in self.files:
            d = self._fast(f)
            if d is None:
                continue
            self.fast[f.id] = d
            tags = d.get("tags", {})
            title, artist = (tags.get("title") or "").strip(), (tags.get("artist") or "").strip()
            if not title:
                title, guessed = guess_names(f.rel)
                artist = artist or guessed
            t = Track(uid=f.id, path=f.path, title=title or "Untitled", artist=artist or "Unknown artist",
                      bpm=d["bpm"] or 120.0, camelot=camelot(d["key"], d["scale"]), key_strength=d["key_strength"],
                      energy=d["arousal"], mood=d["valence"], danceability=d["danceability"],
                      loudness=d["loudness_short_term_mean"], busyness=d["onset_rate"],
                      brightness=d["spectral_centroid_mean"], vocals=vocals_guess(d["voice"]),
                      genres=style_names(d["styles"]), duration=d["duration"], rough_beats=tuple(d["beats"]))
            self._structure(t)
            tracks.append(t)
        self.tracks = tracks
        self.save()
        return tracks

    def _structure(self, t: Track) -> bool:
        a = _json(self.analysis / f"{t.uid}.allin1.json")
        st = _json(self.analysis / f"{t.uid}.stems.json")
        if a is None or st is None or len(a.get("downbeats", [])) < 2:
            return False
        t.set_structure(grid_file=self.analysis / f"{t.uid}.allin1.json", **structure(a, st, self.fast[t.uid]["bpm"]))
        return True

    def add_structure(self, t: Track) -> bool:
        """allin1 has just run on a track: fill in its structure and update tracks.csv."""
        ok = self._structure(t)
        if ok:
            self.save()
        return ok

    def save(self):
        """Write tracks.csv (atomically: the folder may be open in a spreadsheet)."""
        rows = []
        for t in self.tracks:
            d = self.fast[t.uid]
            row = {"track_uid": t.uid, "file_path": self.by_id[t.uid].rel, "title": t.title, "artist": t.artist,
                   "dj_bpm": round(t.bpm, 2), "camelot": t.camelot, "ess_tonal_key_edma_strength": round(t.key_strength, 3),
                   "ml_arousal": round(t.energy, 3), "ml_valence": round(t.mood, 3), "ml_danceability": round(t.danceability, 4),
                   "ess_lowlevel_loudness_ebu128_short_term_mean": round(t.loudness, 2),
                   "ess_rhythm_onset_rate": round(t.busyness, 3), "ess_lowlevel_spectral_centroid_mean": round(t.brightness, 1),
                   "genres": t.genres, "key": f"{d['key']} {d['scale']}", "duration_s": round(t.duration, 1),
                   "ml_voice": round(d["voice"], 3), "tag_genre": (d.get("tags") or {}).get("genre", ""),
                   "analyzed": "fast+structure" if t.deep else "fast"}
            if t.deep:
                row.update({
                    "intro_bars_before_bass": t.bass_in_bars, "a1_outro_bars": t.outro_bars,
                    "intro_bars_before_vocals": t.vocals_in_bars, "outro_bars_after_vocals": t.vocals_out_bars,
                    "a1_sections": " | ".join(f"{s.label} {s.start:.1f}-{s.end:.1f}" for s in t.sections),
                    "a1_first_chorus_bar": t.first_drop_bar or "", "stem_bass_bars": t.bass_bars,
                    "stem_vocals_bars": t.vocal_bars, "stem_vocals_present_frac": t.vocals,
                    "stem_drums_energy_share": "" if t.drums is None else t.drums})
            rows.append(row)
        with self._lock:
            tmp = self.csv.with_suffix(".tmp")
            with open(tmp, "w", newline="") as f:
                w = csv.DictWriter(f, COLUMNS)
                w.writeheader()
                w.writerows(rows)
            os.replace(tmp, self.csv)

    def summary(self) -> str:
        """'128 tracks · 12 fully analyzed', from what's on disk (for the recent-folders list)."""
        try:
            with open(self.csv) as f:
                rows = list(csv.DictReader(f))
        except OSError:
            return f"{len(self.files)} audio files · not analyzed yet"
        deep = sum(r.get("analyzed") == "fast+structure" for r in rows)
        new = len(self.files) - len(rows)
        return f"{len(rows)} tracks" + (f" · {deep} played before" if deep else "") + (f" · {new} new" if new > 0 else "")
