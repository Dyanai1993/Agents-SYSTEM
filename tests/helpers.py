"""Builders for scripted agent replies that satisfy the schemas."""
import json

REQ = {"goal": "word counter", "constraints": [], "acceptance_criteria": ["counts words"], "risks": [], "unknowns": []}
REQ_C = dict(REQ, constraints=["no third-party packages"])


def plan(n=2, risks=None, covers=("AC1",), complexity="trivial"):
    items = []
    for i in range(1, n + 1):
        items.append({"id": f"W{i}", "title": f"item {i}", "files": [], "acceptance_criteria": ["ok"],
                      "covers": list(covers), "depends_on": [f"W{i - 1}"] if i > 1 else []})
    return {"status": "ok", "summary": "plan", "complexity": complexity, "tech_choices": [], "test_strategy": "unittest",
            "risks": risks or [], "work_items": items}


NEEDS = {"status": "needs_input", "blocking_reason": "No database named.", "questions": ["Which database?"]}
DEV = {"status": "ok", "summary": "done", "files_changed": ["a.py"], "tests_added": ["t.py"], "tests_run": "unittest OK",
       "notes": "SECRET-DEV-REASONING"}


def _item(prompt):
    p = json.loads(prompt)
    wi = p["work_item"]
    return wi["id"], [c["id"] for c in wi["acceptance_criteria"]], [c["id"] for c in p.get("constraints", [])]


def _check(name, result, ids, **kw):
    # A GOOD report quotes the line of its own scenario; a bare "OK" (an aggregate) no longer proves a behaviour
    c = {"name": name, "result": result, "criterion_ids": ids, "command": "python -m unittest -v",
         "evidence": f"{name} ... ok"}
    if result == "fail":
        c.update(evidence="AssertionError", severity="high", repro="run it", probable_cause="bug")
    if result == "skipped":
        c.update(command="", evidence="tool missing")
    c.update(kw)
    return c


def report(verdict, checks, **kw):
    r = {"verdict": verdict, "summary": f"{verdict}", "design_problem": False, "constraints_na": [], "checks": checks}
    r.update(kw)
    return r


def t_pass(prompt):
    _, ids, cons = _item(prompt)
    return report("pass", [_check("all", "pass", ids)], constraints_na=[{"id": c, "reason": "n/a here"} for c in cons])


def t_fail(prompt, design=False):
    _, ids, cons = _item(prompt)
    kw = {"design_problem": True, "design_rationale": "interface is wrong"} if design else {}
    return report("fail", [_check("bad", "fail", ids)], constraints_na=[{"id": c, "reason": "n/a"} for c in cons], **kw)


def t_design(prompt):
    return t_fail(prompt, design=True)


def t_blocked(prompt):
    _, ids, cons = _item(prompt)
    return report("blocked", [_check("cannot run", "skipped", ids)], constraints_na=[{"id": c, "reason": "n/a"} for c in cons])
