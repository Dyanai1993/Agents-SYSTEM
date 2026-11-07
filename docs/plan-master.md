# Plan: the master product prompt (2026-11-07)

Source: the owner's "Agents-SYSTEM — Master product and engineering prompt". No review report existed when this plan
was made; every point was checked against the code first. Each phase ends with tests, a browser check with the
FakeBackend (zero quota), a commit and a push. Live subscription checks only with the owner's explicit go.

## Gaps found in the code before starting
- Stop is terminal by accident: `stopped` is not in `orchestrator.RETRYABLE` although `retry()` says it requeues a
  stopped task; the dashboard offers Resume after Stop and it does nothing.
- A stop/pause on a task that is not running only writes control.json; the status changes at the next run.
- `deliver()` exports `<base>..HEAD`, not the approved commit (safe only because the approval identity is re-checked
  right before; nothing exports the approved sha itself).
- No worker liveness, no build/version check, no single-instance guard (two `orch.py assistant` raced live).
- The dashboard cannot start, continue or answer a task; it prints CLI commands.

## Phase A - one entry point, a trustworthy "alive" signal
- [x] `agentsys/worker.py`: one worker = chat turns + task runs the user REQUESTED (never picks tasks on its own);
      single-instance lock; heartbeat thread (state/workers/<instance>.json: kind, pid, build, job, step, beat).
- [x] build id of the code on disk; dashboard flags a worker or server running old code.
- [x] run requests (requests/run/<task>.json): Start / Continue from the dashboard; the worker validates
      (task exists, runnable, window) and records why a request waits or was refused.
- [x] Answer a needs_input question in the task view (retry with the answer + a Continue request).
- [x] Controls: applied at once when no run holds the task; otherwise shown as pending. Stop is reversible
      (Resume requeues from the checkpoint).
- [x] `python scripts/up.py`: dashboard + worker in one terminal. `orch.py worker`; `orch.py assistant` = alias.
- [x] run-next CLI publishes a heartbeat too.

## Phase B - review and delivery
- [x] One review screen: criteria, final test results with evidence, security findings, changed files, the diff.
- [x] Merge approval bound to requirement revision + commit + security report (exists); deliver exactly the
      approved commit; delivery receipt (patch names, folder, branch).

## Phase C - robustness and UI
Done early, from live use (2026-11-07):
- [x] A button never fails silently when the server does not answer (post() returns every failure as an error).
- [x] One style for buttons (primary / normal / danger, hover, pressed, focus) and dropdowns (own arrow, dark popups).
- [x] Visual state: a pipeline (Plan -> items -> final regression -> security -> review -> delivered, the running
      step pulses) and a live terminal of what the agents do (tasks/<id>/live.jsonl, streamed by the runner).
- [x] Narrow a stuck task's requirement in place; a note with Retry; "narrow it" when the plan cannot cover the criteria.
- [x] The tester gets one redo when its report breaks the evidence/format rules (instead of blocking the task).
Still open:
- [ ] Non-overlapping refresh; discard obsolete fetches when the selection changes; actions bound to the
      rendered task/draft; lost-response handling (reconcile before offering to repeat).
- [ ] Actionable errors first, technical details folded.
- [ ] Local, deduplicated browser notifications (needs you / failed / done).
- [ ] Narrow screens (navigation), keyboard, visible focus, labels, contrast, Hebrew direction.

## Phase D - docs and verification
- [ ] README around the real journey with one launch path, statuses and recovery.
- [ ] The 8 journeys of the master prompt, through the real entry points with the FakeBackend.
- [ ] Review handoff per docs/process.md.

## Benchmark plan: raise the quality bar using T-0026 (owner's brief, 2026-11-07)
T-0026 (the FirstWords shell) is the benchmark; the delivered project stays read-only; regressions use isolated
fixtures; FakeBackend proves wiring, not model quality; any live comparison needs explicit quota authorization.
- [x] Phase 1 - handoffs and playbooks: Assistant/intake, Architect, Developer, Tester, Security prompts; UI design
      and testing procedures; a conditional PWA-reliability playbook; stage-aware selection (plan / build / test /
      FINAL); playbooks in the review document and the build id. Verified on real role payloads incl. FINAL.
- [x] Phase 2 - tester evidence: an unrelated command, a grep of a test name or an aggregate count cannot prove a
      separate criterion; expected-failure and batched checks with distinct output still pass.
- [x] Phase 3 - PWA benchmark fixtures + browser checks: broken B while A is verified; corrupt asset while the
      manifest says A; ready marker with a missing cache; real keyboard download / failure / retry / theme + focus.
- [ ] Phase 4 - a quality check that separates a bare shell from one meeting the first-use criteria; false-pass cases.
- [ ] Phase 5 - a short FirstWords follow-up proposal (offline recovery, shell polish), for separate approval.
