# Review request: Phase 4 — Shared memory, lessons, controlled improvement loop, real eval runner and gate

## Where the code is
- Repository: https://github.com/Dyanai1993/Agents-SYSTEM
- Branch to review: https://github.com/Dyanai1993/Agents-SYSTEM/tree/claude/phase4-memory-evals
- Compare with main: https://github.com/Dyanai1993/Agents-SYSTEM/compare/main...claude/phase4-memory-evals
Review the branch above, not main. Files are listed relative to the repository root; each one opens at `https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/<path>`.

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
- lessons.md is capped at 60 lines, deduplicated by key, and at most 5 lessons are injected per agent call, only for the matching role or 'all'
- The reviewer runs on Haiku, only with 5+ lessons (or --force), and its proposals are filtered by code: allowed targets only, old_text exactly once, protected wording kept, source lessons must exist
- No proposal is applied without a human approval file; apply works on a NEW branch, runs unit tests and eval selfcheck first, and never touches main or pushes
- A proposal that fails free checks is reverted and its branch deleted
- The eval gate reverts the branch on any regression, any missing task, or more than 25% token growth; otherwise it keeps the branch for a human PR
- evals/run_evals.py real needs --yes, runs sequentially, stops at a token budget, and records pass, fix loops, calls, tokens and seconds per task
- The dashboard Learning view shows lessons, proposals with state, real eval runs and a before/after comparison with regressions

## Files to read (start here)
- [agentsys/memory.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/memory.py)  (145 lines)
- [agentsys/learning.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/learning.py)  (200 lines)
- [agentsys/loop.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/loop.py)  (330 lines)
- [agentsys/orchestrator.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/orchestrator.py)  (188 lines)
- [agentsys/dashboard_data.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/agentsys/dashboard_data.py)  (222 lines)
- [evals/run_evals.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/evals/run_evals.py)  (145 lines)
- [.claude/agents/reviewer.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/.claude/agents/reviewer.md)  (38 lines)
- [.claude/agents/tester.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/.claude/agents/tester.md)  (43 lines)
- [.claude/agents/developer.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/.claude/agents/developer.md)  (45 lines)
- [.claude/agents/architect.md](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/.claude/agents/architect.md)  (51 lines)
- [schemas/lesson.schema.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/schemas/lesson.schema.json)  (70 lines)
- [schemas/proposals.schema.json](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/schemas/proposals.schema.json)  (66 lines)
- [dashboard/index.html](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/dashboard/index.html)  (175 lines)
- [scripts/orch.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/scripts/orch.py)  (72 lines)
- [scripts/gen_review_prompt.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/scripts/gen_review_prompt.py)  (90 lines)
- [tests/test_phase4.py](https://github.com/Dyanai1993/Agents-SYSTEM/blob/claude/phase4-memory-evals/tests/test_phase4.py)  (310 lines)

## Weak points the builder already suspects (confirm, refute, or rank them)
- The reviewer prompt, lesson injection and the real eval runner were NEVER run against a real model. Proposal quality from Haiku is unknown
- LLM runs are noisy. The gate compares ONE before run with ONE after run, so a regression can be luck and an improvement can be luck. The 25% token threshold is arbitrary
- apply() runs only free checks (unit tests, selfcheck). They prove nothing broke; they do not measure whether the new rule helps
- Lessons are an injection path into future prompts. Mitigations: templates, a sanitized 60-character detail, a schema pattern forbidding newlines/backticks/braces, and DATA framing in the prompts. Is that enough?
- The PROTECTED-wording check is a crude substring test; a proposal could weaken a rule without containing those words
- Eval runs use an isolated home, so they measure prompt changes but never lesson injection
- A real eval run costs about 8 x 90k tokens (estimate from 3 live tasks). Its share of a Max 5x window is unknown
- Lessons are extracted only by orchestrator.process(); tasks run with scripts/run_task.py record nothing
- Proposal approvals use pseudo task ids ('lesson-P-1'); the Approvals tab shows them without a task title
- This branch is stacked on the open PR #6 (tester coverage retry)

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
- Can a lesson or a proposal carry an instruction into a future prompt that the schema, sanitizer and DATA framing do not stop?
- Can a proposal change a file outside the allowed targets, via path tricks, symlinks, or an old_text that spans files?
- Can apply() leave the repo on the wrong branch, dirty, or with a half-applied change after a crash? Check every exit path
- Is a single before/after eval run a defensible gate? What is the cheapest change that makes it more trustworthy (repeat runs, per-task thresholds, paired comparison)?
- Does anything in the eval runner or learning code spend quota without --yes, loop on calls, or bypass the billing guard and the budget caps?
- Which metric would show that the improvement loop actually improves anything, and is it recorded?

## Output format (use exactly this)
1. **Verdict**: one of `ship`, `ship after fixes`, `do not ship`, with one sentence.
2. **Findings table**: columns `#`, `severity (blocker/high/medium/low)`, `file:line`, `what is wrong`, `how to reproduce or verify`, `suggested fix (one line)`.
3. **Claims check**: for each claim above: `verified`, `not verified`, or `false`, with evidence.
4. **Missing tests**: bullet list of concrete test cases.
5. **Things you did NOT review** and why.
Keep it short. No praise. Rank by risk.
