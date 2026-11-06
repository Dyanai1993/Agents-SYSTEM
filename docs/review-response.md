# Response to review 1

| # | Finding | Status | Where |
|---|---|---|---|
| 1 | Prompt fields optional in schema | **Done**: `depends_on`, `covers`, `tests_run` (when ok), `design_problem`, `constraints_na`, `command`, `criterion_ids` are required; non-blank strings use `pattern \S`; fail checks need severity/repro/probable_cause; design_problem needs rationale | `schemas/*.json`, `tests/test_core.py` |
| 2 | `pass` + `skipped` accepted | **Done**: `pass` needs every check `pass`. A pass check needs a real command and evidence or `exit_code: 0` | `test_report.schema.json` |
| 2 | Evidence not proven | **Done; live run 1 forced a fix**: a check is backed when its command matches a logged Bash command OR its evidence excerpt appears in a logged Bash output: the longest run that matches unchanged must be 6+ chars and 75% of the evidence; `...` cuts and path-like tokens are ignored (Git Bash rewrites /tmp paths on Windows). The first live task (T-0001) was rejected because the tester ran 3 batched commands and described more checks than that. Every pass/fail check must be backed by the backend tool log. CLI backend parses `stream-json` tool_use/tool_result. Fails closed when no tool log (`AGENTSYS_EVIDENCE=warn` to relax). The SDK backend does not build a tool log yet, so it fails closed. | `semantics.evidence_errors`, `runner.CliBackend` |
| 3 | Weak tests fool the Tester | **Partly**: prompt rule (derive expectations from criteria, check must fail for a wrong implementation); `criterion_ids` coverage enforced by code. Assertion quality itself stays a prompt rule. Mutation testing is not built. | `tester.md`, `semantics.derive_verdict` |
| 4 | fail / blocked contradiction | **Done**: one decision order, recomputed by code (`fail` > `blocked` > `pass`). Reported `fail` with all checks passing is a hard error. `design_problem` redefined with required rationale | `semantics.process_test_report` |
| 5 | `QUESTION:` / `BLOCKED:` prefixes | **Done**: `status: needs_input` + `questions` (Architect); `status: blocked` + `blocking_kind: user_input\|environment` (Developer). Environment blocks are not shown as user questions. Blocking `unknowns` stop before any agent call | `plan`, `dev_report` schemas, `loop.py` |
| 5 | Opus approval channel | **Open (Phase 3)**: approval will be a decision file written by the dashboard, never text from an agent |  |
| 6 | Lost requirements | **Done**: constraints and original criteria go to Developer and Tester; plan must cover every `AC`; replan gets failing item, completed ids and cannot drop coverage; completed unchanged items are not rebuilt; ids unique, dependencies valid and ordered, trivial <= 3 items, plan size checked against the call cap | `semantics.plan_errors`, `loop.py` |
| 6 | Final regression | **Done**: one tester run on the finished workspace against all original criteria and constraints | `loop.py` |
| 7 | Injection beyond role-change | **Prompt part done** (safety line rewritten). **Open (Phase 6)**: OS-level sandbox, no secrets in the task environment, network limits. Bash cannot be made read-only by tool lists. |  |
| 7 | Tester mutates files | **Done (detective)**: product-file changes after a tester run are reverted and the report is discarded. `qa_tests/` edits by the developer are reverted | `loop.py` |
| 8 | JSON-only reliability | **Partly**: real JSON examples in prompts, and a test that validates those examples against the schemas. `--json-schema` not adopted (Draft-07 vs 2020-12, internal retries vs no-retry rule): decide after the first live run | `tests/test_loop.py::PromptTests` |
| 9 | Commit policy | **Done**: commit per successful developer run; `qa:` commit for tester files; blocked/failed developer work is not committed and is listed in the status report; `files_changed` is taken from git, model claims are cross-checked | `loop.py` docstring |
| 10 | Usage | **Done**: per-role turn and time limits; adversarial checks risk-based (0-5); evidence is a short excerpt, full output in the tool log payload; rejected alternatives optional; budget math (`1 + 2N + 1` calls) enforced at plan time. Prompt wording trimmed. | `config.ROLE_LIMITS`, `semantics.plan_errors` |
| - | Architect `Write` tool | **Done**: removed | `config.py`, `architect.md` |

## Still unverified
Everything above runs against the fake backend and a fake `claude` binary only. The first live run must confirm: the real `stream-json` shape (tool_use / tool_result), `apiKeySource`, and that the tool-log check does not reject honest runs.

## Live run T-0010 (orchestrator, intake from plain text)
The tester wrote "all constraints verified" in `summary` but no check carried the constraint ids, so the report was rejected and the task stopped with `needs_input`. The user could not answer anything: it was a tester omission.
Fix: coverage gaps without skipped checks trigger exactly ONE completion call to the tester (with the gap list). Still incomplete -> `needs_input` naming the gap. Skipped checks (tool or environment problems) are never retried. The tester prompt now shows a constraint check in its example and says that only `checks` and `constraints_na` count.


## External review of phase 4 (verdict: do not ship)
Report: `phase4-code-review-he.md` on commit 54f7224. Every finding was checked against the code. All 12 were real; none was refused.
Tests for each fix: `tests/test_review_fixes.py` and `tests/test_phase4.py`.

| # | Finding | Result | Fix |
|---|---|---|---|
| 1 | blocker: guard reads settings of the wrong folder; credential source checked after the run | **Fixed** | Guard also reads `.claude/settings*.json` of the agent's real working directory. The CLI backend streams the output and kills the process the moment `init` shows a wrong `apiKeySource`. Agents cannot leave `.claude/` or `CLAUDE.md` in the workspace (removed, alert logged, never committed). **Not live-verified** |
| 2 | `tools: []` still gets default tools; `--allowedTools` does not limit availability | **Fixed** | `--tools <list>` (empty string = none) plus `--strict-mcp-config` on every call. `live_smoke.py` now fails if `init` shows other tools or MCP servers. **Re-run the smoke test** |
| 3 | The system dirties its own repo, then `apply()` refuses | **Fixed** | `memory/inbox`, `memory/proposals`, `lessons.md` are git-ignored. A test runs the default layout (home = repo) end to end |
| 4 | Gate accepts empty, duplicate, unpaired runs | **Fixed** | Gate needs two different, finished, full-suite runs with unique ids, tied to the base commit and to the branch head, clean tree, no budget skips. Invalid input is not a verdict: the proposal stays open. Token limit is no longer rounded |
| 5 | Unchecked git failures reported as success | **Fixed** | Every git call that matters is checked. Failed delete -> `revert_failed` and an error; failed cleanup -> `cleanup_failed`. `recover-proposal` repairs |
| 6 | Constant `_learning` task id: its 45-minute budget never resets | **Fixed** | Each review has its own task id and ledger |
| 7 | Approval not bound to content | **Fixed** | The approval request stores a hash of the proposal AND the target file content. `apply()` refuses when either changed |
| 8 | Symlink target escapes the repo | **Fixed** | Targets with a symlink anywhere in the path, or resolving outside the repo, are refused at review and at apply |
| 9 | Limits not hard: eval budget between tasks only, `0` disables, reviewer input unbounded | **Fixed** | Per-task token cap in the ledger (default 450k); eval budget is a cap inside each task; budget must be positive; reviewer sees at most 40 lessons |
| 10 | Reusing a label resumes an old run | **Fixed** | Every run folder is unique (`label-timestamp`); resuming needs `--resume DIR`. Results are saved after every task and carry run id and commit |
| 11 | Interrupt leaves a half-applied branch | **Fixed** | Journal first; `BaseException` triggers cleanup; start ref is a commit when HEAD is detached; `orch.py recover-proposal` |
| 12 | Word filter does not protect meaning; lessons carry model text | **Partly fixed** | Lessons are fixed templates only; model text is only hashed. Proposals may change only the `Rules` section of a prompt, may not remove never/do-not/only rules, and may not add weakening wording. A human still approves the exact diff. A filter cannot judge meaning: that stays a human job |
| - | Claims: "60 lines" was 62 | **Fixed** | The cap is the total line count of the file |
| - | Missing tests (empty eval, label reuse, dup ids, zero budget, interrupt, symlink, hash, checker timeout, hard-coded integration client, crash-safe lessons file) | **Added** | A hanging check now fails its task only. Eval 05 now requires real calls and rejects a hard-coded client |

Still open: proposal quality from a real model, one noisy before/after run as a gate (the review's repeat-run suggestion is not built), the Windows suite, and live verification of the new CLI flags and streaming.

## Windows run of the review fixes (live, 2026-11-06)
- `live_smoke.py`: all 8 checks passed. Tools = ['Bash'], no MCP servers, subscription credential source, input tokens 19.6k -> 5.9k.
- 180 tests ran on Windows: 3 problems, all fixed:
  1. Symlink tests need a privilege Windows normally does not grant: they now skip with a clear reason where symlinks cannot be created.
  2. **Real bug**: on timeout only the `.cmd` shim was killed; the real child kept the pipe open and the call returned after 30 s instead of 1 s. The CLI backend now kills the whole process tree (`taskkill /T` on Windows, a process group on POSIX), reaps the process and closes every handle. A test with a child process that outlives its parent guards this on every platform.
  3. `ResourceWarning` (unclosed file): the stderr file leaked on the error paths. Closed in `finally`; the tests now run clean with `-W error::ResourceWarning`.

## First real merge request (T-0020, 2026-11-06)
Reading the real approval request showed what no test had: the delivery contained `__pycache__/*.pyc` and tester scratch files
(`qa_tests/err.txt`, `out.txt`, `o.txt`, ...), and the title was cut inside a word ("chara").
Fixed: every workspace starts with a `.gitignore` for build junk (a seed may bring its own); the tester prompt says that real test scripts go
in `qa_tests/` and all scratch output goes OUTSIDE the workspace; titles are cut at a word boundary. Tests: `CleanDeliveryTests`.

## Live run T-0010: the timeout clock and a missing retry command (found by the local tester session)
1. `timeout_s` (45 minutes) was measured from the first start of the task, so every hour a task waited for a human or sat paused counted against it.
   The rerun of T-0010 died with `task timeout 2700s reached` before it could do any work. Fixed: the clock restarts with every run
   (`Ledger.begin_run()`, called by `loop.run_task`); active agent time is recorded separately (`active_s`). Pause/stop never counted against the clock after this.
2. A task in `needs_input`, `needs_approval`, `blocked` or `paused` could only be restarted by calling `orchestrator.process()` from code.
   Added `orch.py retry TASK [--answer "..."]`: re-queues the task; it resumes from its checkpoint. An answer is stored as a constraint
   ("Clarification from the user: ...") and blocking unknowns are marked answered. Done, rejected, stopped and waiting-for-approval tasks are refused.
3. `timeline.py` counted tokens twice: already fixed earlier (tokens are logged once per call).
Tests: `tests/test_fix_timeout_retry.py` (includes the story end to end: stop for a question, wait 10 hours, answer, resume).
Product note from the same run (not a system bug): a CSV row with fewer or more fields than the header gives `null` values or a `null` key.
The requirement did not say what to do, and a real intake should ask. Decide it in the next requirement.
