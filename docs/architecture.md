# Architecture (Phase 1)

```
 task text
    |
    v
 orchestrator (script; state machine arrives in Phase 3)
    |  run_agent(role, input, model, limits)
    v
 +--------------------------- runner.py ---------------------------+
 | 1 billing guard (env, settings)      4 init message: apiKeySource |
 | 2 budget ledger check (calls, time)  5 ledger + window update     |
 | 3 backend: sdk | cli | fake           6 schema check, event log    |
 +-------------------------------------------------------------------+
    |                                   |
    v                                   v
 tasks/<id>/ (state, budget, payloads)  logs/<id>.jsonl
                      \                /
                       v              v
                   dashboard (reads files only, 127.0.0.1)
```

Failure behaviour: usage limit -> `checkpoint.json` + `limit_hit` event + stop (no retry).
Bad agent output -> `error` event, result `ok=False`, orchestrator decides. Wrong credential source -> alert + stop.

## Not verified yet (needs your login)
- `SdkBackend` and the real `claude -p` stream-json format (`apiKeySource` in the init message). The CLI backend is tested
  against a fake `claude` script only. First live check happens at the start of Phase 2, with 1 Haiku call.
- Usage window sizes in `config.py` are guesses for Max 5x. They are calibrated from real limit hits.

## Phase 3 additions
```
 submit -> queue/<task>.json
 scheduler.run_next()  (sequential, one task per call, priority order, window check for NEW tasks)
   -> orchestrator.process(task)   idempotent, resumes from files
        intake (Haiku)  ->  loop.run_task  ->  MERGE GATE  ->  deliver (patches / new branch)
 human side (dashboard or CLI):   approvals/decided/<id>.json     tasks/<id>/control.json (pause|stop|resume)
```
- Gates are files. Gate types: merge, deploy, delete, send, spend, credentials, opus, other. Only `merge` and `opus` are wired in Phase 3.
  The others are enforced today by not granting the tools; their hooks come in Phase 6.
- Opus: `run_agent(model="opus")` raises until a human approves an `opus` request for that task.
- Dashboard writes only two things: approval decisions and control files. POSTs need `X-Agentsys: 1`, JSON, and a localhost Host/Origin.
- Security, integrate and deploy states were logged as `skipped` until Phase 5 decided their scope (see below).
- A usage-limit pause stores `resume_after = now + 5h` (assumption: no official reset time is available).

## Phase 4 additions
```
 task ends -> memory.extract_lessons (facts -> templated lessons, no LLM) -> memory/inbox/*.json -> memory/lessons.md (<= 60 lines)
 every agent call <- memory.retrieve(role, goal)  (<= 5 plain-text hints, labelled as data in the prompts)

 orch.py review-lessons -> reviewer (Haiku) -> proposals (code filters targets, exact old_text, protected wording)
   -> approval request (gate "lesson") -> human decision file
   -> learning.apply: NEW branch + exact edit + unit tests + eval selfcheck  (fail => revert, delete branch)
   -> evals real (before / after)  -> learning.gate: regression | missing task | +25% tokens => delete branch, else keep for a human PR
```
Known limit: one before/after run is noisy. See `docs/reviews/phase4-review-prompt.md`.

## Phase 5 additions
```
 ... final regression (tester) passes -> security (sonnet, Read/Bash/Grep/Glob, no Write/Edit)
      reviews the whole task diff for concrete vulnerabilities -> security_report.json
      verdict derived from finding severities (code, not the model): any high/critical -> fail
   fail  -> task blocked, same as a failed final regression (a human decides, nothing reaches merge review)
   blocked (the reviewer needs information only a human has) -> needs_input
   pass (zero findings, or only low/medium) -> proceeds to the merge gate; findings are shown in the
      approval context the human reads before approving (agentsys/orchestrator._merge_context)
```
Specialists originally planned for this phase, decided down to one real role:
- **security**: built (above) - the one specialist whose job (finding vulnerabilities) is genuinely different from writing or testing a feature.
- **integrate**: retired as a separate role. Code that calls an external API is just a normal developer work item; the real risk is network/credential ACCESS, which belongs to Phase 6's `credentials`/`spend` approval gates, not a new agent.
- **deploy**: retired as a separate role. `orchestrator.deliver()` already does the entire "get this into a real repo" job (branch/patch, idempotency journal). There is no other deployment target in this project to build a specialist for.
`schemas/task_state.schema.json`'s `state` enum and `approval_request.schema.json`'s gate-type enum were updated to match: `integrate`/`deploy` removed as task states (nothing ever set them); `deploy` stays as a gate type (Phase 6, same category as `delete`/`send`/`spend`/`credentials` - a human-approval gate is still a reasonable future need even with no specialist agent behind it).

## Dashboard monitoring (Phase 5)
Still read-only (files only, no LLM, no lock probing). `python dashboard/server.py 8765`.
- Overview: "Needs you" list and a column with ONE reason per task - waiting for approval, needs input (with the question),
  blocked (with the queue note, e.g. which cap was hit), paused (until when), a workspace an agent changed and could not
  revert, or **stuck**: an active state with no event for `STUCK_MIN` minutes (the longest role time limit + 10). Last activity per task.
- Task monitor: the plan's work items with their dependencies and status (done / in progress: build|test|fix N / stopped at ... /
  waiting for W2 / pending; a re-planned item is not "done"), then the final regression and security steps; the security
  report with its findings; cost per agent and per model in tokens (summed from the task's own log - every call's tokens are
  logged exactly once), calls per agent, agent time, share of the token cap.
- Writes stay as before: approval decisions and pause/resume/stop control files.
