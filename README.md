# Multi-agent engineering system (personal use, subscription only)

Status: **Phase 5 – Security specialist** (phases 1-4 built; phases 1-2 verified live; phase 4 reviewed 4 rounds, not independently re-reviewed since). Next: Phase 6 (hardening). Repository: https://github.com/Dyanai1993/Agents-SYSTEM

## Rules that never change
- Billing = your Claude Max login (`claude login`). No API key. `make guard` fails if any API-key / provider variable is set.
- Personal use only. One agent at a time. Hard caps per task (see `agentsys/config.py`, tuned for Max 5x, all marked "assumption").
- Verified billing facts and sources: `docs/billing-assumptions.md`.

## Quick start (on your own computer)
```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
claude login            # subscription login, once
make test               # 35 unit tests, zero quota
make evals              # eval self-check: every check passes on reference, fails on empty
make guard              # billing guard
make demo && make dashboard   # demo data via fake backend, dashboard at http://127.0.0.1:8765
```

### Windows (no `make`)
```powershell
python -m unittest discover -s tests -t . -v      # make test
python evals/run_evals.py selfcheck               # make evals
python -m agentsys.billing_guard                  # make guard
python scripts/seed_demo.py; python dashboard/server.py 8765   # make demo; make dashboard
```
No need to install `make`: every target is one plain Python command.

## Layout
| Path | Purpose |
|---|---|
| `agentsys/billing_guard.py` | env + settings + runtime credential-source check (fail closed) |
| `agentsys/runner.py` | `run_agent()` and the `sdk` / `cli` / `fake` backends |
| `agentsys/ledger.py` | per-task `budget.json` + rolling usage window estimate |
| `agentsys/logger.py`, `schemas/` | validated event log, payload files, JSON schemas |
| `evals/` | 8 benchmark tasks with reference solutions and check scripts |
| `dashboard/` | local read-only dashboard (Overview, Activity, Messages) |

See `docs/architecture.md`.

## Start here: one command
```bash
python scripts/up.py                 # the dashboard AND the worker, in one terminal (Ctrl+C stops both)
```
Open http://127.0.0.1:8765. The header line says whether the worker is alive and what it is doing (idle; answering
your message; running T-0025 with the developer for 12 min), or that it is offline, crashed, or runs old code.
1. **New task**: write what you want in your own words (Hebrew or English); the assistant asks what it needs and keeps
   an editable draft. Nothing is queued until you approve a revision.
2. **Task monitor**: press **Start**. The worker plans, builds and tests it until it needs you. While it runs you see
   the current agent and how long it has been working; the heartbeat proves it is alive (a long agent call is quiet).
3. When it needs you, the Task monitor says what and offers the one next action: answer its question right there,
   decide an approval (Approvals tab), **Continue** after an approval, **Retry** after a block, **Resume** after a stop.
4. **Review** (in the Task monitor): every acceptance criterion with the final checks and their evidence, the security
   report, the changed files and the real diff - of the exact commit the approval delivers. **Approve and deliver**
   writes the patch files to `deliveries/<task>/` (and a branch if a target repo was set) and shows a receipt with
   the patch names, the folder, the commit and how to apply them (`git am`). Nothing is merged into main.

- The worker is the only process that calls a model: it answers chat messages and runs the tasks you Start/Continue,
  never anything else, and makes no call while nothing waits. Only one worker can run. After updating the code,
  restart `up.py` (the header tells you when a process runs old code).
- The dashboard never calls a model: every button writes a request file. Refreshing the page costs nothing. A failed
  chat turn keeps your draft and waits for Retry - it is never retried on its own.
- Pause and Stop act at once when nothing is running, otherwise before the next agent call. Stop is undone by Resume.
- Still there for terminals: `python scripts/orch.py worker` (the worker alone; `assistant` is the old name) and
  `python scripts/orch.py run-next` (one run, it shows in the header too).
- Ctrl+Enter sends (Enter adds a line). You can type your next message while the assistant answers, and the page keeps
  refreshing without losing what you typed. Click a proposal to quote it into your reply.
- Edit the draft by hand any time; a late assistant reply never overwrites a newer edit (revision numbers).
- "Review and submit" shows the exact requirement; "Approve and submit" queues ONE task with exactly that requirement
  (a retry or double click returns the same task). It goes straight to the Architect: no intake, no requirement gate.
- Drafts live in `drafts/D-####/session.json`, apart from tasks. Limits per session: the usual task caps (30 calls,
  450k tokens), messages up to 8000 characters, the last 20 messages / 16000 characters sent to the model.

## Run a task end to end (CLI)
```bash
python scripts/orch.py submit T-0010 --request "A CLI that converts CSV to JSON, stdlib only" [--target-repo PATH]
python scripts/orch.py run-next       # intake writes a requirement DRAFT; the task waits for your approval
python scripts/orch.py approve T-0010-requirements-1    # or the dashboard; `retry T-0010 --answer "..."` changes the draft
python scripts/orch.py run-next       # plan -> build/test/fix -> final regression -> security -> merge gate
python scripts/orch.py pending        # or use the dashboard: Approvals tab
python scripts/orch.py approve T-0010-merge-1
python scripts/orch.py run-next       # delivers patches (and branch agentsys/T-0010 in the target repo)
python scripts/orch.py pause|stop|resume T-0010
python scripts/orch.py delete T-0010  # or Task monitor -> Delete task: moves it to trash/ (recoverable), never while it runs
```
`submit --goal G --criteria C` gives a structured requirement directly and skips the requirement approval.
Approving a merge never merges into `main` and never pushes: it writes patch files to `deliveries/<task>/` and, with `--target-repo`, creates a new branch there.

## Learning loop (Phase 4)
```bash
python scripts/orch.py lessons                    # consolidated lessons (written by code after each task)
python scripts/orch.py review-lessons [--force]   # Haiku proposes small prompt changes (needs 5+ lessons)
python scripts/orch.py proposals                  # then approve each one in the dashboard (Approvals tab)
python scripts/orch.py apply-proposal P-1         # new branch agentsys/lesson-P-1 + free checks
python evals/run_evals.py real --label baseline --yes        # REAL quota: about 8 x 90k tokens (estimate)
git checkout agentsys/lesson-P-1                             # the AFTER run must be on the proposal branch (clean tree)
python evals/run_evals.py real --label after-P-1 --yes       # run on the proposal branch
git checkout main
python scripts/orch.py gate-proposal P-1 --before evals/results/real-baseline-*.json --after evals/results/real-after-P-1-*.json
```
The BEFORE run must be on the base commit, both runs must be complete and cover the full suite. Invalid input gives no verdict. A regression or more than 25% token growth deletes the branch. Otherwise the branch stays and you open the PR. `recover-proposal` repairs an interrupted apply.
Nothing is merged or pushed by the system.

## Definition of done per phase
See `docs/process.md`. Each phase ends with a self-review prompt for an independent reviewer: `docs/reviews/phase<N>-review-prompt.md`.
