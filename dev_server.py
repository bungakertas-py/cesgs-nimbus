#!/usr/bin/env python3
"""Dev server anti-cache untuk CESGS Nimbus.

Menyajikan ROOT proyek di http://127.0.0.1:8000 dengan header no-store, jadi
setiap perubahan file (index.html / style.css / app.js / data) langsung terlihat
tanpa perlu hard-refresh atau bersihkan cache manual.

Jalankan dari root proyek:
    python dev_server.py            # port default 8000
    python dev_server.py 8080       # port lain

Buka:  http://127.0.0.1:8000/frontend/index.html
"""
from __future__ import annotations

import http.server
import os
import socketserver
import sys

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
ROOT = os.path.dirname(os.path.abspath(__file__))


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler yang selalu menonaktifkan cache browser."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def log_message(self, fmt, *args):  # log ringkas: metode, path, status
        sys.stdout.write("  %s %s\n" % (self.command, self.path))
        sys.stdout.flush()


class DevServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True       # hindari "address already in use" saat restart
    daemon_threads = True


if __name__ == "__main__":
    with DevServer(("127.0.0.1", PORT), NoCacheHandler) as httpd:
        url = f"http://127.0.0.1:{PORT}/frontend/index.html"
        print("=" * 60)
        print(f"  CESGS Nimbus — DEV SERVER (anti-cache)")
        print(f"  Root : {ROOT}")
        print(f"  Buka : {url}")
        print(f"  Ctrl+C untuk berhenti.")
        print("=" * 60)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer dihentikan.")
