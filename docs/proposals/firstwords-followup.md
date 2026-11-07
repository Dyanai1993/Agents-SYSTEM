# Proposal: FirstWords follow-up - offline recovery, then first-screen polish

Status: **proposed, not started.** Each task is a live run on the Max subscription; the owner submits and starts it.
Lessons, audio, progress tracking and accounts stay in their later stages and are out of scope for both tasks.

## Why

The benchmark found two kinds of problems in T-0026 (the delivered shell, kept read-only):

- **Offline recovery can lose the working copy.** Five flaws, each reproduced and caught in
  `evals/pwa_benchmark/check.mjs`:
  - Navigation goes to the network first.
  - Staging writes into the active cache, and a failed download deletes it.
  - A "ready" marker is trusted even when its cache is gone.
  - Focus falls to the page body when Download disappears.
  - A manifest link was counted as proof that the app installs.
- **The first screen does not tell a first-time user what to do.** `evals/pwa_benchmark/first_use.mjs` scores the
  real T-0026 build **1/7**:
  - no prominent first action
  - no guidance
  - every feature has equal weight
  - browser-default font
  - a full-width column

The pending draft D-0006 is a visual pass only. It assumes "only restyled, with no change to behaviour", so it fixes
neither problem.

## Recommendation: two small tasks, in this order

**A before B.** A fixes a path that deletes the user's working offline copy. B then lays out and styles the final
screen, including A's progress, failure and retry states. Each task stays small (A has 5 criteria, B has 9 including
the regression), which avoids the oversized loop T-0025 needed narrowing for.

### Task A - offline recovery (start from the T-0026 repository, branch `agentsys/T-0026`)

Goal: the offline copy is never lost or replaced by an unverified build. Every failure says what happened and
offers the one action that fixes it.

Constraints:
- Build on T-0026's project. Add no lessons, audio or new content.
- Everything T-0026 checked still passes: build, type checks, tests, RTL, themes, keyboard, 400% zoom.
- Checks run in a real browser with real clicks and key presses. No event interception before the app.
- Phone install and hosted HTTPS are reported as unverified, never inferred.

Acceptance criteria:
1. Version A is verified and the server serves a broken version B. Navigating to `/` shows A, and so does an
   offline reload.
2. A new version downloads into its own staging cache. It becomes active only after the size and SHA-256 of every
   file match the manifest. Until then, `/` still shows the old version. After the switch, the old cache is deleted.
3. A download fails (a corrupt or missing file):
   - The active cache is untouched, and an offline reload still shows the working version.
   - The screen says in plain Hebrew what failed and offers "נסו שוב".
4. "Ready for offline use" appears only when the active cache really holds every file. If the cache was evicted,
   the screen says the copy must be downloaded again and offers a re-download.
5. With the keyboard:
   - Activating Download moves focus to the progress region, then to the result message or to Retry.
   - Enter on Retry retries.
   - Focus never falls to the page body.

### Task B - first screen and visual polish (D-0006 revision 4; start from Task A's branch)

D-0006 now holds this requirement. A blocking question keeps it from being submitted until Task A is delivered and
the draft's project repository points at Task A's branch.

Goal: a first-time adult learner understands within five seconds what the app is for and what to do first. The
design is calm and modern.

Constraints:
- Keep D-0006's constraints:
  - tokens in one file
  - Assistant and Inter bundled with their licence files, working offline
  - one centred column of at most 720px
  - light, dark and high-contrast themes, chosen in the header (your earlier decision in D-0006)
  - no mascots
- Drop D-0006's assumption that only styling changes. The screen hierarchy changes.

Acceptance criteria (a summary; D-0006 has the exact nine):
1. At 390px and 1280px, the first screen shows exactly one prominent first action ("הורדה לשימוש ללא חיבור") in the
   first viewport, under a heading and one sentence that explains it, with its progress and result message beside it.
2. Nothing else sits in the first action's section. The update check and other rarely used controls are in a
   separate titled settings section. The header shows the app name and the theme selector.
3. `tokens.css` defines colours, an 8px spacing scale, radii and type sizes. A check script fails the build when any
   other CSS file has a hard-coded colour.
4. Assistant and Inter are bundled locally with their licence files, and the app renders with them while offline.
5. Buttons are at least 44px tall and have hover, focus and pressed states. A script checks every text/background
   pair in light, dark and high-contrast against 4.5:1, and fails if any pair is below it.
6. At 390px there is no horizontal scroll. Hebrew is right-to-left, and English words are left-to-right.
7. Playwright saves screenshots at 390x844 and 1280x800, light and dark, to `firstwords/screenshots/`. The visual
   judgement is reported as unverified (visual), for the owner.
8. Every check that T-0026 and Task A defined still passes.

## How the owner checks the result (no model calls)

- After Task B, build the project and run `node evals/pwa_benchmark/first_use.mjs <repo>/firstwords/dist`. The target
  is **7/7** (today 1/7).
- For Task A, compare the Tester's checks one by one with scenarios 1-5 of `check.mjs`. Each one must be a real
  browser run with its own output.
- Open the screenshots. A screen that passes 7/7 can still look dull, and only you can judge that.

## Not decided here

- When to run each task. Each live run needs your quota authorization.
