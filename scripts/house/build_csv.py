"""Build data/house/tracks.csv: one row per downloaded house track, with every fact we have on it.

Merges the source metadata (FMA or MTG-Jamendo), the file itself, Essentia's MusicExtractor and
pretrained models, allin1's beats and sections, and per-bar stem levels, then derives DJ-ready
columns: Camelot key, a house-range BPM, intro/outro runway in bars, where bass and vocals enter.
Also writes columns.csv, a dictionary of every column. A track missing an analysis just gets
blanks for it, so this can run while analysis is still going.

    .venv-audio/bin/python scripts/house/build_csv.py
"""
import ast
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from mutagen.id3 import ID3, ID3NoHeaderError

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
META = ROOT / "meta"
ANALYSIS = ROOT / "analysis"

PRESENT_DB = -18.0  # a stem is playing in a bar when it is within 18 dB of the full mix there...
SILENCE_DB = -60.0  # ...and the bar itself is louder than this (dBFS)
VOCAL_BARS = 0.10   # has_vocals: vocals play in at least this share of bars
HOUSE_BPM = (100, 140)  # resolves half/double-tempo disagreements between the two trackers
HOUSE_STYLES = ["House", "Deep House", "Tech House", "Progressive House", "Acid House", "Electro House",
                "Garage House", "Tribal House", "Euro House", "Hard House", "Italo House", "Ghetto House",
                "Hip-House", "Tropical House"]
SECTION_LABELS = ["intro", "verse", "chorus", "bridge", "inst", "solo", "break", "outro"]
STEMS = ["drums", "bass", "vocals", "other"]

PITCH = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
         "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
CAMELOT_MAJOR = {0: 8, 7: 9, 2: 10, 9: 11, 4: 12, 11: 1, 6: 2, 1: 3, 8: 4, 3: 5, 10: 6, 5: 7}


def camelot(key: str, scale: str) -> str:
    pc = PITCH[key]
    if scale == "minor":
        return f"{CAMELOT_MAJOR[(pc + 3) % 12]}A"  # same number as its relative major
    return f"{CAMELOT_MAJOR[pc]}B"


def load_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def clean(v):
    """NaN and empty strings become blanks; numpy scalars become plain Python."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, np.generic):
        return v.item()
    if isinstance(v, str) and not v.strip():
        return None
    return v


# ---------------------------------------------------------------- source metadata

def fma_meta(ids: list) -> dict:
    t = pd.read_csv(META / "fma" / "tracks.csv", index_col=0, header=[0, 1], low_memory=False)
    raw = pd.read_csv(META / "fma" / "raw_tracks.csv", index_col=0, low_memory=False)
    artists = pd.read_csv(META / "fma" / "raw_artists.csv", index_col=0, low_memory=False)
    genres = pd.read_csv(META / "fma" / "genres.csv", index_col=0).title
    echo = pd.read_csv(META / "fma" / "echonest.csv", index_col=0, header=[0, 1, 2])
    names = lambda ids_str: "; ".join(genres[g] for g in ast.literal_eval(ids_str) if g in genres.index)

    out = {}
    for tid in ids:
        tr, al, ar = t.loc[tid, "track"], t.loc[tid, "album"], t.loc[tid, "artist"]
        r = raw.loc[tid] if tid in raw.index else pd.Series(dtype=object)
        a = artists.loc[ar["id"]] if ar["id"] in artists.index else pd.Series(dtype=object)
        released = clean(al["date_released"]) or clean(tr["date_created"])
        row = {
            "title": tr["title"], "artist": ar["name"], "album": al["title"],
            "release_date": str(released)[:10] if released else None,
            "track_number": tr["number"], "album_type": al["type"], "album_tracks": al["tracks"],
            "source_url": r.get("track_url"), "album_url": r.get("album_url"), "artist_url": r.get("artist_url"),
            "license": tr["license"], "license_url": r.get("license_url"),
            "source_genre_top": tr["genre_top"], "source_genres": names(tr["genres"]),
            "source_genres_all": names(tr["genres_all"]),
            "source_tags": "; ".join(ast.literal_eval(tr["tags"])),
            "listens": tr["listens"], "favorites": tr["favorites"], "interest": tr["interest"],
            "comments": tr["comments"], "album_listens": al["listens"], "album_favorites": al["favorites"],
            "explicit": r.get("track_explicit"), "source_instrumental": r.get("track_instrumental"),
            "composer": tr["composer"], "lyricist": tr["lyricist"], "publisher": tr["publisher"],
            "copyright_c": r.get("track_copyright_c"), "copyright_p": r.get("track_copyright_p"),
            "language": tr["language_code"], "date_recorded": tr["date_recorded"],
            "date_uploaded": str(tr["date_created"])[:10],
            "album_producer": al["producer"], "album_engineer": al["engineer"],
            "artist_id": int(ar["id"]), "artist_location": ar["location"],
            "artist_latitude": ar["latitude"], "artist_longitude": ar["longitude"],
            "artist_website": ar["website"], "artist_wikipedia": ar["wikipedia_page"],
            "artist_active_year_begin": ar["active_year_begin"], "artist_active_year_end": ar["active_year_end"],
            "artist_members": ar["members"], "artist_associated_labels": ar["associated_labels"],
            "artist_related_projects": ar["related_projects"],
            "artist_tags": "; ".join(ast.literal_eval(ar["tags"])), "artist_favorites": ar["favorites"],
            "artist_since": str(a.get("artist_date_created"))[:10] if clean(a.get("artist_date_created")) else None,
        }
        if tid in echo.index:
            e = echo.loc[tid, "echonest"]
            for k, v in e["audio_features"].items():
                row[f"echonest_{k}"] = v
            for k, v in e["social_features"].items():
                row[f"echonest_{k}"] = v
        out[tid] = row
    return out


def jamendo_licenses() -> dict:
    """{'14/214.mp3': (license name, url)} from the dataset's audio_licenses.txt."""
    out, path = {}, None
    for line in (META / "jamendo" / "audio_licenses.txt").read_text().splitlines():
        if re.fullmatch(r"\d+/\d+\.mp3", line.strip()):
            path = line.strip()
        elif path and (m := re.match(r"Available under a (.+) license: (\S+)", line.strip())):
            out[path] = (m.group(1), m.group(2))
    return out


def jamendo_meta(ids: list) -> dict:
    # Parsed by hand: a few names contain tabs. IDs lead and date + URL close every line, so any
    # extra field is folded into the album name.
    meta = {}
    with open(META / "jamendo" / "raw.meta.tsv") as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split("\t")
            meta[p[0]] = {"ARTIST_ID": p[1], "ALBUM_ID": p[2], "TRACK_NAME": p[3], "ARTIST_NAME": p[4],
                          "ALBUM_NAME": " ".join(p[5:-2]), "RELEASEDATE": p[-2], "URL": p[-1]}
    tags, paths = {}, {}
    with open(META / "jamendo" / "raw_30s_cleantags.tsv") as f:
        next(f)
        for line in f:
            track_id, _, _, path, _, *t = line.rstrip("\n").split("\t")
            tags[track_id], paths[track_id] = t, path
    licenses = jamendo_licenses()

    out = {}
    for tid in ids:
        m = meta[tid]
        t = tags.get(tid, [])
        pick = lambda kind: "; ".join(x.split("---", 1)[1] for x in t if x.startswith(kind))
        name, url = licenses.get(paths.get(tid), (None, None))
        out[tid] = {
            "title": m["TRACK_NAME"], "artist": m["ARTIST_NAME"], "album": m["ALBUM_NAME"],
            "release_date": m["RELEASEDATE"], "source_url": m["URL"],
            "artist_url": f"https://www.jamendo.com/artist/{int(m['ARTIST_ID'].split('_')[1])}",
            "album_url": f"https://www.jamendo.com/album/{int(m['ALBUM_ID'].split('_')[1])}",
            "license": f"Creative Commons {name}" if name else None, "license_url": url,
            "source_genres": pick("genre---"), "source_genre_top": pick("genre---").split("; ")[0] or None,
            "source_tags": "; ".join(x.split("---", 1)[1] for x in t),
            "source_instrument_tags": pick("instrument---"), "source_mood_tags": pick("mood/theme---"),
            "artist_id": m["ARTIST_ID"],
        }
    return out


# ---------------------------------------------------------------- the file

def file_facts(path: Path) -> dict:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    row = {"file_path": str(path.relative_to(ROOT)), "file_bytes": path.stat().st_size, "file_sha256": h.hexdigest()}
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return row
    for col, frame in [("id3_title", "TIT2"), ("id3_artist", "TPE1"), ("id3_album", "TALB"), ("id3_genre", "TCON"),
                       ("id3_date", "TDRC"), ("id3_bpm", "TBPM"), ("id3_key", "TKEY"), ("id3_publisher", "TPUB"),
                       ("id3_copyright", "TCOP")]:
        if frame in tags:
            row[col] = str(tags[frame])
    row["id3_has_cover_art"] = any(k.startswith("APIC") for k in tags.keys())
    return row


# ---------------------------------------------------------------- analyses

def essentia_features(e: dict) -> dict:
    row = {f"ess_{k.replace('.', '_')}": v for k, v in e.items()
           if k not in ("rhythm.beats_position", "chromaprint_120s")}
    row["fingerprint_chromaprint_120s"] = e.get("chromaprint_120s")
    key, scale = e["tonal.key_edma.key"], e["tonal.key_edma.scale"]
    others = [(e[f"tonal.key_{p}.key"], e[f"tonal.key_{p}.scale"]) for p in ("krumhansl", "temperley")]
    row.update({"key": f"{key} {scale}", "camelot": camelot(key, scale),
                "key_strength": round(e["tonal.key_edma.strength"], 3),
                "key_profiles_agreeing": 1 + sum(o == (key, scale) for o in others)})
    return row


def bar_of(t: float, downbeats: np.ndarray) -> int:
    """1-based bar number that time t falls in (0 = before the first downbeat)."""
    return int(np.searchsorted(downbeats, t + 1e-3, side="right"))


def allin1_features(a: dict) -> dict:
    beats, downbeats = np.array(a["beats"]), np.array(a["downbeats"])
    row = {"a1_bpm": a["bpm"], "a1_beats": len(beats), "a1_bars": len(downbeats),
           "a1_first_beat_s": round(beats[0], 3) if len(beats) else None,
           "a1_first_downbeat_s": round(downbeats[0], 3) if len(downbeats) else None}
    if len(beats) > 8:
        ibi = np.diff(beats)
        q = len(ibi) // 4
        row["a1_bpm_from_beats"] = round(60 / np.median(ibi), 2)
        row["a1_beat_interval_cv"] = round(float(np.std(ibi) / np.mean(ibi)), 4)
        row["a1_tempo_drift_pct"] = round((np.median(ibi[:q]) / np.median(ibi[-q:]) - 1) * 100, 2)
    # Beats per bar, from the 1-2-3-4 beat positions (first and last bar can be partial).
    runs, n = [], 0
    for p in a["beat_positions"]:
        if p == 1 and n:
            runs.append(n)
            n = 0
        n += 1
    full = runs[1:]
    if full:
        row["a1_beats_per_bar"] = Counter(full).most_common(1)[0][0]
        row["a1_bars_4beat_frac"] = round(sum(r == 4 for r in full) / len(full), 3)

    segs = a["segments"]
    row["a1_lead_silence_s"] = round(sum(s["end"] - s["start"] for s in segs if s["label"] == "start"), 2)
    row["a1_tail_silence_s"] = round(sum(s["end"] - s["start"] for s in segs if s["label"] == "end"), 2)
    segs = [s for s in segs if s["label"] not in ("start", "end")]
    merged = []
    for s in segs:
        if merged and merged[-1]["label"] == s["label"]:
            merged[-1]["end"] = s["end"]
        else:
            merged.append(dict(s))
    row["a1_n_segments"] = len(segs)
    row["a1_sections"] = " | ".join(f"{s['label']} {s['start']:.1f}-{s['end']:.1f}" for s in merged)
    row["a1_section_order"] = " > ".join(s["label"] for s in merged)
    for label in SECTION_LABELS:
        row[f"a1_{label}_s"] = round(sum(s["end"] - s["start"] for s in segs if s["label"] == label), 2)
    bars = lambda s: bar_of(s["end"], downbeats) - bar_of(s["start"], downbeats)
    row["a1_intro_bars"] = bars(merged[0]) if merged and merged[0]["label"] == "intro" else 0
    row["a1_outro_bars"] = bars(merged[-1]) if merged and merged[-1]["label"] == "outro" else 0
    chorus = next((s for s in merged if s["label"] == "chorus"), None)
    row["a1_first_chorus_s"] = round(chorus["start"], 2) if chorus else None
    row["a1_first_chorus_bar"] = bar_of(chorus["start"], downbeats) if chorus else None
    row["a1_n_breaks"] = sum(s["label"] == "break" for s in merged)
    # Do section changes land on a downbeat, and on the 8-bar phrase grid counted from bar 1?
    bounds = [s["start"] for s in merged[1:]]
    if bounds and len(downbeats):
        near = [int(np.argmin(np.abs(downbeats - b))) for b in bounds]
        on_beat = [abs(downbeats[i] - b) < 0.15 for i, b in zip(near, bounds)]
        row["a1_boundaries_on_downbeat_frac"] = round(np.mean(on_beat), 3)
        row["a1_boundaries_on_8bar_grid_frac"] = round(np.mean([ok and i % 8 == 0 for i, ok in zip(near, on_beat)]), 3)
    return row


def stem_features(st: dict) -> dict:
    if "db" not in st:
        return {}
    db = {k: np.array(v) for k, v in st["db"].items()}
    downbeats, n = st["downbeats"], len(db["mix"])
    loud = db["mix"] > SILENCE_DB
    row = {}
    present = {}
    for s in STEMS:
        on = (db[s] - db["mix"] > PRESENT_DB) & loud
        present[s] = on
        idx = np.flatnonzero(on)
        row[f"stem_{s}_energy_share"] = st["energy_share"][s]
        row[f"stem_{s}_present_frac"] = round(on.mean(), 3)
        row[f"stem_{s}_first_bar"] = int(idx[0]) + 1 if len(idx) else None
        row[f"stem_{s}_last_bar"] = int(idx[-1]) + 1 if len(idx) else None
        row[f"stem_{s}_first_s"] = round(downbeats[idx[0]], 2) if len(idx) else None
        row[f"stem_{s}_bars"] = "".join("1" if x else "0" for x in on)
    row["has_vocals"] = row["stem_vocals_present_frac"] >= VOCAL_BARS
    first, last = row["stem_bass_first_bar"], row["stem_bass_last_bar"]
    row["intro_bars_before_bass"] = first - 1 if first else n
    row["outro_bars_after_bass"] = n - last if last else n
    first, last = row["stem_vocals_first_bar"], row["stem_vocals_last_bar"]
    row["intro_bars_before_vocals"] = first - 1 if first else n
    row["outro_bars_after_vocals"] = n - last if last else n
    drums_only = present["drums"] & ~present["bass"] & ~present["vocals"]
    row["intro_drums_only_bars"] = int(np.argmin(drums_only)) if not drums_only.all() else n
    # Loudness of the mix bar by bar; energy_bars scales each bar 0-9 between the track's own quiet and loud.
    mix = db["mix"][loud] if loud.any() else db["mix"]
    p5, p10, p50, p90, p95 = np.percentile(mix, [5, 10, 50, 90, 95])
    row.update({"energy_bar_db_p10": round(p10, 1), "energy_bar_db_p50": round(p50, 1),
                "energy_bar_db_p90": round(p90, 1), "energy_bar_range_db": round(p90 - p10, 1),
                "energy_peak_bar": int(np.argmax(db["mix"])) + 1})
    scaled = np.clip((db["mix"] - p5) / max(p95 - p5, 1e-6) * 9, 0, 9).round().astype(int)
    row["energy_bars"] = "".join(map(str, scaled))
    return row


def model_features(m: dict) -> dict:
    style = m["style"]
    top = sorted(style.items(), key=lambda kv: -kv[1])
    parents = Counter()
    for k, v in style.items():
        parents[k.split("---")[0]] += v
    row = {"ml_style_top1": top[0][0], "ml_style_top1_prob": top[0][1],
           "ml_style_top5": "; ".join(f"{k} {v:.2f}" for k, v in top[:5]),
           "ml_genre_top1": parents.most_common(1)[0][0],
           # The head scores each style independently (they don't sum to 1), so take the best house style.
           "ml_house_score": max(v for k, v in style.items() if "House" in k)}
    for s in HOUSE_STYLES:
        row[f"ml_style_{s.lower().replace(' ', '_').replace('-', '_')}"] = style[f"Electronic---{s}"]
    for key, n in (("moodtheme", 5), ("instrument", 5)):
        ranked = sorted(m[key].items(), key=lambda kv: -kv[1])[:n]
        row[f"ml_{key}_top{n}"] = "; ".join(f"{k} {v:.2f}" for k, v in ranked)
    for mood in ("happy", "sad", "party", "relaxed", "aggressive"):
        row[f"ml_mood_{mood}"] = m[f"mood_{mood}"][mood]
    row.update({"ml_danceability": m["danceability"]["danceable"], "ml_voice": m["voice_instrumental"]["voice"],
                "ml_approachability": m["approachability"]["approachability"],
                "ml_engagement": m["engagement"]["engagement"],
                "ml_valence": m["emomusic"]["valence"], "ml_arousal": m["emomusic"]["arousal"]})
    return row


def dj_bpm(ess, a1):
    """One BPM for DJing: agree -> allin1's; half/double -> the one in the house range."""
    inside = lambda b: HOUSE_BPM[0] <= b <= HOUSE_BPM[1]
    if ess and a1:
        r = a1 / ess
        if abs(r - 1) <= 0.02:
            return a1, "agree"
        if abs(r - 2) <= 0.04 or abs(r - 0.5) <= 0.01:
            return (a1 if inside(a1) else ess), "half/double, took the house-range one"
        return (ess if inside(ess) and not inside(a1) else a1), "disagree"
    b = a1 or ess
    return (b, "allin1 only" if a1 else "essentia only") if b else (None, None)


# ---------------------------------------------------------------- assemble

def main():
    manifests = [pd.read_csv(META / f"{s}_manifest.csv", dtype={"source_id": str}) for s in ("fma", "jamendo")]
    files = pd.concat(manifests, ignore_index=True)
    meta = {("fma", str(k)): v for k, v in fma_meta([int(i) for i in files[files.source == "fma"].source_id]).items()}
    meta.update({("jamendo", k): v for k, v in jamendo_meta(list(files[files.source == "jamendo"].source_id)).items()})

    rows = []
    for f in files.itertuples():
        path = ROOT / f.path
        uid = f"{f.source}_{path.stem}"
        row = {"track_uid": uid, "source": f.source, "source_track_id": f.source_id}
        row.update(meta[(f.source, f.source_id)])
        row.update(file_facts(path))
        e = load_json(ANALYSIS / "essentia" / f"{uid}.json")
        a = load_json(ANALYSIS / "allin1" / f.source / f"{path.stem}.json")
        st = load_json(ANALYSIS / "stems" / f"{uid}.json")
        m = load_json(ANALYSIS / "models" / f"{uid}.json")
        if e:
            row["duration_s"] = round(e["metadata.audio_properties.length"], 2)
            row.update(essentia_features(e))
        if a:
            row.update(allin1_features(a))
        if st:
            row.update(stem_features(st))
        if m:
            row.update(model_features(m))
        row["dj_bpm"], row["dj_bpm_basis"] = dj_bpm(row.get("ess_rhythm_bpm"), row.get("a1_bpm_from_beats"))
        if row["dj_bpm"]:
            row["dj_bpm"] = round(row["dj_bpm"], 2)
        row["analyzed"] = "+".join(k for k, v in (("essentia", e), ("allin1", a), ("stems", st), ("models", m)) if v)
        rows.append(row)

    # Flags and duplicates, now that every row is in.
    seen = {}
    for row in rows:
        flags = []
        if row["dj_bpm_basis"] == "disagree":
            flags.append("bpm trackers disagree")
        if row.get("a1_bars_4beat_frac") is not None and row["a1_bars_4beat_frac"] < 0.8:
            flags.append("not steady 4/4")
        if row.get("ml_house_score") is not None and row["ml_house_score"] < 0.15:
            flags.append("model hears little house")
        if row.get("duration_s") and row["duration_s"] < 180:
            flags.append("under 3 min")
        norm = re.sub(r"[^a-z0-9]", "", f"{row['artist']}|{row['title']}".lower())
        if norm in seen:
            row["duplicate_of"] = seen[norm]
            flags.append("duplicate")
        else:
            seen[norm] = row["track_uid"]
        row["flags"] = "; ".join(flags)

    df = pd.DataFrame(rows).map(clean)
    first = ["track_uid", "source", "title", "artist", "album", "release_date", "duration_s", "dj_bpm", "dj_bpm_basis",
             "key", "camelot", "key_strength", "source_genres", "ml_style_top1", "ml_house_score", "has_vocals",
             "intro_bars_before_bass", "intro_bars_before_vocals", "outro_bars_after_vocals", "ml_danceability",
             "ml_valence", "ml_arousal", "ess_lowlevel_loudness_ebu128_integrated", "license", "flags", "analyzed"]
    order = [c for c in first if c in df] + [c for c in df if c not in first]
    df = df[order].sort_values(["source", "track_uid"])
    df.to_csv(ROOT / "tracks.csv", index=False)
    write_dictionary(list(df.columns))
    print(f"wrote {len(df)} tracks x {len(df.columns)} columns to {ROOT / 'tracks.csv'}")
    print(df.analyzed.value_counts().to_string())


# ---------------------------------------------------------------- column dictionary

DOC = {
    "track_uid": "Unique id: <source>_<file stem>",
    "source": "Where the track came from: fma (Free Music Archive dataset) or jamendo (MTG-Jamendo dataset)",
    "source_track_id": "The track's id in its source dataset",
    "title": "Track title (source metadata)", "artist": "Artist name (source metadata)",
    "album": "Album title (source metadata)", "release_date": "Album release date, else upload date (YYYY-MM-DD)",
    "duration_s": "Decoded audio length (seconds)",
    "dj_bpm": "One tempo for DJing: allin1's beat-derived BPM when both trackers agree; on a half/double "
              "disagreement, whichever lies in 100-140 BPM",
    "dj_bpm_basis": "How dj_bpm was chosen: agree, half/double resolution, disagree (flagged), or one tracker only",
    "key": "Key from Essentia's KeyExtractor with the EDM-tuned 'edma' profile",
    "camelot": "key on the Camelot wheel (8A = A minor); neighbours (+-1, same number A/B) mix harmonically",
    "key_strength": "Confidence of the edma key estimate (0-1)",
    "key_profiles_agreeing": "How many of three key profiles (edma, Krumhansl, Temperley) give the same key (1-3)",
    "license": "Creative Commons license the artist chose; check it before sharing mixes",
    "license_url": "Link to that license", "source_url": "Track page at the source (credit this)",
    "album_url": "Album page at the source", "artist_url": "Artist page at the source",
    "flags": "Warnings: bpm trackers disagree, not steady 4/4, model hears little house, under 3 min, duplicate",
    "analyzed": "Which analyses are present for this track: essentia, allin1, stems, models",
    "duplicate_of": "track_uid of an earlier row with the same artist and title",
    "source_genre_top": "FMA: top-level genre; Jamendo: first genre tag",
    "source_genres": "Genres the uploader tagged", "source_genres_all": "FMA: genres plus their parents",
    "source_tags": "Free-form tags at the source", "source_instrument_tags": "Jamendo instrument tags",
    "source_mood_tags": "Jamendo mood/theme tags",
    "listens": "FMA play count (2017 snapshot)", "favorites": "FMA favourites", "interest": "FMA interest score",
    "comments": "FMA comment count", "album_listens": "FMA album play count", "album_favorites": "FMA album favourites",
    "track_number": "Position on the album", "album_type": "FMA album type (Album, Single Tracks, ...)",
    "album_tracks": "Tracks on the album", "explicit": "FMA explicit-content flag",
    "source_instrumental": "FMA uploader's instrumental flag", "composer": "Composer credit", "lyricist": "Lyricist credit",
    "publisher": "Publisher credit", "copyright_c": "Copyright (C) line", "copyright_p": "Phonographic (P) line",
    "language": "Language code", "date_recorded": "Recording date given by uploader", "date_uploaded": "Upload date",
    "album_producer": "Album producer credit", "album_engineer": "Album engineer credit",
    "artist_id": "Artist id at the source", "artist_location": "Artist's stated location",
    "artist_latitude": "Artist latitude", "artist_longitude": "Artist longitude", "artist_website": "Artist website",
    "artist_wikipedia": "Artist Wikipedia page", "artist_active_year_begin": "Year the artist started",
    "artist_active_year_end": "Year the artist stopped", "artist_members": "Band members",
    "artist_associated_labels": "Labels the artist is associated with", "artist_related_projects": "Related projects",
    "artist_tags": "Tags on the artist", "artist_favorites": "FMA artist favourites",
    "artist_since": "Date the artist joined FMA",
    "file_path": "Audio file, relative to data/house", "file_bytes": "File size (bytes)",
    "file_sha256": "SHA-256 of the file", "id3_has_cover_art": "The MP3 embeds cover art",
    "fingerprint_chromaprint_120s": "Chromaprint fingerprint of the first 120 s (AcoustID lookups, duplicate checks)",
    "a1_bpm": "allin1 tempo (integer BPM)", "a1_bpm_from_beats": "60 / median beat interval (BPM, 2 decimals)",
    "a1_beats": "Beats found by allin1", "a1_bars": "Bars (downbeats) found by allin1",
    "a1_first_beat_s": "Time of the first beat (s)", "a1_first_downbeat_s": "Time of bar 1 (s)",
    "a1_beat_interval_cv": "Beat-interval spread / mean; near 0 = machine-steady tempo",
    "a1_tempo_drift_pct": "Tempo change from the first to the last quarter of the track (%)",
    "a1_beats_per_bar": "Most common beats per bar", "a1_bars_4beat_frac": "Share of full bars with exactly 4 beats",
    "a1_lead_silence_s": "Silence before the music starts (s)", "a1_tail_silence_s": "Silence after it ends (s)",
    "a1_n_segments": "Section count before merging repeats",
    "a1_sections": "Sections with start-end times (s), repeats merged; allin1 uses pop-style labels",
    "a1_section_order": "Section labels in order, repeats merged",
    "a1_intro_bars": "Bars in the opening intro section", "a1_outro_bars": "Bars in the closing outro section",
    "a1_first_chorus_s": "Start of the first 'chorus' (often the main drop in house) (s)",
    "a1_first_chorus_bar": "Bar where the first 'chorus' starts", "a1_n_breaks": "Number of 'break' sections",
    "a1_boundaries_on_downbeat_frac": "Share of section changes within 0.15 s of a downbeat",
    "a1_boundaries_on_8bar_grid_frac": "Share of section changes on the 8-bar phrase grid counted from bar 1",
    "has_vocals": f"Vocals stem plays in at least {VOCAL_BARS:.0%} of bars",
    "intro_bars_before_bass": "Bars before the bass stem first plays (mix-in runway without two basslines)",
    "outro_bars_after_bass": "Bars after the bass stem last plays",
    "intro_bars_before_vocals": "Bars before vocals first play", "outro_bars_after_vocals": "Bars after vocals last play",
    "intro_drums_only_bars": "Opening bars with drums but no bass or vocals",
    "energy_bar_db_p10": "Quiet bars: 10th percentile of per-bar mix level (dBFS)",
    "energy_bar_db_p50": "Median per-bar mix level (dBFS)", "energy_bar_db_p90": "Loud bars: 90th percentile (dBFS)",
    "energy_bar_range_db": "p90 - p10 of per-bar level: how much the energy moves (dB)",
    "energy_peak_bar": "Loudest bar",
    "energy_bars": "One digit per bar, 0-9: that bar's loudness between the track's own 5th and 95th percentile",
    "ml_style_top1": "Highest-scoring of 400 Discogs styles (Essentia Discogs-EffNet)",
    "ml_style_top1_prob": "Its score (0-1; each style is scored independently, so scores don't sum to 1)",
    "ml_style_top5": "Top five styles with scores",
    "ml_genre_top1": "Top Discogs parent genre (its styles' scores summed)",
    "ml_house_score": "Best score among the 20 Discogs styles with 'House' in the name (0-1)",
    "ml_moodtheme_top5": "Top five of 56 MTG-Jamendo moods/themes", "ml_instrument_top5": "Top five of 40 instruments",
    "ml_danceability": "Probability the track is danceable", "ml_voice": "Probability it has voice (vs instrumental)",
    "ml_approachability": "Approachability (0-1): how mainstream/accessible it sounds",
    "ml_engagement": "Engagement (0-1): how much active listening it invites",
    "ml_valence": "Valence, 1 (sad/negative) to 9 (happy/positive), emoMusic model",
    "ml_arousal": "Arousal, 1 (calm) to 9 (energetic), emoMusic model",
}

ESS_BASE = {
    "bpm": "Tempo (BPM), RhythmExtractor2013", "beats_count": "Beats found", "onset_rate": "Note onsets per second",
    "danceability": "Essentia DFA danceability (about 0-3, higher = more danceable)",
    "beats_loudness": "Energy at the beats", "beats_loudness_band_ratio": "Share of beat energy in band n of 6 (1 = lowest)",
    "bpm_histogram_first_peak_bpm": "Strongest tempo candidate (BPM)", "bpm_histogram_first_peak_weight": "Its weight",
    "bpm_histogram_second_peak_bpm": "Second tempo candidate (BPM)", "bpm_histogram_second_peak_weight": "Its weight",
    "bpm_histogram_second_peak_spread": "Spread of the second peak",
    "key": "Key tonic", "scale": "major/minor", "strength": "Key confidence",
    "chords_changes_rate": "Chord changes per frame", "chords_number_rate": "Distinct chords / frames",
    "chords_strength": "Chord detection strength", "chords_key": "Key implied by the chords",
    "chords_scale": "Scale implied by the chords", "hpcp_crest": "Pitch-class profile peakiness",
    "hpcp_entropy": "Pitch-class profile entropy (higher = less tonal)", "tuning_frequency": "Tuning reference (Hz)",
    "tuning_diatonic_strength": "Fit to a diatonic scale", "tuning_equal_tempered_deviation": "Deviation from equal temperament",
    "tuning_nontempered_energy_ratio": "Energy outside equal-tempered pitches",
    "average_loudness": "Mean loudness (0-1, normalized)", "dynamic_complexity": "Loudness fluctuation (dB)",
    "integrated": "Integrated loudness (LUFS)", "loudness_range": "Loudness range (LU)",
    "momentary": "Momentary loudness (LUFS)", "short_term": "Short-term loudness (LUFS)",
    "spectral_centroid": "Spectral centroid, brightness (Hz)", "spectral_rolloff": "Frequency below which 85% of energy lies (Hz)",
    "spectral_flux": "Frame-to-frame spectral change", "spectral_complexity": "Number of spectral peaks",
    "spectral_entropy": "Spectral entropy (noisiness)", "spectral_energy": "Spectral energy", "spectral_rms": "Spectral RMS",
    "spectral_kurtosis": "Spectral kurtosis", "spectral_skewness": "Spectral skewness", "spectral_spread": "Spectral spread",
    "spectral_decrease": "Spectral decrease", "spectral_strongpeak": "Strongest spectral peak ratio",
    "spectral_energyband_low": "Energy 20-150 Hz", "spectral_energyband_middle_low": "Energy 150-800 Hz",
    "spectral_energyband_middle_high": "Energy 800-4000 Hz", "spectral_energyband_high": "Energy 4-20 kHz",
    "spectral_contrast_coeffs": "Spectral contrast, band n", "spectral_contrast_valleys": "Spectral valleys, band n",
    "zerocrossingrate": "Zero-crossing rate (noisiness/percussiveness)", "dissonance": "Sensory dissonance",
    "pitch_salience": "Pitch salience (how pitched vs noisy)", "hfc": "High-frequency content",
    "silence_rate_20dB": "Share of frames below -20 dB", "silence_rate_30dB": "Share of frames below -30 dB",
    "silence_rate_60dB": "Share of frames below -60 dB", "mfcc": "MFCC coefficient n (timbre)",
    "bit_rate": "Encoded bit rate (bit/s)", "codec": "Codec", "length": "Length (s)", "lossless": "Lossless codec (1/0)",
    "md5_encoded": "MD5 of the encoded audio", "number_channels": "Channels", "replay_gain": "ReplayGain (dB)",
    "sample_rate": "Sample rate (Hz)",
}


def describe(col: str) -> tuple:
    if col in DOC:
        group = col.split("_")[0] if col.split("_")[0] in ("a1", "ml", "stem", "echonest", "id3", "file", "artist") else "summary"
        return group, DOC[col]
    if col.startswith("ess_"):
        parts = col[4:].split("_")
        stat = next((s for s in ("mean", "stdev") if s in parts), None)
        idx = parts[-1] if parts[-1].isdigit() else None
        words = [p for p in parts if p not in ("mean", "stdev") and p != idx]
        base = next((b for b in sorted(ESS_BASE, key=len, reverse=True) if "_".join(words).endswith(b)), None)
        text = ESS_BASE.get(base, " ".join(words))
        if "_".join(words).startswith(("lowlevel_barkbands", "lowlevel_erbbands", "lowlevel_melbands")):
            text = f"{words[1]} band spectrum: {' '.join(words[2:])}"
        if idx:
            text = text.replace("band n", f"band {idx}").replace("coefficient n", f"coefficient {idx}")
        if "key_edma" in col or "key_krumhansl" in col or "key_temperley" in col:
            text += f" ({words[2]} profile)"
        return "essentia", text + (f", {stat} over frames" if stat else "")
    if col.startswith("stem_"):
        _, stem, *rest = col.split("_")
        what = {"energy_share": "share of total stem energy", "present_frac": "share of bars where it plays",
                "first_bar": "first bar it plays", "last_bar": "last bar it plays", "first_s": "time it first plays (s)",
                "bars": "one digit per bar, 1 = playing"}["_".join(rest)]
        return "stems", f"{stem.capitalize()} stem (Demucs via allin1): {what}"
    if col.startswith("a1_") and col.endswith("_s") and col[3:-2] in SECTION_LABELS:
        return "a1", f"Seconds allin1 labelled '{col[3:-2]}'"
    if col.startswith("ml_style_"):
        return "ml", f"Probability of Discogs style '{col[9:].replace('_', ' ').title()}'"
    if col.startswith("ml_mood_"):
        return "ml", f"Probability the track sounds {col[8:]}"
    if col.startswith("echonest_"):
        return "echonest", f"Echo Nest (Spotify's predecessor) {col[9:].replace('_', ' ')}, FMA subset only, 0-1 except tempo"
    if col.startswith("id3_"):
        return "id3", f"ID3 tag in the file: {col[4:]}"
    return "other", ""


def write_dictionary(columns: list):
    with open(ROOT / "columns.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["column", "group", "description"])
        for col in columns:
            w.writerow([col, *describe(col)])


if __name__ == "__main__":
    main()
