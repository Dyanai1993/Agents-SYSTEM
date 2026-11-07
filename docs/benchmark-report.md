# Benchmark report: raising the quality bar with T-0026

The owner's brief (2026-11-07) used T-0026, the FirstWords shell, as the benchmark:
- The delivered project stays a read-only baseline. Regressions use isolated fixtures.
- FakeBackend proves wiring, not better model output.
- Tool permissions, the model policy, billing guards and call limits stay as they are. Skill selection makes no
  model call.

Branch `claude/fix-timeout-and-retry`. Commits:

| Commit | Content |
|---|---|
| `74b5b20` | Phases 1-2: prompts, playbooks, evidence rules |
| `599851a` | Phase 3: PWA reliability checks |
| `81ae457` | Phase 4: first-use evaluation and false-pass rules |
| this commit | Phase 5: proposal, plus this report |

## Evidence at a glance

| Check | Result |
|---|---|
| `evals/pwa_benchmark/check.mjs`: same browser checks against a reference and a T-0026-like broken build | reference **6/6**; broken build: **5/5** flaw checks fail |
| `evals/pwa_benchmark/first_use.mjs`: seven first-use criteria measured in the browser | reference **7/7**; bare shell **1/7**; real T-0026 build **1/7**; **7/7** mutants caught |
| Unit suite (both browser evaluations included when Playwright is installed) | **381 pass**, 3 skipped |
| Model calls made by any of the above | **0** |

"A check that cannot fail proves nothing": every browser check is shown to fail on the broken build or on a mutant
built for it.

## By role

### Requirements Assistant and intake (`assistant.md` rules 11-12, `orchestrator.md` rules 7-8)
- **Changed:**
  - For anything with a screen, the draft captures who the user is, the one first useful action, what a first visit
    must communicate, and what the user sees on failure and how they recover.
  - Inferred points are marked as assumptions.
  - A criterion that points to a check or document nobody has seen, or two decisions that contradict each other,
    is a blocking unknown, never a "ready" draft.
- **Why:** T-0026's requirement said nothing about the first action or about recovery, so nobody built either.
- **Evidence:** prompt text in `docs/agent-prompts-for-review.md`. Not measurable without a live run.

### Architect (`architect.md` rules 2a, 2b)
- **Changed:**
  - Read the existing workspace first, and plan changes to it, not a rewrite.
  - State the contract in the summary. For UI: the screen hierarchy, the states (first visit, progress, success,
    failure, recovery), widths, direction and focus. For stateful work: what must survive a failure, and the allowed
    transitions.
  - The important parts become item criteria, re-checked by the FINAL regression.
- **Playbooks:**
  - `architecture` (always)
  - `frontend-react`, `ui-design`, `python`, `pwa-reliability` (For the Architect), by keyword, at the plan stage

### Developer (`developer.md` rules 7-9)
- **Changed:**
  - Reuse the workspace's components and tokens.
  - Before `ok`, run the real entry point plus one failure that matters, and report real output.
  - For a screen, look at the rendered result at the agreed widths and judge it against the contract.
  - Focus goes somewhere deliberate when a control disappears.
- **Playbooks:**
  - `testing` (always)
  - `frontend-react`, `ui-design`, `python`, `pwa-reliability` (For the Developer), matched on the work item, the
    tech choices and the changed files

### Tester (`tester.md` rules 8-10; evidence rules in `agentsys/semantics.py`)
- **Changed:**
  - Drive real handlers and assert what the user sees and where focus goes.
  - Name the broken implementation each check would reject.
  - Evidence comes from the check's own run.
  - Anything not performed (device, deployment, visual, external) is `unverified` with a reason: never a pass, never
    a blocker, and shown to you in the review and the merge approval.
- **Enforced in code (false passes rejected), with one automatic redo that tells the Tester why:**
  - aggregate counts ("Ran 42 tests, OK") for a behaviour
  - a grep or listing of source or of a test name for a behaviour
  - phase 4: build, install and dev-server lines ("built in 1.24s", "ready in 312 ms")
  - phase 4: text the command echoed itself (`npm run build && echo PASS`)
  - phase 4: any grep for an on-screen criterion, even one worded "contains"
- **Playbooks:**
  - `testing` (always)
  - `pwa-reliability` (For the Tester)
  - since phase 4, `ui-design` with a For the Tester section: how to measure FU1-FU7, and that the polish judgement
    is `unverified (visual)`
- **Evidence:**
  - `tests/test_quality_bar.py`: 18 tests, covering false passes, legitimate passes that must still pass,
    unverified handling, and a FakeBackend loop run (rejected, one redo, measured pass)

### Security (`security.md` rules 6-7)
- **Changed:**
  - Trace each changed entry point across its trust boundary.
  - For a web app or PWA: the manifest scope, what the service worker serves (could it serve an unverified
    version?), activation and cleanup (could a failure delete the working version?), and user-data storage.
  - Losing the offline promise is a finding with a severity.
- **Playbooks:** none, by design. Security keeps its own rules.

## By playbook

| Playbook | Roles | Added for the benchmark | Shown by |
|---|---|---|---|
| `architecture` | Architect (always) | nothing; the contract rule lives in the Architect prompt (rule 2b) | prompt review doc |
| `frontend-react` | Architect, Developer | (earlier senior-level content) | `tests/test_skills.py` selection |
| `ui-design` | Architect, Developer, **Tester** | a screen procedure (who, first action, states, focus, evidence); the T-0026 example; a For the Tester measuring section | `first_use.mjs` (the same criteria, measured) |
| `testing` | Developer, Tester (always) | a procedure for a check that proves something; the list of what is not evidence | `tests/test_quality_bar.py` |
| `pwa-reliability` | Architect, Developer, Tester (own sections) | invariants: cache-first navigation from the active cache, staged and verified updates, failure keeps the active copy, a marker is checked against its cache | `check.mjs` scenarios 1-4 |
| `python` | Architect, Developer | (earlier content) | selection tests |

Selection is deterministic: keywords and stage, at most 3 playbooks and 16,000 characters per call, and each role
sees only its own section. Playbooks are part of the worker's build id, so a running worker shows when it is on old
text.

## Limits (stated plainly)

- **No live comparison was run.** Whether the models now produce better work is unproven. A comparison needs a live
  run of the same requirement before and after, which uses your quota and needs your explicit authorization.
- **The first-use evaluation measures structure, not taste.** A screen can pass 7/7 and still look dull, so the
  polish judgement stays yours (screenshots are saved).
- **Two holes are not closed:**
  - Genuine output from a *different* scenario quoted for a criterion is not detected. Doing so would take a model
    judgement.
  - A build line still proves any criterion worded as "the build ..." (for example "the build output contains
    sw.js").
- **Coverage gaps:**
  - The T-0026 comparison runs only where `tasks/T-0026/workspace/firstwords/dist` exists.
  - The browser tests skip where `evals/pwa_benchmark` has no Playwright installed.

## Next

- Phase 5: [`docs/proposals/firstwords-followup.md`](proposals/firstwords-followup.md) proposes two small tasks:
  - Task A: offline recovery
  - Task B: first-screen polish, D-0006 extended
  Both are for your approval. Lessons and audio stay in later stages.
- Merging this branch into `main` needs your explicit approval.
