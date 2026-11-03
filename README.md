# Multi-agent engineering system (personal use, subscription only)

Status: **Phase 1 – Foundation.** No real agent prompts yet (Phase 2).

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
