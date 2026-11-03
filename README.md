# Multi-agent engineering system (personal use, subscription only)

Status: **Phase 3 – Orchestrator** (phases 1-2 done and verified live). Next: shared memory (Phase 4).

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
