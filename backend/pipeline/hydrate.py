"""
Pulihkan frame masa lalu dari situs live (GitHub Pages) ke OUTPUT_DIR sebelum
pipeline jalan. Diperlukan karena runner GitHub Actions selalu mulai bersih —
tanpa ini, window retensi -24 jam mustahil (tak ada memori antar-run).

Hanya mengunduh frame PAST (cutoff <= valid_time < run f000); present+future akan
di-generate ulang oleh run baru yang lebih fresh. Best-effort: gagal = lewati.

URL data situs diambil dari env SITE_DATA_URL (di-set oleh workflow).
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import requests

from config import KEEP_PAST_HOURS, OUTPUT_DIR
from download import latest_available_run

SITE = os.environ.get("SITE_DATA_URL", "").rstrip("/")


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=dt.timezone.utc)


def _fetch(url: str, dest: Path) -> bool:
    try:
        r = requests.get(url, timeout=60)
    except Exception:
        return False
    if r.status_code != 200 or not r.content:
        return False
    dest.write_bytes(r.content)
    return True


def main() -> None:
    if not SITE:
        print("hydrate: SITE_DATA_URL kosong — lewati (fresh start).")
        return
    run = latest_available_run()
    cutoff = run - dt.timedelta(hours=KEEP_PAST_HOURS)
    try:
        cat = requests.get(f"{SITE}/catalog.json", timeout=60).json()
    except Exception as e:
        print(f"hydrate: catalog live tak terbaca ({e}) — fresh start.")
        return

    n = 0
    for layer in cat.get("layers", {}).values():
        for fr in layer.get("frames", []):
            vt = _parse(fr["valid_time"])
            if not (cutoff <= vt < run):        # hanya frame masa lalu yg dipertahankan
                continue
            preview = fr.get("preview_image")
            if not preview:
                continue
            stem = Path(preview).stem                                   # buang ekstensi (.png/.webp)
            base = stem[:-len("_preview")] if stem.endswith("_preview") else stem
            files = [f"{base}.json", preview]
            if fr.get("data_image"):
                files.append(fr["data_image"])
            if fr.get("velocity_json"):
                files.append(fr["velocity_json"])
            if all(_fetch(f"{SITE}/{fn}", OUTPUT_DIR / fn) for fn in files):
                n += 1
    print(f"hydrate: {n} frame masa lalu dipulihkan dari {SITE} (cutoff {cutoff:%Y-%m-%d %HZ}).")


if __name__ == "__main__":
    main()
