
const CACHE_NAME = "mandiri-taspen-shell-v6";

const APP_SHELL = [
  "/manifest.webmanifest",
  "/install.js",
  "/assets/logo-danantara-v2.svg",
  "/assets/taspen-symbol.png",
  "/assets/taspen-service-background.png",
  "/assets/mandiri-taspen-icon-180.png",
  "/assets/mandiri-taspen-icon-192.png",
  "/assets/mandiri-taspen-icon-512.png",
  "/assets/mandiri-taspen-icon-maskable-512.png"
];

// Instalasi: simpan aset statis saja.
// Jangan masukkan halaman login atau dashboard ke cache.
self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then(cache => cache.addAll(APP_SHELL))
      .then(() => self.skipWaiting())
  );
});

// Aktivasi: hapus cache versi lama milik aplikasi ini saja.
self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(cacheNames =>
        Promise.all(
          cacheNames
            .filter(name =>
              name.startsWith("mandiri-taspen-shell-") &&
              name !== CACHE_NAME
            )
            .map(name => caches.delete(name))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);

  // Jangan menangani permintaan selain GET atau permintaan ke domain lain.
  if (
    request.method !== "GET" ||
    url.origin !== self.location.origin
  ) {
    return;
  }

  // API, sesi login, dan notifikasi harus selalu ditangani server.
  // Jangan menyimpan respons API ke cache.
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // Halaman login dan halaman lain yang membutuhkan sesi
  // harus selalu diambil dari server, bukan cache.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request));
    return;
  }

  // Aset statis: gunakan cache jika tersedia.
  if (APP_SHELL.includes(url.pathname)) {
    event.respondWith(
      caches.match(request, { ignoreSearch: true })
        .then(cachedResponse => {
          if (cachedResponse) {
            return cachedResponse;
          }

          return fetch(request);
        })
    );
  }
});
