# Multi-agent engineering system (personal use, subscription only)

Status: **Phase 4 – Shared memory and improvement loop** (phases 1-3 built; phases 1-2 verified live). Next: specialists (Phase 5). Repository: https://github.com/Dyanai1993/Agents-SYSTEM

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

## Run a task end to end (Phase 3)
```bash
python scripts/orch.py submit T-0010 --request "A CLI that converts CSV to JSON, stdlib only" [--target-repo PATH]
python scripts/orch.py run-next       # advances ONE task: intake -> plan -> build/test/fix -> merge gate
python scripts/orch.py pending        # or use the dashboard: Approvals tab
python scripts/orch.py approve T-0010-merge-1
python scripts/orch.py run-next       # delivers patches (and branch agentsys/T-0010 in the target repo)
python scripts/orch.py pause|stop|resume T-0010
```
Approving a merge never merges into `main` and never pushes: it writes patch files to `deliveries/<task>/` and, with `--target-repo`, creates a new branch there.

## Learning loop (Phase 4)
```bash
python scripts/orch.py lessons                    # consolidated lessons (written by code after each task)
python scripts/orch.py review-lessons [--force]   # Haiku proposes small prompt changes (needs 5+ lessons)
python scripts/orch.py proposals                  # then approve each one in the dashboard (Approvals tab)
python scripts/orch.py apply-proposal P-1         # new branch agentsys/lesson-P-1 + free checks
python evals/run_evals.py real --label baseline --yes        # REAL quota: about 8 x 90k tokens (estimate)
python evals/run_evals.py real --label after-P-1 --yes       # run on the proposal branch
python scripts/orch.py gate-proposal P-1 --before evals/results/real-baseline-*.json --after evals/results/real-after-P-1-*.json
```
A regression, a missing task or more than 25% token growth deletes the branch. Otherwise the branch stays and you open the PR.
Nothing is merged or pushed by the system.

## Definition of done per phase
See `docs/process.md`. Each phase ends with a self-review prompt for an independent reviewer: `docs/reviews/phase<N>-review-prompt.md`.
