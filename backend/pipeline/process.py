"""
Processor: GRIB2 -> aset siap-frontend.

Menghasilkan, untuk satu layer angin pada satu langkah forecast:
  1. <name>.png       : PNG data (R=u, G=v terkemas) untuk engine partikel WeatherLayers GL.
  2. <name>_preview.png: PNG pratinjau kecepatan angin berwarna (skala knots) — untuk
                         verifikasi cepat oleh manusia tanpa perlu frontend.
  3. <name>.json      : metadata (bounds, dimensi, unscale, waktu run & valid).
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import warnings
from pathlib import Path

import numpy as np
import xarray as xr
from PIL import Image

from config import LAYERS, OUTPUT_DIR, REGION

warnings.filterwarnings("ignore", message="Ignoring index file")

# Skala warna kecepatan angin (knots) meniru legend BMKG Signature.
# (batas_knots, (R, G, B))
_KNOTS_SCALE = [
    (0,   (0x00, 0x30, 0x50)),   # tenang - biru gelap
    (5,   (0x2b, 0x83, 0xba)),   # biru
    (10,  (0x5a, 0xa8, 0xcf)),
    (15,  (0xab, 0xdd, 0xa4)),   # hijau muda
    (20,  (0x66, 0xbd, 0x63)),   # hijau
    (25,  (0xd9, 0xef, 0x8b)),   # kuning-hijau
    (34,  (0xfe, 0xe0, 0x8b)),   # kuning
    (48,  (0xfd, 0xae, 0x61)),   # oranye
    (64,  (0xf4, 0x6d, 0x43)),   # oranye-merah
    (80,  (0xd7, 0x30, 0x27)),   # merah
    (100, (0xa5, 0x00, 0x26)),   # merah tua
    (120, (0x7a, 0x00, 0x77)),   # ungu
]

MS_TO_KNOTS = 1.943844

# Hujan PER-JAM (mm/jam) — rainbow BMKG, ambang per-jam. Kering transparan.
_RAIN_SCALE = [
    (0.0,  (0x14, 0x37, 0x8f,   0)),   # kering = transparan
    (1.0,  (0x14, 0x37, 0x8f, 140)),
    (2.0,  (0x14, 0x37, 0x8f, 225)),   # biru tua
    (4.0,  (0x23, 0x60, 0xc8, 235)),   # biru
    (8.0,  (0x22, 0xa5, 0xe0, 240)),   # cyan
    (10.0, (0x23, 0xd3, 0xc0, 240)),   # turkis
    (15.0, (0x35, 0xc8, 0x4a, 245)),   # hijau
    (20.0, (0x8e, 0xd8, 0x2a, 245)),   # hijau-kuning
    (25.0, (0xea, 0xd8, 0x21, 248)),   # kuning
    (30.0, (0xf5, 0xa9, 0x1e, 248)),   # oranye
    (35.0, (0xf2, 0x70, 0x1c, 250)),   # oranye tua
    (40.0, (0xe4, 0x23, 0x20, 250)),   # merah
    (50.0, (0xe3, 0x3b, 0xbf, 252)),   # magenta
    (60.0, (0x8a, 0x29, 0xc8, 255)),   # ungu
]

# AKUMULASI HUJAN 24 JAM (mm/hari) — rainbow sama, ambang harian (lebih tinggi).
_RAIN_ACCUM_SCALE = [
    (0.0,   (0x14, 0x37, 0x8f,   0)),
    (2.0,   (0x14, 0x37, 0x8f, 120)),
    (5.0,   (0x14, 0x37, 0x8f, 235)),
    (10.0,  (0x23, 0x60, 0xc8, 240)),
    (20.0,  (0x22, 0xa5, 0xe0, 240)),
    (40.0,  (0x23, 0xd3, 0xc0, 242)),
    (60.0,  (0x35, 0xc8, 0x4a, 245)),
    (90.0,  (0x8e, 0xd8, 0x2a, 245)),
    (120.0, (0xea, 0xd8, 0x21, 248)),
    (150.0, (0xf5, 0xa9, 0x1e, 248)),
    (200.0, (0xf2, 0x70, 0x1c, 250)),
    (300.0, (0xe4, 0x23, 0x20, 252)),
    (400.0, (0xe3, 0x3b, 0xbf, 253)),
    (500.0, (0x8a, 0x29, 0xc8, 255)),
]

# Suhu (°C) OPAQUE: biru dingin -> merah panas.
_TEMP_SCALE = [
    (-10, (0x2b, 0x1c, 0x6b, 255)), (0, (0x25, 0x3f, 0xa0, 255)),
    (8,   (0x2b, 0x83, 0xba, 255)), (16, (0x4d, 0xaf, 0x8f, 255)),
    (22,  (0xa6, 0xd9, 0x6a, 255)), (28, (0xfe, 0xe0, 0x8b, 255)),
    (32,  (0xfd, 0xae, 0x61, 255)), (36, (0xf4, 0x6d, 0x43, 255)),
    (42,  (0xa5, 0x00, 0x26, 255)),
]

# Suhu STRATOSFER 70 hPa (°C) OPAQUE. Di sana jauh lebih dingin (~-45..-75°C)
# daripada permukaan, jadi skalanya digeser ke rentang dingin sendiri.
_TEMP_STRATO_SCALE = [
    (-78, (0x3b, 0x1c, 0x6b, 255)), (-70, (0x25, 0x3f, 0xa0, 255)),
    (-64, (0x2b, 0x83, 0xba, 255)), (-58, (0x35, 0xa0, 0x8a, 255)),
    (-52, (0xa6, 0xd9, 0x6a, 255)), (-46, (0xfe, 0xe0, 0x8b, 255)),
    (-40, (0xfd, 0xae, 0x61, 255)),
]

# Kelembapan (%) OPAQUE: coklat kering -> hijau -> biru lembap.
_HUM_SCALE = [
    (0,  (0x7a, 0x45, 0x0a, 255)), (25, (0xb9, 0x84, 0x3a, 255)),
    (50, (0x88, 0xb0, 0x55, 255)), (70, (0x35, 0x9a, 0x86, 255)),
    (85, (0x21, 0x6b, 0xb0, 255)), (100, (0x12, 0x3f, 0x86, 255)),
]

# Tutupan awan (%) RGBA: cerah transparan -> abu (makin tertutup makin pekat).
_CLOUD_SCALE = [
    (0,   (0xff, 0xff, 0xff,   0)), (20, (0xc8, 0xd0, 0xd8,  70)),
    (50,  (0xaa, 0xb4, 0xbe, 150)), (80, (0x96, 0xa0, 0xac, 205)),
    (100, (0x78, 0x82, 0x8e, 235)),
]

# Tekanan MSL (hPa) OPAQUE: rendah (badai) ungu/biru -> tinggi merah.
_PRESS_SCALE = [
    (980,  (0x5e, 0x3c, 0x99, 255)), (995, (0x35, 0x6b, 0xc4, 255)),
    (1005, (0x7d, 0xc8, 0xd8, 255)), (1013, (0xf0, 0xf0, 0xe0, 255)),
    (1020, (0xf4, 0xc0, 0x60, 255)), (1030, (0xe0, 0x5a, 0x3a, 255)),
]

# CAPE (J/kg) POTENSI BADAI: stabil transparan -> hijau (sedang) -> kuning/oranye
# (tinggi) -> merah/ungu (ekstrem, potensi badai petir kuat). Ambang mirip acuan
# konvektif: <300 lemah, ~500-1000 sedang, 1000-2500 tinggi, >2500 ekstrem.
_CAPE_SCALE = [
    (0,    (0x14, 0x37, 0x8f,   0)),   # stabil: transparan
    (500,  (0x2f, 0x9e, 0x7a,   0)),   # <500: transparan
    (1000, (0x5a, 0xc8, 0x6a, 150)),   # hijau: sedang
    (1800, (0xea, 0xd8, 0x21, 195)),   # kuning
    (2600, (0xf5, 0xa9, 0x1e, 216)),   # oranye: tinggi
    (3400, (0xe4, 0x23, 0x20, 232)),   # merah
    (4200, (0x8a, 0x29, 0xc8, 246)),   # ungu: ekstrem
]


def _load_wind(grib_path: Path) -> tuple[np.ndarray, np.ndarray, dict]:
    """Muat u/v dari GRIB, orientasikan agar baris-0 = utara, kolom-0 = barat."""
    ds = xr.open_dataset(grib_path, engine="cfgrib",
                         backend_kwargs={"indexpath": ""})
    # Komponen angin: di 10 m cfgrib menamai "u10"/"v10"; di level isobarik
    # (mis. 70 hPa) menjadi "u"/"v". Pilih yang ada agar loader dipakai kedua kasus.
    uname = "u10" if "u10" in ds else ("u" if "u" in ds else None)
    vname = "v10" if "v10" in ds else ("v" if "v" in ds else None)
    if uname is None or vname is None:
        raise RuntimeError(f"Komponen angin tak ditemukan di {grib_path.name}: {list(ds.data_vars)}")
    u = ds[uname]
    v = ds[vname]

    # pastikan longitude menaik (barat->timur)
    if float(ds.longitude[0]) > float(ds.longitude[-1]):
        u = u.isel(longitude=slice(None, None, -1))
        v = v.isel(longitude=slice(None, None, -1))
    # pastikan latitude menurun (utara di baris-0); GFS subset kita urut naik -> flip
    if float(ds.latitude[0]) < float(ds.latitude[-1]):
        u = u.isel(latitude=slice(None, None, -1))
        v = v.isel(latitude=slice(None, None, -1))

    meta = {
        "west": float(min(ds.longitude.values)),
        "east": float(max(ds.longitude.values)),
        "south": float(min(ds.latitude.values)),
        "north": float(max(ds.latitude.values)),
        "width": int(u.sizes["longitude"]),
        "height": int(u.sizes["latitude"]),
    }
    return u.values.astype("float32"), v.values.astype("float32"), meta


def _encode_vector_png(u: np.ndarray, v: np.ndarray, unscale: list[float], dest: Path) -> None:
    """Kemas u,v ke PNG RGBA: R=u, G=v (skala unscale), A=255 valid / 0 jika NaN."""
    lo, hi = unscale
    rng = hi - lo
    valid = np.isfinite(u) & np.isfinite(v)

    def enc(a):
        n = np.clip((a - lo) / rng, 0.0, 1.0)
        return (n * 255.0).round().astype("uint8")

    r = enc(np.nan_to_num(u))
    g = enc(np.nan_to_num(v))
    b = np.zeros_like(r)
    alpha = np.where(valid, 255, 0).astype("uint8")
    rgba = np.dstack([r, g, b, alpha])
    Image.fromarray(rgba, mode="RGBA").save(dest)


def _speed_to_rgb(speed_knots: np.ndarray) -> np.ndarray:
    """Petakan kecepatan (knots) ke RGB via interpolasi linear skala BMKG."""
    stops = np.array([s[0] for s in _KNOTS_SCALE], dtype="float32")
    cols = np.array([s[1] for s in _KNOTS_SCALE], dtype="float32")
    out = np.empty(speed_knots.shape + (3,), dtype="float32")
    for c in range(3):
        out[..., c] = np.interp(speed_knots, stops, cols[:, c])
    return out.round().astype("uint8")


def _save_preview(img: Image.Image, dest: Path) -> None:
    """Simpan pratinjau (heatmap untuk mata): WebP q90 bila .webp (5-7x lebih kecil dari
    PNG, visual sama). Data image (nilai angin di piksel) TETAP PNG lossless di tempat lain."""
    if str(dest).lower().endswith(".webp"):
        img.save(dest, "WEBP", quality=90, method=6, alpha_quality=100)
    else:
        img.save(dest)


def _render_speed_preview(u: np.ndarray, v: np.ndarray, dest: Path, scale: int = 4) -> None:
    """PNG pratinjau kecepatan angin berwarna (untuk mata manusia)."""
    speed_kt = np.sqrt(u ** 2 + v ** 2) * MS_TO_KNOTS
    rgb = _speed_to_rgb(np.nan_to_num(speed_kt))
    img = Image.fromarray(rgb, mode="RGB")
    if scale > 1:
        img = img.resize((img.width * scale, img.height * scale), Image.BILINEAR)
    _save_preview(img, dest)


def _load_scalar(grib_path: Path, filter_keys: dict | None = None) -> tuple[np.ndarray, dict]:
    """Muat satu variabel skalar dari GRIB, orientasi baris-0=utara, kolom-0=barat.

    filter_keys mis. {'stepType': 'instant'} untuk memilih satu varian bila GRIB
    punya beberapa (spt PRATE instant vs avg).
    """
    backend_kwargs = {"indexpath": ""}
    if filter_keys:
        backend_kwargs["filter_by_keys"] = filter_keys
    ds = xr.open_dataset(grib_path, engine="cfgrib", backend_kwargs=backend_kwargs)
    name = list(ds.data_vars)[0]            # subset kita hanya 1 variabel
    da = ds[name]
    if float(ds.longitude[0]) > float(ds.longitude[-1]):
        da = da.isel(longitude=slice(None, None, -1))
    if float(ds.latitude[0]) < float(ds.latitude[-1]):
        da = da.isel(latitude=slice(None, None, -1))
    meta = {
        "west": float(min(ds.longitude.values)),
        "east": float(max(ds.longitude.values)),
        "south": float(min(ds.latitude.values)),
        "north": float(max(ds.latitude.values)),
        "width": int(da.sizes["longitude"]),
        "height": int(da.sizes["latitude"]),
    }
    return da.values.astype("float32"), meta


def _scalar_to_rgba(values: np.ndarray, scale: list) -> np.ndarray:
    """Petakan nilai skalar ke RGBA via interpolasi linear skala warna."""
    stops = np.array([s[0] for s in scale], dtype="float32")
    cols = np.array([s[1] for s in scale], dtype="float32")   # (n, 4)
    v = np.nan_to_num(values)
    out = np.empty(v.shape + (4,), dtype="float32")
    for c in range(4):
        out[..., c] = np.interp(v, stops, cols[:, c])
    return out.round().astype("uint8")


def _render_scalar_preview(values: np.ndarray, scale: list, dest: Path, scale_up: int = 4) -> None:
    """PNG heatmap skalar berwarna (RGBA, transparan di area nilai ~0)."""
    rgba = _scalar_to_rgba(values, scale)
    img = Image.fromarray(rgba, mode="RGBA")
    if scale_up > 1:
        img = img.resize((img.width * scale_up, img.height * scale_up), Image.BILINEAR)
    _save_preview(img, dest)


_SCALAR_SCALES = {
    "rain_surface": _RAIN_SCALE,
    "rain_accum_surface": _RAIN_ACCUM_SCALE,
    "temp_surface": _TEMP_SCALE,
    "temp_strato": _TEMP_STRATO_SCALE,
    "humidity_surface": _HUM_SCALE,
    "cloud_surface": _CLOUD_SCALE,
    "pressure_surface": _PRESS_SCALE,
    "storm_potential": _CAPE_SCALE,
}


def _export_velocity_json(u: np.ndarray, v: np.ndarray, grid: dict,
                          run: dt.datetime, fstep: int, dest: Path) -> None:
    """Tulis JSON format 'velocity' (dipakai leaflet-velocity / earth wind-js).

    Urutan data: baris-major dari la1(utara) ke la2(selatan), lo1(barat) ke lo2(timur).
    u = parameterNumber 2, v = parameterNumber 3 (parameterCategory 2 = momentum).
    """
    nx, ny = grid["width"], grid["height"]
    dx = round((grid["east"] - grid["west"]) / (nx - 1), 4)
    dy = round((grid["north"] - grid["south"]) / (ny - 1), 4)
    header = {
        "lo1": grid["west"], "la1": grid["north"],
        "lo2": grid["east"], "la2": grid["south"],
        "nx": nx, "ny": ny, "dx": dx, "dy": dy,
        "parameterCategory": 2, "parameterUnit": "m.s-1",
        "refTime": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecastTime": fstep,
    }
    u_flat = np.nan_to_num(u).astype("float32").ravel(order="C").round(2).tolist()
    v_flat = np.nan_to_num(v).astype("float32").ravel(order="C").round(2).tolist()
    payload = [
        {"header": {**header, "parameterNumber": 2, "parameterNumberName": "U-component_of_wind"}, "data": u_flat},
        {"header": {**header, "parameterNumber": 3, "parameterNumberName": "V-component_of_wind"}, "data": v_flat},
    ]
    dest.write_text(json.dumps(payload, separators=(",", ":")))


def process_wind(grib_path: Path, layer_key: str, run: dt.datetime, fstep: int,
                 out_dir: Path = OUTPUT_DIR) -> dict:
    """Proses satu file GRIB angin -> PNG data + preview + velocity JSON + metadata."""
    layer = LAYERS[layer_key]
    u, v, grid = _load_wind(grib_path)

    valid_time = run + dt.timedelta(hours=fstep)
    stamp = f"{run:%Y%m%d_%H}_f{fstep:03d}"
    base = f"{layer_key}_{stamp}"

    data_png = out_dir / f"{base}.png"
    preview_png = out_dir / f"{base}_preview.webp"
    velocity_json = out_dir / f"{base}_velocity.json"
    meta_json = out_dir / f"{base}.json"

    _encode_vector_png(u, v, layer["unscale"], data_png)
    _render_speed_preview(u, v, preview_png)
    _export_velocity_json(u, v, grid, run, fstep, velocity_json)

    speed_kt = np.sqrt(u ** 2 + v ** 2) * MS_TO_KNOTS
    meta = {
        "layer": layer_key,
        "kind": layer["kind"],
        "model": "GFS",
        "level": layer["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": fstep,
        "valid_time": valid_time.strftime("%Y-%m-%dT%H:00:00Z"),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "width": grid["width"],
        "height": grid["height"],
        "unscale": layer["unscale"],
        "units": "m/s",
        "data_image": data_png.name,
        "preview_image": preview_png.name,
        "velocity_json": velocity_json.name,
        "speed_knots_max": round(float(np.nanmax(speed_kt)), 1),
    }
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta, {"u": u, "v": v}


def process_scalar(grib_path: Path, layer_key: str, run: dt.datetime, fstep: int,
                   out_dir: Path = OUTPUT_DIR) -> dict:
    """Proses satu file GRIB skalar (mis. hujan) -> PNG heatmap berwarna + metadata."""
    layer = LAYERS[layer_key]
    values, grid = _load_scalar(grib_path, layer.get("filter_keys"))
    # ke satuan tampilan: value * to_unit + offset (mis. K->°C, Pa->hPa).
    values = values * float(layer.get("to_unit", 1.0)) + float(layer.get("offset", 0.0))

    valid_time = run + dt.timedelta(hours=fstep)
    base = f"{layer_key}_{run:%Y%m%d_%H}_f{fstep:03d}"
    preview_png = out_dir / f"{base}_preview.webp"
    meta_json = out_dir / f"{base}.json"

    scale = _SCALAR_SCALES.get(layer_key, _RAIN_SCALE)
    _render_scalar_preview(values, scale, preview_png)

    meta = {
        "layer": layer_key,
        "kind": "scalar",
        "model": "GFS",
        "level": layer["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": fstep,
        "valid_time": valid_time.strftime("%Y-%m-%dT%H:00:00Z"),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "width": grid["width"],
        "height": grid["height"],
        "units": layer["units"],
        "preview_image": preview_png.name,
        "value_max": round(float(np.nanmax(values)), 2),
    }
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta, {"values": values}


def load_prate_mmhr(grib_path: Path) -> tuple[np.ndarray, dict]:
    """Muat PRATE (instant) sebagai mm/jam + grid (untuk akumulasi harian)."""
    vals, grid = _load_scalar(grib_path, {"stepType": "instant"})
    return vals * 3600.0, grid


def write_scalar_frame(values: np.ndarray, grid: dict, layer_key: str, run: dt.datetime,
                       valid_dt: dt.datetime, units: str, base_suffix: str,
                       extra: dict | None = None, out_dir: Path = OUTPUT_DIR) -> dict:
    """Render heatmap + tulis meta untuk array skalar yang SUDAH dihitung
    (dipakai mis. akumulasi hujan harian). base_suffix jadi bagian nama file."""
    scale = _SCALAR_SCALES.get(layer_key, _RAIN_SCALE)
    base = f"{layer_key}_{run:%Y%m%d_%H}_{base_suffix}"
    preview_png = out_dir / f"{base}_preview.webp"
    meta_json = out_dir / f"{base}.json"
    _render_scalar_preview(values, scale, preview_png)
    meta = {
        "layer": layer_key, "kind": "scalar", "model": "GFS",
        "level": LAYERS[layer_key]["level_label"],
        "run_time": run.strftime("%Y-%m-%dT%H:00:00Z"),
        "valid_time": valid_dt.strftime("%Y-%m-%dT%H:00:00Z"),
        "forecast_step_hours": int((valid_dt - run).total_seconds() // 3600),
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "width": grid["width"], "height": grid["height"],
        "units": units, "preview_image": preview_png.name,
        "value_max": round(float(np.nanmax(values)), 2),
    }
    if extra:
        meta.update(extra)
    meta_json.write_text(json.dumps(meta, indent=2))
    return meta


# Encoding nilai per-titik (point_data.bin.gz). value = stored*scale + offset.
_POINT_ENC = {
    "u":        {"dtype": "int16", "scale": 0.01, "offset": 0.0},
    "v":        {"dtype": "int16", "scale": 0.01, "offset": 0.0},
    "rain":     {"dtype": "int16", "scale": 0.1,  "offset": 0.0},
    "temp":     {"dtype": "int16", "scale": 0.1,  "offset": 0.0},
    "humidity": {"dtype": "uint8", "scale": 1.0,  "offset": 0.0},
    "cloud":    {"dtype": "uint8", "scale": 1.0,  "offset": 0.0},
    "pressure": {"dtype": "int16", "scale": 0.1,  "offset": 1000.0},
    "cape":     {"dtype": "int16", "scale": 1.0,  "offset": 0.0},   # CAPE J/kg (potensi badai)
}
_NP_DTYPE = {"int16": np.int16, "uint8": np.uint8}


def write_point_data(series: dict, times: list, grid: dict, out_dir: Path = OUTPUT_DIR) -> int:
    """Emit deret-waktu semua variabel untuk lookup per-titik:
    point_data.bin.gz (biner int16/uint8 terkompresi) + point_meta.json (layout).
    series = {var: [array per waktu]} berorientasi baris-0=utara, kolom-0=barat."""
    ntime, ny, nx = len(times), grid["height"], grid["width"]
    blob = bytearray()
    layout = []
    for var, enc in _POINT_ENC.items():
        arrs = series.get(var)
        if not arrs or len(arrs) != ntime:
            continue
        stacked = np.stack([np.nan_to_num(a) for a in arrs]).astype("float32")  # (t,ny,nx)
        stored = np.round((stacked - enc["offset"]) / enc["scale"]).astype(_NP_DTYPE[enc["dtype"]])
        b = stored.tobytes(order="C")
        layout.append({"var": var, "dtype": enc["dtype"], "scale": enc["scale"],
                       "offset": enc["offset"], "byteOffset": len(blob), "byteLength": len(b)})
        blob += b
    (out_dir / "point_data.bin.gz").write_bytes(gzip.compress(bytes(blob), compresslevel=6))
    meta = {
        "bounds": [grid["west"], grid["south"], grid["east"], grid["north"]],
        "nx": nx, "ny": ny,
        "dx": round((grid["east"] - grid["west"]) / (nx - 1), 4),
        "dy": round((grid["north"] - grid["south"]) / (ny - 1), 4),
        "times": times, "vars": layout,
    }
    (out_dir / "point_meta.json").write_text(json.dumps(meta))
    return (out_dir / "point_data.bin.gz").stat().st_size


if __name__ == "__main__":
    import glob
    from config import RAW_DIR

    grib = sorted(glob.glob(str(RAW_DIR / "gfs_*_f000_10_m_above_ground.grib2")))[-1]
    # ekstrak run & fstep dari nama file
    name = Path(grib).stem  # gfs_YYYYMMDD_HH_fSSS_...
    parts = name.split("_")
    run = dt.datetime.strptime(parts[1] + parts[2], "%Y%m%d%H").replace(tzinfo=dt.timezone.utc)
    fstep = int(parts[3][1:])
    meta, _ = process_wind(Path(grib), "wind_surface", run, fstep)
    print("OK — metadata:")
    print(json.dumps(meta, indent=2))
