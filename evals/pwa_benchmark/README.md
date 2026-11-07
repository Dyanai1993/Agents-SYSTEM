# PWA reliability benchmark (T-0026)

The same real-browser checks run against two implementations of one tiny Hebrew RTL PWA:

- `app/reference/` follows the PWA-reliability playbook (`agentsys/playbooks/pwa-reliability.md`).
- `app/broken/` reproduces the flaws found in T-0026 (the FirstWords shell) on purpose: navigation goes to the network
  first, staging writes into the active cache and a failure deletes it, a "ready" marker is trusted without its
  cache, and a disappearing button leaves focus on the page body.

`check.mjs` serves builds A and B (visibly different) from memory and can serve a broken B, corrupt or drop a file,
or refuse every request ("offline"). It drives Chromium with real keyboard input:

1. A verified, the server serves a broken B: navigation to `/` and an offline reload still show A.
2. B takes over only after it is verified (before the update, `/` still shows A); A's cache is removed after.
3. A corrupt file while the manifest still says A: the active cache survives, an offline reload shows A.
4. A "ready" marker whose cache was evicted: the page says so and offers a re-download.
5. Keyboard download -> failure -> Retry: focus goes to Retry, Enter retries, focus never falls to the body.
6. Keyboard theme toggle: `aria-pressed` and the theme persist after a reload.

Plus a visual review of the reference at 390px and 1280px, light and dark (screenshots in `results/`).

**OK** means: the reference passes every check AND each flaw check (1-5) fails on the broken build - a check that
cannot fail proves nothing. Zero model calls.

```bash
cd evals/pwa_benchmark
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install    # once; reuses an installed Playwright Chromium (1.65.0)
node check.mjs                                    # also runs in the unit suite: tests/test_pwa_benchmark.py
```
