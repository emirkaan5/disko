"""Download full-length MTG-Jamendo house tracks, up to a byte budget.

MTG-Jamendo ships its 55k tracks (320 kbps MP3) as 100 tar archives on cdn.freesound.org, one per
folder (track id mod 100). Tar has no index, so we walk an archive's 512-byte headers with range
requests to find our members, then fetch only those bytes and check each file's SHA-256 against
the dataset's list. Folders holding the most house tracks go first; since a folder is just
track id mod 100, that doesn't bias the pick.

    .venv-audio/bin/python scripts/house/fetch_jamendo.py --budget-gb 3.25
"""
import argparse
import csv
import hashlib
import math
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
META = ROOT / "meta" / "jamendo"
TAR_URL = "https://cdn.freesound.org/mtg-jamendo/raw_30s/audio/raw_30s_audio-{:02d}.tar"
HOUSE_TAGS = {"genre---house", "genre---deephouse", "genre---techhouse", "genre---electrohouse",
              "genre---acidhouse", "genre---progressivehouse", "genre---technohouse", "genre---dancehouse"}
MIN_S, MAX_S = 150, 900
BYTES_PER_S = 40_000  # 320 kbps, for estimating before the exact sizes are known

_local = threading.local()


def get_range(url: str, start: int, end: int) -> bytes:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    r = _local.s.get(url, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    r.raise_for_status()
    return r.content


def candidates() -> dict:
    """{folder: [row, ...]} for house-tagged tracks of usable length."""
    by_folder = defaultdict(list)
    with open(META / "raw_30s_cleantags.tsv") as f:
        next(f)
        for line in f:
            track_id, artist_id, album_id, path, duration, *tags = line.rstrip("\n").split("\t")
            if HOUSE_TAGS & set(tags) and MIN_S <= float(duration) <= MAX_S:
                by_folder[int(path.split("/")[0])].append(
                    {"track_id": track_id, "path": path, "duration": float(duration), "tags": " ".join(tags)})
    return by_folder


def scan(folder: int, wanted: set) -> dict:
    """Walk one archive's tar headers; return {member path: (data offset, size)} for wanted members."""
    url = TAR_URL.format(folder)
    found, off, next_name = {}, 0, None
    while len(found) < len(wanted):
        h = get_range(url, off, off + 511)
        if len(h) < 512 or not h.strip(b"\0"):
            break  # end of archive
        size = int(h[124:136].split(b"\0")[0].strip() or b"0", 8)
        kind = h[156:157]
        data = off + 512
        if kind in (b"L", b"x"):
            # GNU long name / pax header: the next member's real name is in this entry's data.
            body = get_range(url, data, data + size - 1)
            if kind == b"L":
                next_name = body.rstrip(b"\0").decode()
            else:
                for rec in body.decode().splitlines():
                    if " path=" in rec:
                        next_name = rec.split(" path=", 1)[1]
        else:
            name = next_name or h[:100].rstrip(b"\0").decode()
            next_name = None
            if kind in (b"0", b"\0") and name in wanted:
                found[name] = (data, size)
        off = data + math.ceil(size / 512) * 512
    return found


def fetch(folder: int, offset: int, size: int, sha256: str, dest: Path) -> int:
    if dest.exists() and dest.stat().st_size == size:
        return size
    data = get_range(TAR_URL.format(folder), offset, offset + size - 1)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise IOError(f"SHA-256 mismatch for {dest.name}")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(dest)
    return size


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-gb", type=float, default=3.25)
    ap.add_argument("--workers", type=int, default=12)
    args = ap.parse_args()
    budget = args.budget_gb * 1e9

    by_folder = candidates()
    print(f"{sum(map(len, by_folder.values()))} MTG-Jamendo house candidates in {len(by_folder)} folders")
    with open(META / "raw_30s_audio_sha256_tracks.txt") as f:
        sha = dict(reversed(line.split()) for line in f)

    # Scan just enough folders to cover the budget, with 15% slack for the size estimate.
    folders, est = [], 0
    for folder in sorted(by_folder, key=lambda k: -len(by_folder[k])):
        if est >= budget * 1.15:
            break
        folders.append(folder)
        est += sum(r["duration"] * BYTES_PER_S for r in by_folder[folder])
    print(f"scanning {len(folders)} archives")
    located = {}
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(scan, k, {r["path"] for r in by_folder[k]}): k for k in folders}
        for f in as_completed(futs):
            located.update(f.result())
            print(f"  scanned folder {futs[f]:02d}", flush=True)

    chosen, total = [], 0
    for folder in folders:
        for r in by_folder[folder]:
            if r["path"] not in located:
                continue
            offset, size = located[r["path"]]
            if total + size > budget:
                break
            chosen.append((folder, offset, size, r))
            total += size
    print(f"picked {len(chosen)} tracks, {total / 1e9:.2f} GB")

    out = ROOT / "audio" / "jamendo"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {}
        for folder, offset, size, r in chosen:
            dest = out / r["path"].split("/")[1]
            futs[pool.submit(fetch, folder, offset, size, sha[r["path"]], dest)] = (dest, r)
        for f in as_completed(futs):
            dest, r = futs[f]
            try:
                size = f.result()
            except Exception as e:
                print(f"  FAILED {r['track_id']}: {e}")
                continue
            rows.append({"source": "jamendo", "source_id": r["track_id"], "path": f"audio/jamendo/{dest.name}",
                         "bytes": size, "tags": r["tags"]})
            if len(rows) % 25 == 0:
                print(f"  {len(rows)}/{len(chosen)}", flush=True)

    rows.sort(key=lambda r: r["source_id"])
    with open(ROOT / "meta" / "jamendo_manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"done: {len(rows)} files, {sum(r['bytes'] for r in rows) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
