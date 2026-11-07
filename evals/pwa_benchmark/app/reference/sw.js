// REFERENCE service worker - follows the PWA-reliability playbook invariants.
// Navigation and assets come from the ACTIVE verified version first (invariant 6); only when nothing usable is active
// does the network answer. Staging requests (?stage=1) and control URLs always go to the network.
const META = "meta";

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

async function activeName() {
  const hit = await (await caches.open(META)).match("/__active");
  return hit ? await hit.text() : null;
}

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/__") || url.searchParams.has("stage")) return;
  event.respondWith((async () => {
    const name = await activeName();
    if (name && (await caches.has(name))) {
      const cache = await caches.open(name);
      const hit = await cache.match(req.mode === "navigate" ? "/index.html" : url.pathname);
      if (hit) return hit;                        // never an unverified network build over the verified one
    }
    return fetch(req);
  })());
});
