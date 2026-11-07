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

## Start here: the Requirements Assistant
Write what you want in your own words (Hebrew or English); the assistant asks what it needs, keeps an editable draft,
and nothing is queued until you approve a revision. Run these on your own computer, each in its own terminal:
```bash
python dashboard/server.py 8765      # then open http://127.0.0.1:8765 -> "New task"
python scripts/orch.py assistant     # answers your messages: one Haiku call per message, nothing while idle
python scripts/orch.py run-next      # once you approved and submitted a draft: builds it (one step per run)
```
- The dashboard never calls a model: a message is saved as a turn request, and `orch.py assistant` (or `run-next`)
  answers it. Refreshing the page costs nothing. A failed turn (bad output, usage limit) keeps your draft and waits
  for you to press Retry - it is never retried on its own.
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
