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

## Requirement gate (free-text CLI requests)
`orch.py submit ID --request "..."` -> intake (Haiku) writes a DRAFT to `tasks/<id>/requirements.json` -> the task waits
at a `requirements` approval gate (dashboard or `orch.py approve`). Nothing is built from an unapproved draft; the approved
draft becomes `entry["requirements"]` and the build starts on the next `run-next`. Before approval,
`orch.py retry ID --answer "..."` adds the answer to the DRAFT as a clarification constraint (no model call) - the changed
draft needs approval again (the approval's `diff_ref` is the draft's hash). Approval with an open BLOCKING question is
refused from the dashboard. Reject cancels the task. A structured `submit --goal/--criteria` needs no requirement approval.
The Approvals list shows only the latest request per task and gate (an older one was superseded).

## Requirements Assistant
```
 dashboard "New task"  --writes-->  drafts/D-####/session.json  (draft, revision, conversation, turn state)
   message = one turn request (status pending)          hand edit = revision+1 (refused if stale)
 orch.py assistant / run-next  --> assistant.process_next_turn() --> runner.run_agent("assistant", Sonnet, no tools)
   result applied only if the draft is still at the revision the turn started from; else kept, marked not applied
   failure (invalid output, usage limit, backend error) -> turn "failed", draft untouched, explicit Retry only
 "Approve and submit" (human) --> assistant.submit(revision): readiness (requirements schema + no blocking question)
   -> journal (task id) -> orchestrator.submit(requirements=<exact draft>, origin={draft_session, revision})
   -> idempotent: the same revision always returns the same task; an interrupted submission is finished by the retry
```
- Model calls happen only in the local execution layer, never in an HTTP handler. One agent call at a time across
  processes: `queue/_exec.lock`, held by `run-next` and by each assistant turn (a turn waits while a task step runs).
- The assistant (`.claude/agents/assistant.md`, `schemas/assistant_turn.schema.json`) may leave the draft incomplete;
  readiness and submission are decided by code, and only a human action submits.
- Session limits reuse the task ledger (`tasks/D-####/budget.json`, `logs/D-####.jsonl`); the overview lists only
  folders with a `state.json`, so sessions never show as tasks.
- After submission the draft is read-only (no post-submission editing in this version); the task's requirement card
  links back to the conversation. Execution questions use the existing `needs_input` / `retry --answer` flow.

## The worker and its heartbeat (master prompt, phase A)
- `agentsys/worker.py` is the only long-running process that calls a model. It answers Requirements Assistant turns
  first, then runs ONE task the user requested (`requests/run/<task>.json`, written by Start / Continue / Retry /
  Answer in the dashboard) as far as it goes without the human. It never picks a task on its own.
- One worker at a time: an OS lock on `state/worker.lock`. One model call at a time across the worker and a terminal
  `run-next`: `queue/_exec.lock` (unchanged).
- Heartbeat: `state/workers/<instance>.json`, rewritten every 5 s by a thread (also during a long agent call):
  kind, pid, the build id of the code it started with, and the current job (draft or task, since when). Stale after
  20 s. The dashboard shows offline / idle / busy / crashed while doing X / old code / two workers, and a task is
  "running" only while a live heartbeat names it - no more guessing "stuck?" after 30 quiet minutes.
- `scheduler.run_blocker()` says why a task cannot run: a reason the human must act on refuses the request at once;
  a reason that clears by itself (the usage window) keeps it waiting, with the reason shown.
- `orchestrator.apply_control()`: pause / stop / resume take effect at once when no run holds the task (otherwise
  before the next agent call, as before). Stop is reversible: resume requeues from the checkpoint.
- `scripts/up.py` starts the dashboard and the worker together, after the billing guard.

## Review and delivery (master prompt, phase B)
- `dashboard_data.review_view()` / GET `/api/review`: the acceptance criteria, each with the final-regression checks
  that cover it (command, result, evidence), the constraints, the security report, the changed files and the diff.
  All of it is cut from the commit the merge approval is bound to (its `diff_ref`), not from the workspace's HEAD, so
  what the review shows is exactly what an approval delivers. `stale` = the identity changed after the request (a
  new approval will be requested). Diffs are cached per commit (a commit never changes).
- `orchestrator.deliver(commit=...)` exports `<base>..<approved commit>`; an unknown commit is refused. Patches are
  written to a fresh folder first and then replace the old set (stale patches used to be listed and applied). The
  receipt (queue entry `delivery`) holds the patch names, folder, branch, commit, approval id and time.
- `loop._git` (and every other subprocess call) decodes output as UTF-8 with `core.quotepath=false`: the Windows code
  page raised UnicodeDecodeError on a Hebrew diff or file name.

## Project repositories (after the master prompt)
- Every new task gets a new local git repository (`orchestrator.create_project_repo`, in `config.projects_dir()`:
  `AGENTSYS_PROJECTS`, else `<AGENTSYS_HOME>/projects` when a separate data folder is used (tests, demos), else
  `~/Projects`), unless an existing one is chosen (`project_check` validates: absolute path, a git repo, a commit at the
  branch, a branch name that is not an option). Created on submit; the path is journaled so a retried submit reuses it.
- At the first run the workspace starts as a copy of that commit (`git archive`, tracked files only, safe extraction);
  `project_base` records repo, ref and sha. The agents never touch the repository itself.
- Delivery creates `agentsys/<task>` in the repository from `project_base.sha`, so the patches (only the task's own
  commits) apply even if the repository moved on. Nothing is merged or pushed.

## Output quality (2026-11-07, the owner's decision)
Seen live: T-0026 passed every check, yet the app was bare (App.tsx 39 lines, app.css 25 lines, browser defaults),
because the prompts rewarded the minimum that passes ("choose the simplest design", "keep changes small") and the
requirements had no quality criteria. Changed, with no extra calls per task:
- Architect: plan a complete, production-quality result within scope; for anything a person sees, a coherent visual
  design (tokens, layout, responsive, interaction states) with checkable UI criteria; a few substantial items
  instead of many tiny ones.
- Developer: finish each item to production quality (errors, empty/edge cases, no stubs); for UI, polished design
  using the project's tokens. Passing the criteria is the floor, not the goal.
- Requirements Assistant: Sonnet instead of Haiku; proposes checkable quality criteria (visual, robustness,
  accessibility) for anything a person will use.
Not yet measured: no eval run (it costs quota). The next live task is the first evidence. Further levers, not taken
yet: a visual review gate with screenshots, Opus for Architect/Developer, a larger budget per task.
