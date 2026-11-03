"""Orchestrator CLI.

  python scripts/orch.py submit T-0010 --request "Make a CLI that ..." [--target-repo PATH]
  python scripts/orch.py submit T-0010 --goal G --criteria C [--criteria C2] [--constraint X]
  python scripts/orch.py run-next [--backend cli|sdk]     advances ONE task (sequential)
  python scripts/orch.py status
  python scripts/orch.py pending | approve ID [--note N] | reject ID [--note N]
  python scripts/orch.py pause|stop|resume TASK_ID
"""
import argparse, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import approvals, billing_guard, control, orchestrator, runner, scheduler  # noqa: E402

ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd", required=True)
s = sub.add_parser("submit"); s.add_argument("task_id"); s.add_argument("--request"); s.add_argument("--goal")
s.add_argument("--criteria", action="append", default=[]); s.add_argument("--constraint", action="append", default=[])
s.add_argument("--target-repo"); s.add_argument("--priority", type=int, default=5)
r = sub.add_parser("run-next"); r.add_argument("--backend", default="cli", choices=["cli", "sdk"])
sub.add_parser("status"); sub.add_parser("pending")
for n in ("approve", "reject"):
    x = sub.add_parser(n); x.add_argument("id"); x.add_argument("--note", default="")
for n in ("pause", "stop", "resume"):
    x = sub.add_parser(n); x.add_argument("task_id")
a = ap.parse_args()

if a.cmd == "submit":
    req = None
    if a.goal:
        req = {"goal": a.goal, "constraints": a.constraint, "acceptance_criteria": a.criteria, "risks": [], "unknowns": []}
    e = orchestrator.submit(a.task_id, request_text=a.request, requirements=req, target_repo=a.target_repo, priority=a.priority)
    print("queued:", e["task_id"])
elif a.cmd == "run-next":
    try:
        billing_guard.run_guard()
    except billing_guard.BillingGuardError as e:
        print(e, file=sys.stderr); sys.exit(2)
    out = scheduler.run_next(backend=runner.get_backend(a.backend))
    print("nothing to run" if out is None else json.dumps({k: out.get(k) for k in ("task_id", "status", "note", "skipped")}, indent=2))
elif a.cmd == "status":
    for e in scheduler.entries():
        print(f"{e['task_id']:<12} {e['status']:<17} {e['title'][:50]}  {e['note'][:80]}")
elif a.cmd == "pending":
    for x in approvals.pending():
        print(f"{x['id']}  [{x['gate']}]\n{x['context']}\n")
elif a.cmd in ("approve", "reject"):
    print(approvals.decide(a.id, "approved" if a.cmd == "approve" else "rejected", a.note))
else:
    print(control.set_control(a.task_id, a.cmd))
