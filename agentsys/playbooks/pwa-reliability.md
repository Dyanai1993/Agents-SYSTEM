---
name: pwa-reliability
roles: architect, developer, tester
keywords: service worker, service-worker, sw.js, serviceworker, offline, cache, caches, cache storage, download, downloads, update, updates, pwa, workbox, precache, asset-manifest
---
# PWA reliability playbook (offline, downloads, updates)

## Invariants (must hold at every moment, including after a crash or a failed download)
1. ACTIVE VERSION: once a version is verified and active, it is always servable - online, offline, and while another
   version is downloading or failing. Nothing deletes or overwrites the active cache except a completed switch.
2. ISOLATED STAGING: a download writes only into its own staging cache, named for the attempt (for example
   `stage-<version>-<timestamp>`), never into a cache that may be the active one - even when the server still
   advertises the same version as the active one.
3. VERIFY, THEN ACTIVATE: every file of the new version is checked (size and hash from a manifest) before the
   active pointer moves. The pointer moves in one step, and only to a complete, verified cache.
4. SAFE CLEANUP: after a successful switch, delete only caches that are neither active nor staging-in-progress.
   On failure, delete only the failed staging cache.
5. EVICTION IS REAL: the browser may evict caches. A stored "ready" marker is a claim, not a fact - check that the
   active cache exists and holds the entry files before showing "ready"; if not, say so truthfully and offer a
   re-download.
6. NAVIGATION SERVES THE ACTIVE VERSION: navigations (`/`, deep links) are answered from the active version's
   `index.html`, not from an unverified network response. A newer build takes over only through invariant 3.

## For the Architect
- Name the data that must survive a failure (the active pointer, the active cache, user progress) and the allowed
  transitions: none -> downloading -> verified -> active; active -> staging(B) -> verified(B) -> active(B);
  staging(B) -> failed -> (active A unchanged). Put this contract in the plan.
- Give the reliability checks to the FINAL regression too, not only to one work item: they must hold after all
  items are integrated.

## For the Developer
- Implement the invariants literally; keep the staging-cache name distinct from the active-cache name in code.
- Before reporting done, exercise a failure path yourself: a corrupt file while the manifest still names the active
  version; a broken new build served by the server; a "ready" marker whose cache was deleted. Report what happened.

## For the Tester
Run these with two builds whose content is visibly different (A and B), in a real browser where possible:
1. A verified and active; the server now serves a broken B -> normal navigation to `/` and an offline reload still
   show A.
2. The manifest still advertises A and one asset request fails or is corrupt -> the active cache survives and an
   offline reload still works.
3. A persisted ready marker but a missing active cache -> the UI says it is not ready and offers recovery.
4. B fully verified -> B takes over; A's cache is removed only after the switch.
Prove each check would fail on the broken behaviour (for example: staging that writes into `fw-<activeVersion>`).
