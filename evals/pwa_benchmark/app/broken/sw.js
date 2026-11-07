// BROKEN service worker - reproduces T-0026's flaw: a navigation goes to the network first and falls back to the
// verified cache only when the network throws, so a broken new build replaces the verified one.
const META = "meta";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

async function activeName() {
  const hit = await (await caches.open(META)).match("/__active");
  return hit ? "fw-" + (await hit.text()) : null;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/__") || url.searchParams.has("stage")) return;
  event.respondWith((async () => {
    const name = await activeName();
    if (name) {
      const hit = await caches.match(url.pathname, { cacheName: name });
      if (hit && req.mode !== "navigate") return hit;
      if (req.mode === "navigate") {
        const shell = await caches.match("/index.html", { cacheName: name });
        try { return await fetch(req); } catch { if (shell) return shell; }
      }
    }
    return fetch(req);
  })());
});
