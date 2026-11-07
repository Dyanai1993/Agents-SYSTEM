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

## First-use evaluation (`first_use.mjs`)

Does a first screen tell a first-time user what it is for and what to do first? Measured in a real browser -
computed styles and layout, not source text - at 1280px and 390px:

| | Criterion | Measured |
|---|---|---|
| FU1 | One clear first action | exactly one accent-coloured, bold control, visible without scrolling |
| FU2 | Guidance | its section has a heading and an explanatory sentence (not the status line) |
| FU3 | Hierarchy | 2+ titled sections; no other control beside the first action |
| FU4 | Styled, not defaults | custom properties, a non-default font, a 44px rounded primary, a column narrower than the window |
| FU5 | Feedback | a live region next to the first action |
| FU6 | Narrow screens | no horizontal scroll at 390px; the first action still visible |
| FU7 | Direction | `dir=rtl lang=he`; English (`lang=en`) laid out left-to-right |

Targets: the reference (must pass all 7); `fixtures/bare/` - a bare but functional shell modelled on T-0026's first
screen (must fail FU1-FU4); the real T-0026 build, read-only from `tasks/T-0026/workspace/firstwords/dist` when it
is on this machine; and 7 mutants of the reference, each with one property broken (each must fail its criterion).
`node first_use.mjs <static-build-dir>` also scores any build, reported only. Whether a screen looks polished stays
a human judgement: screenshots go to `results/first-use-*.png`.

False passes for these criteria (a build or install line, a dev-server line, an echo of the claim, a grep of source,
an aggregate count) are rejected by the evidence rules: `tests/test_quality_bar.py` (`FirstUse*`).

```bash
cd evals/pwa_benchmark
PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install    # once; reuses an installed Playwright Chromium (1.65.0)
node check.mjs                                    # also runs in the unit suite: tests/test_pwa_benchmark.py
node first_use.mjs
```
