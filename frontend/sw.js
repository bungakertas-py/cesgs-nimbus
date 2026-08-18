/* Service worker CESGS Nimbus — cache SHELL app (berversi), data cuaca TETAP
 * online. Naikkan VERSION tiap rilis frontend agar user dapat versi terbaru
 * (cache lama dihapus saat activate). */
const VERSION = "v7";   // dinaikkan saat ganti nama ke CESGS Nimbus
const CACHE = "cesgs-nimbus-" + VERSION;

// Shell same-origin (relatif thd scope frontend/). Data model (../backend/…)
// berada DI LUAR path frontend → sengaja tak di-cache (lihat handler fetch).
const SHELL = [
  "./", "./index.html", "./style.css", "./app.js", "./skewt.js",
  "./cesgs-mark.png", "./manifest.webmanifest",
  "./icon-192.png", "./icon-512.png",
  "./data/world_countries.geojson", "./data/idn_provinces.geojson", "./data/id_places.json",
];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;      // CDN (unpkg/fonts) → biar network
  if (url.pathname.includes("/backend/")) return;  // DATA CUACA → selalu online, jangan cache
  // Shell: sajikan dari cache dulu (cepat), sambil perbarui cache dari network.
  e.respondWith(
    caches.match(req).then((cached) => {
      const net = fetch(req)
        .then((res) => {
          if (res && res.ok) caches.open(CACHE).then((c) => c.put(req, res.clone()));
          return res;
        })
        .catch(() => cached);
      return cached || net;
    })
  );
});
