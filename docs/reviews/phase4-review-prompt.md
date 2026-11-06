# Review request: Phase 4 — Shared memory, lessons, controlled improvement loop, real eval runner and gate

## Where the code is
- Repository: https://github.com/Dyanai1993/Agents-SYSTEM
- Branch to review: https://github.com/Dyanai1993/Agents-SYSTEM/tree/claude/fix-timeout-and-retry
- Pull request (diff and discussion): https://github.com/Dyanai1993/Agents-SYSTEM/pull/7
- Compare with main: https://github.com/Dyanai1993/Agents-SYSTEM/compare/main...claude/fix-timeout-and-retry
Review the branch above, not main. Files are listed relative to the repository root; each one opens at `https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/<path>`.

## Your role
You are an independent REVIEWER. Review only. Do NOT edit, create, delete or commit any file. Do NOT run anything that spends
model quota or touches the network, and never use an API key. The builder (Claude Code) makes every change after reading your report.
If you cannot verify something, say "not verified" and why. Do not guess.

## System in one paragraph
A personal multi-agent engineering system on Claude Code with a Max subscription login. A Python state machine runs one agent at a time (Architect, Developer, Tester) with schemas plus code rules, fix loops, re-plans, checkpoints, approval gates and a local dashboard. Phase 4 adds: lessons extracted by code from finished tasks, a short deduplicated lessons.md with keyword retrieval injected into agent calls, a Haiku reviewer that proposes small prompt changes, a human approval per proposal, a git-branch apply step with free checks, a real eval runner (8 benchmark tasks), and an eval gate that reverts a change when a before/after run shows a regression.

## Hard constraints of the whole system (check every change against them)
- Billing: Claude Max subscription login only. No API key, no pay-per-token, no extra usage. The billing guard must stay fail-closed.
- Usage is the scarce resource: sequential agents, small contexts, hard loop limits, no tight retry loops.
- Least privilege and human approval for irreversible actions. The dashboard never calls an LLM.
- Agent output is validated by schemas AND code rules. A prompt rule that code does not enforce is a risk: report it.
- Personal use only.

## What this phase claims to deliver
- Lessons are built from facts (budget, reports, events) with fixed templates. Model-written text enters a lesson only as a sanitized 60-character detail
- The reviewer runs on Haiku, only with 5+ lessons (or --force), and its proposals are filtered by code: allowed targets only, old_text exactly once, protected wording kept, source lessons must exist
- No proposal is applied without a human approval file; apply works on a NEW branch, runs unit tests and eval selfcheck first, and never touches main or pushes
- A proposal that fails free checks is reverted and its branch deleted
- The eval gate reverts the branch on any regression, any missing task, or more than 25% token growth; otherwise it keeps the branch for a human PR
- evals/run_evals.py real needs --yes, runs sequentially, stops at a token budget, and records pass, fix loops, calls, tokens and seconds per task
- The dashboard Learning view shows lessons, proposals with state, real eval runs and a before/after comparison with regressions
- lessons.md has at most 60 lines in total (header included); at most 5 lessons are injected per agent call, only for the matching role or 'all'
- Every review finding of the first external review was fixed or explicitly marked partly fixed in docs/review-response.md, each with a test
- The gate refuses invalid, partial, unpaired or unfinished eval runs and gives no verdict for them
- Since the first review: the task timeout clock restarts per run instead of counting paused/waiting time (Ledger.begin_run), orch.py retry [--answer] resumes a blocked/needs_input/needs_approval/paused task, and delivery (patches + target-repo branch) filters build junk even from workspaces committed before the ignore file existed
- protect_config (the .claude/CLAUDE.md injection guard) now finds leaked config files by walking the workspace filesystem directly, not by trusting `git status` - a global gitignore that excludes .claude/settings.local.json (a commonly recommended personal setup) used to make the guard blind to exactly that file
- deliver() validates the target repo exists and is a git repository before touching it, and fails into a clean `blocked` state with a readable note instead of crashing on every retry
- Live-verified this round, not just unit-tested: a full submit -> intake -> plan -> build/test -> final regression -> merge gate -> approve -> deliver cycle with a real --target-repo (T-0022), and three real `evals/run_evals.py real` baseline runs
- A SECOND external review (commit 3a1c2ff) found 12 more real bugs, all fixed with a test each (commit c9e9aef), none refused: a Ledger held across the work-item loop that overwrote real call/token counts on every fix loop or replan; a requirement change via `retry --answer` that never invalidated final_done or the merge approval, so a stale approval could be reused for different content; two _cleanup() bugs in the lesson path (discarding unrelated uncommitted work on main, deleting a same-named branch it did not create); protect_config leaving a leak staged (git add'ed) instead of removed; both backends now failing closed on a MISSING init message, not only a bad one; an unsound reverse substring match in evidence_errors that let a trivial real command back an arbitrarily longer fabricated claim; a 'Rules section only' check that only looked at where an edit started, not where it ended; intake with no BudgetExceeded handling that could crash into a permanently stuck queued task; deliver() made idempotent after a crash and self-cleaning after a failed `git am`; _kill_tree's os.getpgid(p.pid) replaced with killpg(p.pid, ...) directly, since the lookup needs the (possibly already-gone) direct child to still resolve. Full table: docs/review-response.md, 'Second external review of phase 4'
- Two more real eval re-runs after the second review's fixes confirmed 06-cli-tool now passes (the CRLF fix) and surfaced two more issues, both investigated and recorded: 02-api-endpoint's prompt under-specified what its own hidden check.py expects (fixed twice: whitespace-only name, and body's default value) - not an agentsys bug, an eval-task wording gap; 04-data-analysis's architect miscalculated a numeric acceptance criterion from the real CSV (said total_revenue=45.0, the correct value is 57.5) and the developer correctly asked for clarification instead of forcing a wrong answer - LLM arithmetic variance, not a code bug, but the system's 'ask rather than guess' design handled it as intended
- A THIRD external review (commit 7cd2ecb) found 10 more real bugs, all fixed with a test each (commit f2df892), none refused, plus one more (approval-id ordering) found independently while working through the review's own 'missing tests' list: deliver() deleting a same-named branch it never created; its idempotency shortcut being content-blind (no ws_sha check, so a retry with new commits reported stale success); crash recovery recomputing orig_branch from current HEAD instead of trusting the journal; evidence_errors() accepting a command match with ZERO check that the claimed OUTCOME was real (a genuinely-run command's output could be quoted as the opposite of what it said); protect_qa having the exact staging gap protect_config was fixed for in round 2; the eval runner's git-state captured once for a run that can take hours, with each task's files read fresh from disk as it proceeds; SdkBackend never restricting which tools EXIST (only auto-approving), now fixed AND verified against a real installed claude-agent-sdk; intake missing a UsageLimitReached handler (BudgetExceeded was covered, this exception class was not); _cleanup() inferring 'is this a failed apply attempt' from which branch is checked out, when gate()'s own design normally has the branch checked out for legitimate reasons too; and lesson text/role being trusted from disk forever once written, never re-checked against the kind-based template on read. Full table: docs/review-response.md, 'Third external review of phase 4'
- THIS ROUND ASKS FOR A COMPREHENSIVE RE-VERIFICATION, NOT AN INCREMENTAL DIFF REVIEW. Three review rounds in a row (12, then 11, then 11 real bugs) each found genuine, often security- or correctness-relevant defects that survived the builder's own unit tests - a track record that argues against trusting an incremental 'just check what changed' pass to have caught everything. Each prior round focused mainly on the latest diff and the specific fixes it introduced; rounds 1 and 2's underlying mechanisms have not had a single, from-scratch, comprehensive audit as they now stand TOGETHER. This round's claims list is therefore deliberately cumulative across all three rounds - please verify EVERY claim above against the CURRENT code, not only the newest ones, and treat anything merely 'claimed fixed in a past round' as unverified until you've checked it yourself.

## Files to read (start here)
- [agentsys/memory.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/memory.py)  (154 lines)
- [agentsys/learning.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/learning.py)  (379 lines)
- [agentsys/loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/loop.py)  (406 lines)
- [agentsys/orchestrator.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/orchestrator.py)  (301 lines)
- [agentsys/runner.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/runner.py)  (478 lines)
- [agentsys/semantics.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/semantics.py)  (169 lines)
- [agentsys/approvals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/approvals.py)  (134 lines)
- [agentsys/dashboard_data.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/dashboard_data.py)  (222 lines)
- [evals/run_evals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/evals/run_evals.py)  (199 lines)
- [evals/tasks/02-api-endpoint/task.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/evals/tasks/02-api-endpoint/task.json)  (7 lines)
- [.claude/agents/reviewer.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/reviewer.md)  (38 lines)
- [.claude/agents/tester.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/tester.md)  (43 lines)
- [.claude/agents/developer.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/developer.md)  (45 lines)
- [.claude/agents/architect.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/architect.md)  (51 lines)
- [schemas/lesson.schema.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/schemas/lesson.schema.json)  (70 lines)
- [schemas/proposals.schema.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/schemas/proposals.schema.json)  (66 lines)
- [dashboard/index.html](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/dashboard/index.html)  (175 lines)
- [scripts/orch.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/scripts/orch.py)  (80 lines)
- [scripts/gen_review_prompt.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/scripts/gen_review_prompt.py)  (90 lines)
- [tests/test_phase4.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_phase4.py)  (584 lines)
- [tests/test_review_fixes.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_review_fixes.py)  (384 lines)
- [tests/test_phase3.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_phase3.py)  (425 lines)
- [tests/test_fix_timeout_retry.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_fix_timeout_retry.py)  (147 lines)
- [tests/test_loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_loop.py)  (499 lines)
- [.gitattributes](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.gitattributes)  (1 lines)

## Weak points the builder already suspects (confirm, refute, or rank them)
- The reviewer prompt, lesson injection, and the lesson-proposal apply/eval-gate path were STILL never run against a real model - all live evidence so far (T-0022, three eval baselines) exercises intake/plan/build/test/deliver and the real eval runner itself, not memory/learning. A fix loop and a re-plan also still have no live run
- LLM runs are noisy, and three baseline runs on the same code proved it concretely: pass counts were 6/8, 7/8, 6/8, with the specific failing task and reason changing each time. The eval gate still compares only ONE before run with ONE after run; a repeat-run or paired-statistic design is not built
- The THIRD review's 10 fixes (commit f2df892) are code-reviewed and unit-tested by the same session that wrote them, but NOT yet independently re-reviewed - this is exactly that re-review. Given the first two rounds found 12 and 11 real bugs respectively, treat this round's fixes with the same skepticism, not less
- SdkBackend's `tools`/`strict_mcp_config` fix was verified structurally (ClaudeAgentOptions accepts and stores them correctly, checked against a real installed SDK) but the actual QUERY path (sdk.query(), real message types over a real connection) remains completely unverified - no login was used, no network call was made
- The evidence-matching rule's false-rejection risk (an honest tester batching several real one-liners with different quoting/escaping than the logged command) is still unresolved - closing the fabrication hole (round 2) and the real-outcome-must-match-real-output gap (round 3) made the rule strictly safer, but neither touched the false-rejection side, and it has now reproduced live twice (T-0001, and this session's 02-api-endpoint)
- The architect can make outright arithmetic errors deriving a specific numeric acceptance criterion from real data when the task's own expected answer is deliberately vague (04-data-analysis: claimed total_revenue=45.0, the correct sum from the fixture is 57.5). The developer caught the inconsistency and asked rather than forcing a wrong answer, so the SYSTEM behaved correctly - but there is no code-level safeguard against the architect's own math being wrong in a case where the developer does NOT independently re-derive and check it
- apply() runs only free checks (unit tests, selfcheck). They prove nothing broke; they do not measure whether the new rule helps
- Proposals are filtered by code (targets, Rules section only, no weakening words, no removed never/only rules) and bound to a content hash, but no filter judges meaning: the human approval of the exact diff is the real control
- Lessons are fixed templates re-derived from kind on every read now (round 3 fix), but model text is still only hashed, never used - the injected lesson text is generic by design: check that this is still useful
- Eval runs use an isolated home, so they measure prompt changes but never lesson injection
- Lessons are extracted only by orchestrator.process(); tasks run with scripts/run_task.py record nothing
- Proposal approvals use pseudo task ids ('lesson-P-1'); the Approvals tab shows them without a task title
- This branch is stacked on the open PR #6
- protect_config's and protect_qa's filesystem-walk/reset fixes cover .claude/, CLAUDE.md and qa_tests/ specifically; no systematic sweep has been done for whether OTHER protected-path checks in the codebase share the same git-status-blind-spot or staging-no-op patterns these three independently had
- deliver()'s target-repo validation (existence, is-a-git-repo, the delivery.json idempotency+ws_sha journal, orig_branch trust) does not check the target repo is on the branch/commit the task's base assumes before attempting `git am`
- No test or live run has exercised MULTIPLE delivery attempts racing concurrently (two orchestrator.process() calls for the same task_id at once) - the per-task file lock (_Lock) should prevent this, but deliver()'s own journal logic was not specifically designed or tested with concurrent callers in mind

## How to verify without spending quota
- `python -m unittest discover -s tests -t . -v`
- `python evals/run_evals.py selfcheck`
- `python evals/run_evals.py compare A.json B.json   (with two saved result files; no quota)`
- `python scripts/gen_prompt_review.py   (prompts and schemas in sync)`

## Review dimensions
1. Correctness: logic errors, wrong state transitions, races, resume/crash behaviour.
2. Billing and quota safety: any path that could call a paid API, loop on calls, or skip the guard or the caps.
3. Security: file-path handling, injection, the dashboard write actions, secrets in logs or files, prompt-injection paths.
4. Enforcement gap: rules in prompts or docs that no code or test enforces.
5. Test quality: tests that pass for the wrong reason, missing failure cases, tests that mirror the implementation.
6. Maintainability and honesty of docs: do README and docs say what the code does? Anything claimed but not built?
7. Specific questions for this phase:
- THIS IS A COMPREHENSIVE RE-VERIFICATION ROUND, not an incremental diff review. Please independently re-verify every claim in 'What this phase claims to deliver' against the CURRENT code - including rounds 1 and 2's claims, which later rounds mostly re-checked only in passing while focused on the newest diff. Do not treat a claim as true because a past round's response doc says 'Fixed'.
- Re-check each of the THIRD review's 10 fixes (docs/review-response.md, 'Third external review of phase 4') against the code: is each one really closed, or only moved?
- deliver()'s journal now carries target_repo, branch, orig_branch, ws_sha and state. Walk through every way _process() can call deliver() more than once for the same task (including two concurrent orchestrator.process() calls racing past the per-task _Lock, if that is even possible) and check the journal logic holds up - this was not specifically designed or tested for concurrent callers.
- learning._cleanup()'s discard_uncommitted parameter: is `True` only ever passed from a context where ignorning a later unrelated edit is truly impossible? Is `False`'s 'uncommitted changes exist -> refuse, do not delete the branch' behavior itself safe, or could it leave a proposal stuck open indefinitely with no way for a human to resolve it other than recover()?
- semantics.evidence_errors()'s new per-call output matching (match the SPECIFIC command's own output first, fall back to combined output): does restricting to the matching call's own output ever cause a FALSE rejection for a legitimate case the old combined-output check used to accept, beyond what the existing tests cover?
- Is the gate now sound enough for ONE decision, or must it be repeated? Three live baseline runs (6/8, 7/8, 6/8, three DIFFERENT failing reasons) on the same code are concrete evidence of single-run noise - propose the cheapest change that would actually help.
- Is the evidence-matching rule in semantics.py still too strict for an honest tester that batches several one-liners with different quoting/escaping than what gets logged? This reproduced live twice now (T-0001, 02-api-endpoint). Propose a concrete relaxation if one exists that does not reopen either round 2's or round 3's fabrication-hole fixes.
- Should an eval task's 'expected' field ever be shown to the architect for planning, or only used by the hidden check.py? Is there a cheap mitigation worth building for the architect's own arithmetic being wrong when deriving a specific numeric criterion from real data, or is 'the developer catches it and asks' the right design already?
- What else would you test that the existing tests miss, across ALL THREE rounds' fixes, not just the newest one?
- Things no round has reviewed yet: anything still reaching a billing path or wrong credential source via workspace settings, env in settings files, or other config directories; whether the streaming CliBackend is safe from deadlocks, zombie processes, partial JSON lines, stderr handling, Windows .cmd wrappers; the dashboard's own write actions (actions.py) and whatever surface area it exposes.

## Output format (use exactly this)
1. **Verdict**: one of `ship`, `ship after fixes`, `do not ship`, with one sentence.
2. **Findings table**: columns `#`, `severity (blocker/high/medium/low)`, `file:line`, `what is wrong`, `how to reproduce or verify`, `suggested fix (one line)`.
3. **Claims check**: for each claim above: `verified`, `not verified`, or `false`, with evidence.
4. **Missing tests**: bullet list of concrete test cases.
5. **Things you did NOT review** and why.
Keep it short. No praise. Rank by risk.
