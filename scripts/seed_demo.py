"""Create demo tasks through the REAL runner with the fake backend (zero quota)."""
import json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import tasks, runner  # noqa: E402

PLAN = {"status": "ok", "summary": "Add /health endpoint", "complexity": "trivial",
        "tech_choices": [{"choice": "stdlib http.server", "reason": "no deps", "rejected": ["flask"]}],
        "work_items": [{"id": "W1", "title": "health route", "files": ["app.py"], "acceptance_criteria": ["GET /health returns 200"],
                        "covers": ["AC1"], "depends_on": []}],
        "test_strategy": "unit test", "risks": []}
REPORT = {"verdict": "pass", "summary": "1 test ok", "design_problem": False, "constraints_na": [],
          "checks": [{"name": "unit", "result": "pass", "criterion_ids": ["W1.1"], "evidence": "Ran 1 test in 0.001s OK", "command": "python -m unittest"}]}
DEVR = {"status": "ok", "summary": "added route", "files_changed": ["app.py"], "tests_added": ["t.py"], "tests_run": "unittest OK"}
fb = runner.FakeBackend({"architect": PLAN, "tester": REPORT, "developer": DEVR})
for tid, title in (("demo-1", "Add health endpoint"), ("demo-2", "Parse CSV report")):
    tasks.set_state(tid, "plan", title=title, current_agent="architect")
    runner.run_agent("architect", {"requirement": title}, task_id=tid, backend=fb)
    tasks.set_state(tid, "build", current_agent="developer")
    runner.run_agent("developer", {"work_item": "W1"}, task_id=tid, backend=fb)
    tasks.set_state(tid, "test", current_agent="tester")
    runner.run_agent("tester", {"plan": "W1"}, task_id=tid, backend=fb)
tasks.set_state("demo-1", "done", current_agent=None)
tasks.set_state("demo-2", "deploy", current_agent=None, waiting_approval=True)
print("demo data written under", os.environ.get("AGENTSYS_HOME", "project root"))
