// Service worker di Bus Ancona: orari e app disponibili anche senza rete.
// Per forzare un aggiornamento della cache cambia il numero di versione qui sotto.
const V = "bus-v2", T = "bus-tiles-v1";
const SHELL = ["./", "index.html", "maplibre-gl.js", "maplibre-gl.css", "manifest.webmanifest", "icon-192.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(V).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(k => Promise.all(k.filter(x => x !== V && x !== T).map(x => caches.delete(x))))
    .then(() => self.clients.claim()));
});

async function trim(nome, max) {
  const c = await caches.open(nome), k = await c.keys();
  if (k.length > max) await Promise.all(k.slice(0, k.length - max).map(x => c.delete(x)));
}
// rete per prima (dati freschi), cache se sei offline
async function reteOCache(req, nome) {
  try {
    const r = await fetch(req);
    if (r && r.ok) { const c = await caches.open(nome); c.put(req, r.clone()); }
    return r;
  } catch (err) {
    const m = await caches.match(req);
    if (m) return m;
    throw err;
  }
}
// cache per prima (veloce), aggiornata in background
async function cacheORete(req, nome, max) {
  const c = await caches.open(nome), m = await c.match(req);
  const agg = fetch(req).then(r => {
    if (r && r.ok) { c.put(req, r.clone()); if (max) trim(nome, max); }
    return r;
  }).catch(() => null);
  return m || (await agg) || Response.error();
}

self.addEventListener("fetch", e => {
  const r = e.request;
  if (r.method !== "GET") return;
  const u = new URL(r.url);
  if (u.origin === location.origin) {
    if (r.mode === "navigate") {
      e.respondWith(reteOCache(r, V).catch(() => caches.match("index.html")));
    } else if (/\/(data|avvisi|percorsi)\.json$|\/report\.txt$/.test(u.pathname)) {
      e.respondWith(reteOCache(r, V));
    } else {
      e.respondWith(cacheORete(r, V));
    }
  } else if (u.hostname === "tiles.openfreemap.org") {
    e.respondWith(cacheORete(r, T, 400));
  } else if (u.hostname === "router.project-osrm.org") {
    e.respondWith(reteOCache(r, T));
  }
});
