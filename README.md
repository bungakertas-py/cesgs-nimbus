# CESGS Nimbus — Membaca Gerak Langit

Peta cuaca interaktif (Windy-like, bergaya BMKG Signature) dari data model
global gratis **GFS (NOAA/NCEP)**. Menampilkan animasi partikel angin + kontur
kecepatan, batas administrasi, dan time-slider forecast.

## Arsitektur

- **Backend** (`backend/pipeline/`) — Python: unduh subset GFS via NOMADS,
  decode GRIB2 (cfgrib+eccodes), hasilkan aset statik (PNG data, PNG heatmap
  kecepatan, JSON velocity, `catalog.json`). Tanpa database.
- **Frontend** (`frontend/`) — Leaflet + leaflet-velocity, gaya Atmospheric Glass.
  Membaca aset statik dari `backend/data/output/`.
- **Deploy** (`.github/workflows/deploy.yml`) — GitHub Actions cron tiap 6 jam
  menjalankan pipeline lalu publish frontend+data ke GitHub Pages.

## Menjalankan lokal

```bash
# 1) Backend: buat venv + install deps, lalu jalankan pipeline
python -m venv backend/venv
backend/venv/Scripts/pip install -r requirements.txt   # Windows
cd backend/pipeline && python run.py                    # unduh GFS + generate output

# 2) Frontend: server dev anti-cache dari root proyek
python dev_server.py
# buka http://127.0.0.1:8000/frontend/index.html
```

## Domain data

GFS 0.25°, region **62–180°E, 33°S–33°N** (India–Pasifik Barat, Cina Selatan–
tengah Australia). Forecast 0–72 jam tiap 3 jam (atur di `run.py`).

## Status

Fase 1 (angin permukaan) selesai. Roadmap: variabel skalar (hujan/suhu/dll),
dimensi waktu+level, multi-model (ECMWF), poles UI.
