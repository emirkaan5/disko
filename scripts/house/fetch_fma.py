"""Download full-length FMA house tracks, most-listened first, up to a byte budget.

FMA ships every track in one 879 GiB zip (fma_full.zip). We read its central directory with range
requests, then fetch only the members we picked, each checked against its zip CRC-32.

Picks: tracks tagged House (FMA genre 182; its "Chill-out" child is not house), whose tags are all
electronic (genre_top == Electronic), 150-900 s long.

    .venv-audio/bin/python scripts/house/fetch_fma.py --budget-gb 3.25
"""
import argparse
import ast
import bz2
import csv
import struct
import threading
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests
from remotezip import RemoteZip

ROOT = Path(__file__).resolve().parents[2] / "data" / "house"
URL = "https://os.unil.cloud.switch.ch/fma/fma_full.zip"
HOUSE = 182
MIN_S, MAX_S = 150, 900

_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        _local.s = requests.Session()
    return _local.s


def get_range(start: int, end: int) -> bytes:
    r = session().get(URL, headers={"Range": f"bytes={start}-{end}"}, timeout=120)
    r.raise_for_status()
    return r.content


def candidates() -> pd.DataFrame:
    t = pd.read_csv(ROOT / "meta" / "fma" / "tracks.csv", index_col=0, header=[0, 1], low_memory=False)
    genres = t[("track", "genres")].map(ast.literal_eval)
    keep = (genres.map(lambda g: HOUSE in g)
            & (t[("track", "genre_top")] == "Electronic")
            & t[("track", "duration")].between(MIN_S, MAX_S))
    return t[keep].sort_values(("track", "listens"), ascending=False)


def member_name(track_id: int) -> str:
    s = f"{track_id:06d}"
    return f"fma_full/{s[:3]}/{s}.mp3"


def fetch(info, dest: Path) -> int:
    """Fetch one zip member by byte range and check its CRC-32."""
    if dest.exists() and dest.stat().st_size == info.file_size:
        return info.file_size
    # Local file header: 30 fixed bytes, then the name and extra field, then the data.
    head = get_range(info.header_offset, info.header_offset + 29)
    name_len, extra_len = struct.unpack("<HH", head[26:30])
    start = info.header_offset + 30 + name_len + extra_len
    data = get_range(start, start + info.compress_size - 1)
    if info.compress_type == zipfile.ZIP_BZIP2:  # how fma_full.zip stores its MP3s
        data = bz2.decompress(data)
    elif info.compress_type == zipfile.ZIP_DEFLATED:
        data = zlib.decompress(data, -15)
    if zlib.crc32(data) != info.CRC:
        raise IOError(f"CRC mismatch for {info.filename}")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.rename(dest)
    return len(data)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--budget-gb", type=float, default=3.25)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    picks = candidates()
    print(f"{len(picks)} FMA house candidates")
    with RemoteZip(URL) as z:
        members = {i.filename: i for i in z.infolist()}

    chosen, total = [], 0
    for tid in picks.index:
        info = members.get(member_name(tid))
        if info is None:
            continue  # listed in tracks.csv but missing from the audio zip
        if total + info.file_size > args.budget_gb * 1e9:
            break
        chosen.append((tid, info))
        total += info.file_size
    print(f"picked {len(chosen)} tracks, {total / 1e9:.2f} GB")

    out = ROOT / "audio" / "fma"
    out.mkdir(parents=True, exist_ok=True)
    rows, done = [], 0
    with ThreadPoolExecutor(args.workers) as pool:
        futs = {pool.submit(fetch, info, out / f"{tid}.mp3"): (tid, info) for tid, info in chosen}
        for f in as_completed(futs):
            tid, info = futs[f]
            try:
                size = f.result()
            except Exception as e:
                print(f"  FAILED {tid}: {e}")
                continue
            done += 1
            rows.append({"source": "fma", "source_id": tid, "path": f"audio/fma/{tid}.mp3", "bytes": size,
                         "listens": int(picks.loc[tid, ("track", "listens")])})
            if done % 25 == 0:
                print(f"  {done}/{len(chosen)}", flush=True)

    rows.sort(key=lambda r: -r["listens"])
    with open(ROOT / "meta" / "fma_manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"done: {len(rows)} files, {sum(r['bytes'] for r in rows) / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
