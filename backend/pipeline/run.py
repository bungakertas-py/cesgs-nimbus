"""
Orchestrator pipeline: ambil run GFS terbaru -> proses tiap layer & langkah
forecast -> rekonsiliasi (retensi window) -> tulis catalog.json untuk frontend.

Jalankan: python run.py
Di produksi dipanggil GitHub Actions (cron 1x/hari 04:00 WIB). Sebelum ini,
hydrate.py memulihkan frame lama dari situs live agar window -24 jam terjaga.
"""
from __future__ import annotations

import datetime as dt
import glob
import json
from pathlib import Path

import numpy as np

from config import KEEP_PAST_HOURS, LAYERS, OUTPUT_DIR, PROFILE_LEVELS
from cyclones import detect_and_track
from itcz import detect_itcz
from isobars import build_isobars
from monsoon import build_monsoon, build_monsoon_velocity
from profiles import build_profiles
from download import download_grib, latest_available_run
from process import (load_prate_mmhr, process_scalar, process_wind,
                     write_point_data, write_scalar_frame)

# Langkah forecast yang diambil (jam): 0..72 tiap 3 jam (3 hari ke depan).
FORECAST_STEPS = list(range(0, 73, 3))


def _var_names(layer: dict) -> list[str]:
    if layer["kind"] == "vector":
        return [layer["u_var"], layer["v_var"]]
    return [layer["var"]]


# Layer -> nama variabel di point_data (untuk deret-waktu per-titik).
POINT_VAR_OF = {
    "wind_surface": ("u", "v"),
    "rain_surface": ("rain",),
    "temp_surface": ("temp",),
    "humidity_surface": ("humidity",),
    "cloud_surface": ("cloud",),
    "pressure_surface": ("pressure",),
    "storm_potential": ("cape",),
}


def _collect_point(ctx: dict, meta: dict, arr: dict, pvars: tuple, fstep: int) -> None:
    if ctx["grid"] is None:
        w, s, e, nn = meta["bounds"]
        ctx["grid"] = {"west": w, "south": s, "east": e, "north": nn,
                       "width": meta["width"], "height": meta["height"]}
    ctx["times"][fstep] = meta["valid_time"]
    if len(pvars) == 2:  # angin u,v
        ctx["series"].setdefault("u", {})[fstep] = arr["u"]
        ctx["series"].setdefault("v", {})[fstep] = arr["v"]
    else:
        ctx["series"].setdefault(pvars[0], {})[fstep] = arr["values"]


def run_layer(layer_key: str, run: dt.datetime, steps: list[int], ctx: dict | None = None) -> int:
    """Unduh + proses satu layer untuk semua langkah forecast. Kembalikan jumlah frame.
    Jika ctx diberi, kumpulkan array mentah untuk point_data (deret-waktu per-titik)."""
    layer = LAYERS[layer_key]
    grib_level = layer["grib_level"]
    var_names = _var_names(layer)
    pvars = POINT_VAR_OF.get(layer_key) if ctx is not None else None
    n = 0
    for fstep in steps:
        try:
            grib = download_grib(run, fstep, grib_level, var_names)
            if layer["kind"] == "vector":
                meta, arr = process_wind(grib, layer_key, run, fstep)
                extra = f"max {meta['speed_knots_max']} kt"
            else:
                meta, arr = process_scalar(grib, layer_key, run, fstep)
                extra = f"max {meta['value_max']} {meta['units']}"
            grib.unlink(missing_ok=True)  # buang GRIB mentah, hemat disk
        except Exception as e:  # run belum lengkap / gangguan jaringan / decode
            print(f"  ! lewati {layer_key} f{fstep:03d}: {e}")
            continue
        n += 1
        print(f"  + {layer_key} f{fstep:03d} valid {meta['valid_time']}  ({extra})")
        if pvars:
            _collect_point(ctx, meta, arr, pvars, fstep)
    return n


def run_rain_daily(layer_key: str, run: dt.datetime, steps: list[int]) -> int:
    """AKUMULASI HARIAN (mm/hari) untuk layer_key: jumlahkan PRATE*3jam per tanggal,
    hasilkan 1 frame per hari PENUH (8 langkah 3-jaman). Slider = per tanggal."""
    layer = LAYERS[layer_key]
    accum: dict[str, np.ndarray] = {}
    counts: dict[str, int] = {}
    grid = None
    for fstep in steps:
        try:
            g = download_grib(run, fstep, layer["grib_level"], [layer["var"]])
            vals, grid = load_prate_mmhr(g)   # mm/jam
            g.unlink(missing_ok=True)
        except Exception as e:
            print(f"  ! lewati {layer_key} f{fstep:03d}: {e}")
            continue
        day = (run + dt.timedelta(hours=fstep)).strftime("%Y-%m-%d")
        contrib = vals * 3.0                  # mm dalam interval 3 jam
        if day in accum:
            accum[day] += contrib
            counts[day] += 1
        else:
            accum[day] = contrib
            counts[day] = 1
    n = 0
    for day in sorted(accum):
        if counts[day] < 8:                   # hari tak penuh (butuh 24 jam) -> lewati
            continue
        valid_dt = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)
        m = write_scalar_frame(accum[day], grid, layer_key, run, valid_dt,
                               layer["units"], day.replace("-", ""), {"period_hours": 24})
        n += 1
        print(f"  + {layer_key} HARIAN {day}  (max {m['value_max']} mm)")
    return n


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:00:00Z").replace(tzinfo=dt.timezone.utc)


def _frame_files(meta: dict) -> list[Path]:
    """Semua file milik satu frame (meta + gambar + velocity)."""
    files = [Path(meta["_path"])]
    for key in ("data_image", "preview_image", "velocity_json"):
        if meta.get(key):
            files.append(OUTPUT_DIR / meta[key])
    return files


def reconcile_and_catalog(run: dt.datetime) -> tuple[dict, int]:
    """Kumpulkan SEMUA frame di disk (lintas run), buang yang lebih tua dari
    (run - KEEP_PAST_HOURS), dedup per (layer, valid_time) pilih run terbaru,
    lalu susun catalog. Mengembalikan (catalog, jumlah_frame)."""
    cutoff = run - dt.timedelta(hours=KEEP_PAST_HOURS)

    metas: list[dict] = []
    for mp in glob.glob(str(OUTPUT_DIR / "*.json")):
        p = Path(mp)
        if p.name == "catalog.json" or p.name.endswith("_velocity.json"):
            continue
        try:
            m = json.loads(p.read_text())
        except Exception:
            continue
        if "valid_time" not in m or "layer" not in m:
            continue
        m["_path"] = mp
        metas.append(m)

    # 1) buang frame lebih tua dari cutoff (-24 jam)
    kept: list[dict] = []
    for m in metas:
        if _parse(m["valid_time"]) < cutoff:
            for f in _frame_files(m):
                Path(f).unlink(missing_ok=True)
        else:
            kept.append(m)

    # 2) dedup per (layer, valid_time) -> run_time terbaru menang; sisanya dihapus
    best: dict[tuple, dict] = {}
    losers: list[dict] = []
    for m in kept:
        k = (m["layer"], m["valid_time"])
        cur = best.get(k)
        if cur is None or _parse(m["run_time"]) > _parse(cur["run_time"]):
            if cur is not None:
                losers.append(cur)
            best[k] = m
        else:
            losers.append(m)
    for m in losers:
        for f in _frame_files(m):
            Path(f).unlink(missing_ok=True)

    # 3) susun catalog dari frame pemenang
    by_layer: dict[str, list[dict]] = {}
    for m in best.values():
        by_layer.setdefault(m["layer"], []).append(m)

    catalog = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "model": "GFS",
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "region": None,
        "layers": {},
    }
    total = 0
    for layer_key in LAYERS:  # jaga urutan definisi (angin dulu, lalu hujan)
        frames = by_layer.get(layer_key)
        if not frames:
            continue
        frames.sort(key=lambda m: _parse(m["valid_time"]))
        if catalog["region"] is None:
            catalog["region"] = {"bounds": frames[0]["bounds"]}
        entry = {
            "kind": frames[0]["kind"],
            "level": frames[0]["level"],
            "units": frames[0]["units"],
            "frames": [],
        }
        if frames[0].get("unscale") is not None:
            entry["unscale"] = frames[0]["unscale"]
        for m in frames:
            fr = {
                "valid_time": m["valid_time"],
                "forecast_step_hours": m["forecast_step_hours"],
                "preview_image": m["preview_image"],
            }
            for key in ("data_image", "velocity_json", "speed_knots_max", "value_max"):
                if m.get(key) is not None:
                    fr[key] = m[key]
            entry["frames"].append(fr)
        catalog["layers"][layer_key] = entry
        total += len(frames)
    return catalog, total


def main() -> None:
    run = latest_available_run()
    cutoff = run - dt.timedelta(hours=KEEP_PAST_HOURS)
    print("== Pipeline CESGS Nimbus (GFS) ==")
    print(f"Run GFS: {run:%Y-%m-%d %HZ} | langkah: {FORECAST_STEPS[0]}..{FORECAST_STEPS[-1]} jam")

    ctx = {"series": {}, "times": {}, "grid": None}   # kumpulan nilai utk point_data
    for layer_key in LAYERS:
        print(f"\nLayer: {layer_key}")
        if layer_key == "rain_accum_surface":
            run_rain_daily(layer_key, run, FORECAST_STEPS)   # akumulasi 24 jam
        else:
            run_layer(layer_key, run, FORECAST_STEPS, ctx)

    # point_data: deret-waktu semua variabel utk lookup per-titik (langkah yg ada di SEMUA var)
    common = None
    for d in ctx["series"].values():
        common = set(d) if common is None else (common & set(d))
    steps = sorted(common or [])
    if steps and ctx["grid"]:
        times = [ctx["times"][s] for s in steps]
        series = {var: [d[s] for s in steps] for var, d in ctx["series"].items()}
        sz = write_point_data(series, times, ctx["grid"])
        print(f"\npoint_data.bin.gz: {sz/1e6:.1f} MB ({len(times)} waktu, {len(series)} var)")

        # Deteksi & pelacakan siklon (indikasi GFS) -> cyclones.json
        cyc = detect_and_track(series, times, ctx["grid"])
        cyc["run_time"] = run.strftime("%Y-%m-%dT%H:00:00Z")
        cyc["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (OUTPUT_DIR / "cyclones.json").write_text(json.dumps(cyc))
        print(f"cyclones.json: {len(cyc['tracks'])} track siklon")

        # Zona ITCZ (pita + garis pertemuan angin) dari u,v yang sudah ada -> itcz.json
        itz = detect_itcz(series, times, ctx["grid"])
        itz["run_time"] = run.strftime("%Y-%m-%dT%H:00:00Z")
        itz["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (OUTPUT_DIR / "itcz.json").write_text(json.dumps(itz))
        nseg = sum(len(f) for f in itz["frames"])
        print(f"itcz.json: {nseg} segmen ({len(itz['frames'])} waktu)")

        # Garis isobar + pusat H/L (auto di layer Tekanan) -> isobars.json
        iso = build_isobars(series, times, ctx["grid"])
        iso["run_time"] = run.strftime("%Y-%m-%dT%H:00:00Z")
        iso["generated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        (OUTPUT_DIR / "isobars.json").write_text(json.dumps(iso))
        nln = sum(len(f["iso"]) for f in iso["frames"])
        print(f"isobars.json: {nln} garis ({len(iso['frames'])} waktu)")

        # Status monsun + arus angin rata-rata (animasi) -> monsoon.json + _velocity.json
        mon = build_monsoon(series, times, ctx["grid"])
        mon["run_time"] = run.strftime("%Y-%m-%dT%H:00:00Z")
        (OUTPUT_DIR / "monsoon.json").write_text(json.dumps(mon))
        mvel = build_monsoon_velocity(series, ctx["grid"], run)   # arus monsun dominan (1 medan)
        if mvel:
            (OUTPUT_DIR / "monsoon_velocity.json").write_text(json.dumps(mvel, separators=(",", ":")))
        # Swirl Borneo Vortex: medan kotak Kalimantan barat (stride halus utk pilinan)
        bvel = build_monsoon_velocity(series, ctx["grid"], run, lon_min=107.5, lon_max=117.5,
                                      lat_min=-2.5, lat_max=7.5, stride=2)
        if bvel:
            (OUTPUT_DIR / "monsoon_velocity_bv.json").write_text(json.dumps(bvel, separators=(",", ":")))
        ph = mon.get("phase") or {}
        print(f"monsoon.json: {ph.get('label', '-')} ({ph.get('season', '-')})")

        # Profil vertikal (Skew-T): unduh multi-level per waktu -> profile.bin.gz
        print("\nProfil vertikal (Skew-T):")
        psz = build_profiles(run, steps, ctx["times"])
        if psz:
            print(f"profile.bin.gz: {psz/1e6:.1f} MB ({len(steps)} waktu, {len(PROFILE_LEVELS)} level)")

    catalog, total = reconcile_and_catalog(run)
    (OUTPUT_DIR / "catalog.json").write_text(json.dumps(catalog, indent=2))
    days = sorted({f["valid_time"][:10]
                   for L in catalog["layers"].values() for f in L["frames"]})
    print(f"\nSelesai. {total} frame di katalog (retensi >= {cutoff:%Y-%m-%d %HZ}).")
    print(f"Layer: {list(catalog['layers'])} | tanggal: {days}")


if __name__ == "__main__":
    main()
