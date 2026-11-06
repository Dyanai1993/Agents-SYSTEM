# Review request: Phase 3 — Orchestrator, approval gates, scheduler, dashboard actions

## Where the code is
- Repository: https://github.com/Dyanai1993/Agents-SYSTEM
- Branch to review: https://github.com/Dyanai1993/Agents-SYSTEM/tree/claude/phase4-memory-evals
- Pull request (diff and discussion): https://github.com/Dyanai1993/Agents-SYSTEM/pull/5
- Compare with main: https://github.com/Dyanai1993/Agents-SYSTEM/compare/main...claude/phase4-memory-evals
Review the branch above, not main. Files are listed relative to the repository root; each one opens at `https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/<path>`.

## Your role
You are an independent REVIEWER. Review only. Do NOT edit, create, delete or commit any file. Do NOT run anything that spends
model quota or touches the network, and never use an API key. The builder (Claude Code) makes every change after reading your report.
If you cannot verify something, say "not verified" and why. Do not guess.

## System in one paragraph
A personal multi-agent engineering system on Claude Code with a Max subscription login. A Python state machine runs one agent at a time (Architect, Developer, Tester) through a core loop with fix loops, re-plans and checkpoints. Phase 3 added intake, a queue and scheduler, a merge approval gate that delivers patch files or a new branch, an Opus approval gate, pause/stop control files, and a local dashboard with Approvals, Usage and Alerts views that can write approval decisions and control files. (Note: the branch also contains the late Phase 3 hardening: task lock and exclusive decision files.)

## Hard constraints of the whole system (check every change against them)
- Billing: Claude Max subscription login only. No API key, no pay-per-token, no extra usage. The billing guard must stay fail-closed.
- Usage is the scarce resource: sequential agents, small contexts, hard loop limits, no tight retry loops.
- Least privilege and human approval for irreversible actions. The dashboard never calls an LLM.
- Agent output is validated by schemas AND code rules. A prompt rule that code does not enforce is a risk: report it.
- Personal use only.

## What this phase claims to deliver
- orchestrator.process() is idempotent: re-running it resumes from files and costs nothing while waiting for a human
- Approving a merge never merges into main, never pushes, and never touches a target repo with uncommitted changes
- run_agent(model='opus') raises until a human decision file for that task exists
- Pause and stop are honored before every agent call and a pause survives scheduler runs
- The scheduler advances exactly one task per call, in priority order, and refuses new tasks when the usage window is nearly used
- Dashboard POSTs are rejected without the X-Agentsys header, JSON content type and a localhost Host/Origin
- Decisions are final: an approval cannot be decided twice

## Files to read (start here)
- [agentsys/orchestrator.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/orchestrator.py)  (188 lines)
- [agentsys/scheduler.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/scheduler.py)  (57 lines)
- [agentsys/approvals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/approvals.py)  (120 lines)
- [agentsys/control.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/control.py)  (27 lines)
- [agentsys/actions.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/actions.py)  (48 lines)
- [agentsys/intake.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/intake.py)  (16 lines)
- [agentsys/loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/loop.py)  (354 lines)
- [agentsys/dashboard_data.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/dashboard_data.py)  (222 lines)
- [dashboard/server.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/dashboard/server.py)  (81 lines)
- [dashboard/index.html](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/dashboard/index.html)  (175 lines)
- [.claude/agents/orchestrator.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/.claude/agents/orchestrator.md)  (34 lines)
- [tests/test_phase3.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/tests/test_phase3.py)  (294 lines)
- [docs/architecture.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/docs/architecture.md)  (56 lines)

## Weak points the builder already suspects (confirm, refute, or rank them)
- A per-task lock file (O_EXCL, stale after 2 hours) and an exclusive-create decision file were added at the end of the phase. They are NEW and lightly tested: look for races, stale-lock takeover problems, and crash-while-locked behaviour
- The intake prompt and the scheduler were never run against a real model; only the core loop was run live
- A usage-limit pause uses resume_after = now + 5 hours, which is a guess, not an official reset time
- needs_input tasks must be resubmitted: there is no in-place answer flow
- Only merge and opus gates are wired. delete, send, spend, credentials gates exist only as names; they are enforced by not granting tools
- Dashboard has no authentication; it relies on 127.0.0.1 binding plus header/Host checks
- git am delivery to a target repo assumes the patches apply on the target tree; the workspace is a separate repo with its own root commit
- Window size constants are guesses; the estimate uses 21k tokens per call from only three live tasks

## How to verify without spending quota
- `python -m unittest discover -s tests -t . -v`
- `python evals/run_evals.py selfcheck`
- `python -m agentsys.billing_guard`
- `python scripts/gen_prompt_review.py (prompts and schemas still in sync)`

## Review dimensions
1. Correctness: logic errors, wrong state transitions, races, resume/crash behaviour.
2. Billing and quota safety: any path that could call a paid API, loop on calls, or skip the guard or the caps.
3. Security: file-path handling, injection, the dashboard write actions, secrets in logs or files, prompt-injection paths.
4. Enforcement gap: rules in prompts or docs that no code or test enforces.
5. Test quality: tests that pass for the wrong reason, missing failure cases, tests that mirror the implementation.
6. Maintainability and honesty of docs: do README and docs say what the code does? Anything claimed but not built?
7. Specific questions for this phase:
- Can an approval or control file be forged or replayed through the dashboard or by a local web page?
- Can a task be delivered without a human decision file, by any path (resume, scheduler, rejected then approved, deleted files)?
- What happens if the process is killed between delivering patches and marking the task done?
- Is there any path where the scheduler calls agents in parallel or in a loop without a cap?
- Does anything write secrets or full environment dumps into logs, payloads or status reports?

## Output format (use exactly this)
1. **Verdict**: one of `ship`, `ship after fixes`, `do not ship`, with one sentence.
2. **Findings table**: columns `#`, `severity (blocker/high/medium/low)`, `file:line`, `what is wrong`, `how to reproduce or verify`, `suggested fix (one line)`.
3. **Claims check**: for each claim above: `verified`, `not verified`, or `false`, with evidence.
4. **Missing tests**: bullet list of concrete test cases.
5. **Things you did NOT review** and why.
Keep it short. No praise. Rank by risk.
