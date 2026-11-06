# Review request: Phase 5 — Round 2 (scoped): the fixes for the 9 findings of the first Phase 5 review

## Where the code is
- Repository: https://github.com/Dyanai1993/Agents-SYSTEM
- Branch to review: https://github.com/Dyanai1993/Agents-SYSTEM/tree/claude/fix-timeout-and-retry
- THE CHANGE TO REVIEW: https://github.com/Dyanai1993/Agents-SYSTEM/compare/cf8cf811...claude/fix-timeout-and-retry
- Compare with main: https://github.com/Dyanai1993/Agents-SYSTEM/compare/main...claude/fix-timeout-and-retry
Review the branch above, not main. Files are listed relative to the repository root; each one opens at `https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/<path>`.

## Your role
You are an independent REVIEWER. Review only. Do NOT edit, create, delete or commit any file. Do NOT run anything that spends
model quota or touches the network, and never use an API key. The builder (Claude Code) makes every change after reading your report.
If you cannot verify something, say "not verified" and why. Do not guess.

## System in one paragraph
A personal multi-agent engineering system on Claude Code with a Max subscription login. A Python state machine runs one agent at a time (Architect, Developer, Tester, and since Phase 5 a read-only Security reviewer) with schemas plus code rules, fix loops, checkpoints, approval gates and a local dashboard. The first external review of Phase 5 (at commit cf8cf811) returned 'do not ship' with 9 findings; all were accepted and fixed. THIS ROUND IS SCOPED to the change from cf8cf811 to the branch tip: verify that each of the 9 fixes really closes its finding, and look for anything the fixes broke or newly opened. The previous review's full findings and the builder's answers are in docs/review-response.md, section 'External review of Phase 5'. Do not re-review Phase 4 or unchanged Phase 5 code unless a fix points you there.

## Hard constraints of the whole system (check every change against them)
- Billing: Claude Max subscription login only. No API key, no pay-per-token, no extra usage. The billing guard must stay fail-closed.
- Usage is the scarce resource: sequential agents, small contexts, hard loop limits, no tight retry loops.
- Least privilege and human approval for irreversible actions. The dashboard never calls an LLM.
- Agent output is validated by schemas AND code rules. A prompt rule that code does not enforce is a risk: report it.
- Personal use only.

## What this phase claims to deliver
- Finding 1 (read-only guard bypass): loop.guarded_call() records HEAD before a tester or security call and checks the workspace AFTER it, also when the call raised (invalid report, timeout, usage limit, KeyboardInterrupt). It detects working-tree changes, STAGED changes, and commits the role made itself. The tester keeps its legitimate qa_tests/ writes; the security reviewer may write nothing, qa_tests/ included
- Finding 1, revert: loop._revert_agent_changes() uses `reset --mixed <before>` + checkout + clean (tester, qa_tests/ kept) or `reset --hard <before>` + clean (security), never `checkout -- .` alone, which restores from the already-changed index. The result is re-checked afterwards: if anything is still changed, the task stops with 'could NOT be reverted' instead of trusting unchecked git calls
- Finding 1 applied to the TESTER's guard too (it predates Phase 5 and had the same holes); both roles now share guarded_call()
- Finding 2: the '...'-pieces evidence fallback is REMOVED, not patched - no piece-by-piece match can stop a label from one line being glued to a value from another. One check's evidence is one excerpt; '...' only shortens it. The tester prompt now says one check per scenario, one excerpt per evidence
- Finding 3: quote characters are no longer deleted before comparing. Quote STYLE is unified (' and ` become "), quote PRESENCE is kept - in commands, in tool output, and in security quotes
- Finding 4: the final-regression and security 'done' flags each record the content id of the workspace they checked (loop._worktree_id: a git tree id built in a throwaway index, so uncommitted changes count and the real index is never touched). A different id resets the flag at the start of the next run. Uncommitted changes found right before the final regression can only be a human's manual fix; they are committed ('manual: ...') so they are tested, security-reviewed and delivered, and are not mistaken for tester edits. _worktree_id fails closed: a failed read raises instead of returning '' (two '' would compare equal)
- Finding 5: the security prompt asks for [REDACTED] in place of the secret value only; semantics._quote_in_file checks the rest of the quote against ONE line of the file, pieces in order, a non-empty value at each mask. The old 'sk-...redacted' form is rejected
- Finding 6: orchestrator._merge_diff_ref adds a hash of security_report.json, so an approval made for a different (or no) security report is stale and a new approval is requested
- Finding 7: the schema caps findings at 10 and descriptions at 500 characters; the approval summary shows at most 10 findings, each description at most 200 characters, always names the full report, and comes before the (possibly long) file list; approvals.request marks a cut instead of cutting silently at 4000 characters
- Finding 8: loop.refuse_unverifiable_backend raises before the first agent call (in loop.run_task, and in orchestrator._process before intake); 'sdk' is removed from orch.py run-next, run_task.py and run_evals.py. live_smoke.py and review-lessons keep it (no tool log needed there)
- Finding 9: docs/agent-prompts-for-review.md is regenerated; scripts/gen_prompt_review.py now has render(), reads and writes UTF-8 (it used the Windows default code page before), and a test fails when the committed file differs from render()
- Every fix has a dedicated test; 267 unit tests pass, 3 skipped (Windows symlink privilege)

## Files to read (start here)
- [agentsys/loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/loop.py)  (566 lines)
- [agentsys/semantics.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/semantics.py)  (276 lines)
- [agentsys/orchestrator.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/orchestrator.py)  (381 lines)
- [agentsys/approvals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/agentsys/approvals.py)  (137 lines)
- [.claude/agents/security.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/security.md)  (49 lines)
- [.claude/agents/tester.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/.claude/agents/tester.md)  (43 lines)
- [schemas/security_report.schema.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/schemas/security_report.schema.json)  (116 lines)
- [scripts/gen_prompt_review.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/scripts/gen_prompt_review.py)  (27 lines)
- [scripts/orch.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/scripts/orch.py)  (80 lines)
- [scripts/run_task.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/scripts/run_task.py)  (26 lines)
- [evals/run_evals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/evals/run_evals.py)  (215 lines)
- [tests/test_loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_loop.py)  (781 lines)
- [tests/test_phase3.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_phase3.py)  (584 lines)
- [tests/test_core.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/tests/test_core.py)  (243 lines)
- [docs/review-response.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/fix-timeout-and-retry/docs/review-response.md)  (194 lines)

## Weak points the builder already suspects (confirm, refute, or rank them)
- Finding 2's fix brings back a known false rejection: live task T-0023's tester quoted two scenarios in one check and was rejected. The only mitigation is a prompt rule (one scenario per check). A rejected tester report still blocks the whole task - there is no one-time 'run it again' retry for evidence errors (there is one for coverage gaps)
- guarded_call catches BaseException (KeyboardInterrupt included) and runs git commands before re-raising it. A second interrupt during the revert, or a git crash, is covered only by the post-revert re-check, not by any retry
- The manual-fix rule commits ANY uncommitted change found right before the final regression. If something other than a human leaves files there (a bug in an earlier step), they would be committed and delivered - visible in the merge diff, but not flagged as unexpected
- _worktree_id runs `git add -A` into a throwaway index on every run: it respects .gitignore (changes to ignored files do not count) and costs time on a large workspace
- A task whose progress.json predates this change re-runs the final regression and security once (no stored content id) - safe, but it spends calls
- Security severity is still the model's own label; the code derives the verdict from it but does not judge it
- Live verification of these fixes: none. All evidence is unit tests with a FakeBackend and real git repositories

## How to verify without spending quota
- `python -m unittest discover -s tests -t . -v`
- `python evals/run_evals.py selfcheck`
- `python scripts/gen_prompt_review.py   (must leave docs/agent-prompts-for-review.md unchanged)`

## Review dimensions
1. Correctness: logic errors, wrong state transitions, races, resume/crash behaviour.
2. Billing and quota safety: any path that could call a paid API, loop on calls, or skip the guard or the caps.
3. Security: file-path handling, injection, the dashboard write actions, secrets in logs or files, prompt-injection paths.
4. Enforcement gap: rules in prompts or docs that no code or test enforces.
5. Test quality: tests that pass for the wrong reason, missing failure cases, tests that mirror the implementation.
6. Maintainability and honesty of docs: do README and docs say what the code does? Anything claimed but not built?
7. Specific questions for this phase:
- For each of the 9 findings in docs/review-response.md ('External review of Phase 5'): is it really closed, partly closed, or only moved? Re-run your original reproductions against the new code where you can
- guarded_call: is there any path where an agent call ends (normally or by any exception) and the workspace check does NOT run? Is re-raising a non-StepFailed exception (e.g. a usage-limit pause) after a successful revert the right behavior?
- _revert_agent_changes for the tester uses `reset --mixed <before>` and keeps qa_tests/. Can a tester use qa_tests/ (or a commit that touches both qa_tests/ and product files) to make a product change survive?
- _worktree_id / the 'done' flags: can the workspace content change between the moment a check passes and the moment its id is recorded, so that the recorded id does not match what was checked? Is auto-committing uncommitted changes before the final regression safe?
- With the '...' fallback removed and quote presence kept: is there any remaining way to make evidence with a different meaning match, or any common honest form that is now rejected that was not rejected before Phase 5?
- _quote_in_file with [REDACTED]: can the mask be abused - for example, can a whole line be replaced by masks and short glue so that almost nothing real is checked?
- What would you test that the new tests miss?

## Output format (use exactly this)
1. **Verdict**: one of `ship`, `ship after fixes`, `do not ship`, with one sentence.
2. **Findings table**: columns `#`, `severity (blocker/high/medium/low)`, `file:line`, `what is wrong`, `how to reproduce or verify`, `suggested fix (one line)`.
3. **Claims check**: for each claim above: `verified`, `not verified`, or `false`, with evidence.
4. **Missing tests**: bullet list of concrete test cases.
5. **Things you did NOT review** and why.
Keep it short. No praise. Rank by risk.
