"""Run or resume a task.

  python scripts/run_task.py T-0001 --goal "Build a CLI that counts words" --criteria "prints counts" [--backend cli]

Real backends use your Claude login. The billing guard runs before every call.
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import billing_guard, loop, runner  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("task_id")
ap.add_argument("--goal", required=True)
ap.add_argument("--criteria", action="append", required=True, help="acceptance criterion (repeatable)")
ap.add_argument("--constraint", action="append", default=[])
ap.add_argument("--backend", default="cli", choices=["cli"])   # sdk builds no tool log: see loop.refuse_unverifiable_backend
a = ap.parse_args()
try:
    billing_guard.run_guard()
except billing_guard.BillingGuardError as e:
    print(e, file=sys.stderr); sys.exit(2)
req = {"goal": a.goal, "constraints": a.constraint, "acceptance_criteria": a.criteria, "risks": [], "unknowns": []}
res = loop.run_task(a.task_id, req, backend=runner.get_backend(a.backend))
print(json.dumps(res, indent=2))
sys.exit(0 if res["status"] in ("ready_for_review", "paused") else 1)
