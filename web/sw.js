// Minimal app-shell service worker — caches only the static shell (HTML/
// CSS/JS/icons) for fast loading. Never intercepts /api/ or /tools/: live
// bus data must always be a real network request, never a stale cached one.
//
// Bump CACHE_VERSION on any app-shell change (a rename, a new icon, a CSS
// tweak) — that changes this file's own bytes, which is what makes the
// browser install a new service worker at all; activate() below then wipes
// the old cache so returning visitors get the update instead of a stale
// shell forever.
//
// The page itself (a navigation request) is network-first — see the fetch
// handler below — precisely so a layout/behaviour change in index.html or
// app.js reaches a returning visitor on their very next load instead of
// waiting on a background revalidation to catch up; the cache only serves
// it when the network is unreachable. Everything else (CSS/JS/icons) stays
// cache-first-with-revalidation, which is fine for assets that are either
// static or already cache-busted by a query string (see index.html's
// app.js?v=N).
const CACHE_VERSION = "helo-buskl-shell-v3";
const SHELL_FILES = [
  "/",
  "/insights",
  "/manifest.json",
  "/offline.html",
  "/favicon.svg",
  "/favicon.ico",
  "/apple-touch-icon.png",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-512-maskable.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_VERSION)
      .then((cache) => cache.addAll(SHELL_FILES))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/tools/")) return; // always live

  const isNavigation = event.request.mode === "navigate";

  if (isNavigation) {
    // Network-first for the page itself: a layout/behaviour change lives in
    // index.html/app.js, so a returning visitor must get the live version
    // whenever the network is reachable at all. The cache here is strictly
    // an offline fallback (and a same-tick placeholder while the request is
    // in flight isn't needed — this is a page nav, not a paint-blocking
    // asset), never preferred over a live fetch the way the block below
    // prefers cache for CSS/JS/icons.
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => caches.match(event.request).then((cached) => cached || caches.match("/offline.html")))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => {
      const network = fetch(event.request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_VERSION).then((cache) => cache.put(event.request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
