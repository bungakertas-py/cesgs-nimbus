"""
Konfigurasi pipeline CESGS Nimbus (peta cuaca GFS).

Semua parameter global (region, variabel, path output) terpusat di sini agar
mudah diperluas ke multi-model / multi-level / multi-variabel di fase berikutnya.
"""
from pathlib import Path

# ---------------------------------------------------------------------------
# Domain DATA: Samudra Hindia (India) hingga Pasifik Barat, Cina Selatan hingga
# tengah Australia. Sengaja dibuat LEBIH LUAS dari area tampilan (viewing lock
# di frontend), supaya saat pan/zoom tepi domain data tidak pernah terlihat.
# Rasio domain (~118°bujur : ~66°lintang, proyeksi Mercator ~1.75) dekat dengan
# rasio layar desktop 16:9, jadi saat difit ke layar nyaris mengisi penuh.
# Cakupan inti (yang akan tampak): ~68-174E, -28..28N (India..Pasifik Barat,
# Cina Selatan..tengah Australia). Margin data ~5-6 derajat di tiap sisi.
# ---------------------------------------------------------------------------
REGION = {
    "left_lon": 62.0,     # 62E  (Laut Arab / India barat)
    "right_lon": 180.0,   # 180E (Pasifik Barat, batas antemeridian)
    "top_lat": 33.0,      # 33N  (Cina Selatan + margin)
    "bottom_lat": -33.0,  # 33S  (melewati tengah Australia)
}

# ---------------------------------------------------------------------------
# Model GFS (NOAA/NCEP), resolusi 0.25 derajat.
# Run tersedia 00/06/12/18 UTC, biasanya rilis ~3.5-5 jam setelah jam run.
# ---------------------------------------------------------------------------
GFS = {
    "resolution": "0p25",
    "run_hours": [0, 6, 12, 18],
    # jeda aman (jam) setelah jam-run sebelum data dianggap tersedia
    "availability_lag_hours": 5,
    # endpoint NOMADS "filter" untuk subset variabel + region (file kecil)
    "filter_url": "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl",
}

# ---------------------------------------------------------------------------
# Definisi layer. Fase 1: hanya angin permukaan (10 m).
# Tiap layer mendefinisikan variabel GRIB, level, tipe render, dan rentang skala.
# ---------------------------------------------------------------------------
LAYERS = {
    "wind_surface": {
        "kind": "vector",           # butuh 2 komponen (u, v)
        "u_var": "UGRD",
        "v_var": "VGRD",
        "grib_level": "10_m_above_ground",
        "level_label": "surface",
        # rentang encoding (m/s) untuk mengemas nilai ke 0..255 di PNG
        "unscale": [-40.0, 40.0],
    },
    "rain_surface": {
        "kind": "scalar",           # HUJAN per-jam (laju), 3-jaman
        "var": "PRATE",             # laju presipitasi permukaan (kg m-2 s-1)
        "grib_level": "surface",
        "level_label": "surface",
        "to_unit": 3600.0,          # kg m-2 s-1 -> mm/jam
        "units": "mm/jam",
        # PRATE punya 2 stepType (instant & avg) di f003+; ambil laju SESAAT.
        "filter_keys": {"stepType": "instant"},
    },
    "rain_accum_surface": {
        "kind": "scalar",           # AKUMULASI HUJAN 24 JAM (harian; lihat run_rain_daily)
        "var": "PRATE",
        "grib_level": "surface",
        "level_label": "surface",
        "units": "mm/hari",
        "filter_keys": {"stepType": "instant"},
    },
    "temp_surface": {
        "kind": "scalar", "var": "TMP", "grib_level": "2_m_above_ground",
        "level_label": "surface", "offset": -273.15, "units": "°C",  # K -> °C
    },
    "humidity_surface": {
        "kind": "scalar", "var": "RH", "grib_level": "2_m_above_ground",
        "level_label": "surface", "units": "%",
    },
    "cloud_surface": {
        "kind": "scalar", "var": "TCDC", "grib_level": "entire_atmosphere",
        "level_label": "surface", "units": "%",
        "filter_keys": {"stepType": "instant"},   # TCDC entire-atm punya instant & avg
    },
    "pressure_surface": {
        "kind": "scalar", "var": "PRMSL", "grib_level": "mean_sea_level",
        "level_label": "surface", "to_unit": 0.01, "units": "hPa",  # Pa -> hPa
    },
    "storm_potential": {
        "kind": "scalar", "var": "CAPE", "grib_level": "surface",
        "level_label": "surface", "units": "J/kg",   # energi labil (potensi badai/petir)
    },
    # --- LEVEL KETINGGIAN: stratosfer 70 hPa (~18 km). Hanya angin & suhu yang
    # bermakna di sana. Dipilih lewat dropdown LEVEL di frontend; variabel lain
    # (hujan/kelembapan/awan/tekanan) diredupkan karena tak punya versi ketinggian. ---
    "wind_strato": {
        "kind": "vector",
        "u_var": "UGRD",
        "v_var": "VGRD",
        "grib_level": "70_mb",       # isobarik 70 hPa
        "level_label": "70 hPa",
        # angin stratosfer bisa lebih kencang (QBO); lebarkan rentang encoding.
        "unscale": [-50.0, 50.0],
    },
    "temp_strato": {
        "kind": "scalar", "var": "TMP", "grib_level": "70_mb",
        "level_label": "70 hPa", "offset": -273.15, "units": "°C",  # K -> °C
    },
}

# ---------------------------------------------------------------------------
# Retensi data: tiap run disimpan frame dari (run_time - KEEP_PAST_HOURS) sampai
# ujung forecast (+72 jam). Frame lebih tua dari batas itu dihapus otomatis,
# sehingga window bergeser tiap hari (-24 jam belakang ... +72 jam depan).
# ---------------------------------------------------------------------------
KEEP_PAST_HOURS = 24

# ---------------------------------------------------------------------------
# PROFIL VERTIKAL (untuk diagram Skew-T log-P per titik). Diambil T/RH/angin di
# banyak level tekanan sekaligus (1 request/waktu, multi-level), lalu di-downsample
# ke grid lebih kasar (profil atmosfer mulus, tak perlu 0.25 derajat) untuk file
# kecil yang dimuat malas saat kartu Skew-T dibuka.
# ---------------------------------------------------------------------------
PROFILE_LEVELS = [
    1000, 950, 925, 900, 850, 800, 750, 700, 650, 600, 550, 500,
    450, 400, 350, 300, 250, 200, 150, 100, 70, 50,
]  # hPa, permukaan -> atas
PROFILE_STRIDE = 4   # ambil tiap-4 titik grid 0.25 -> ~1 derajat

# ---------------------------------------------------------------------------
# Path output. Data ditulis sebagai file statis (PNG + JSON) yang nanti
# disajikan langsung ke frontend (tanpa database).
# ---------------------------------------------------------------------------
BACKEND_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BACKEND_DIR / "data"
RAW_DIR = DATA_DIR / "raw"        # file GRIB2 mentah (sementara)
OUTPUT_DIR = DATA_DIR / "output"  # PNG + JSON siap-frontend

for _d in (DATA_DIR, RAW_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)
