"""Rules a JSON schema cannot express. Deterministic code, no LLM.

The prompts ask for these things. This module enforces them.
"""
import os
import re


def norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", (cmd or "").strip())


# ------------------------------------------------------------------ plan
def plan_errors(plan: dict, ac_ids, call_cap: int) -> list:
    errs = []
    items = plan["work_items"]
    ids = [w["id"] for w in items]
    if len(set(ids)) != len(ids):
        errs.append("duplicate work item ids")
    seen = set()
    for w in items:
        for d in w["depends_on"]:
            if d == w["id"]:
                errs.append(f"{w['id']} depends on itself")
            elif d not in ids:
                errs.append(f"{w['id']} depends on unknown item {d}")
            elif d not in seen:
                errs.append(f"{w['id']} is listed before its dependency {d} (order must respect dependencies)")
        for c in w["covers"]:
            if c not in ac_ids:
                errs.append(f"{w['id']} covers unknown criterion {c}")
        seen.add(w["id"])
    covered = {c for w in items for c in w["covers"]}
    for missing in sorted(set(ac_ids) - covered, key=lambda x: int(x[2:])):
        errs.append(f"requirement criterion {missing} is not covered by any work item")
    if plan["complexity"] == "trivial" and len(items) > 3:
        errs.append("a trivial plan has at most 3 work items")
    need = 1 + 2 * len(items) + 1          # architect + (developer+tester) per item + final regression
    if need > call_cap:
        errs.append(f"plan needs at least {need} agent calls but the task cap is {call_cap}: split the task")
    return errs


# ------------------------------------------------------------------ test report
def derive_verdict(report: dict, expected_ids, constraint_ids):
    """Decision order: demonstrated failure -> fail; unperformed required check -> blocked; else pass."""
    checks = report["checks"]
    reasons = []
    if any(c["result"] == "fail" for c in checks):
        return "fail", reasons
    skipped = [c["name"] for c in checks if c["result"] == "skipped"]
    if skipped:
        reasons.append(f"skipped checks: {', '.join(skipped)}")
    passed = {i for c in checks if c["result"] == "pass" for i in c["criterion_ids"]}
    for cid in expected_ids:
        if cid not in passed:
            reasons.append(f"criterion {cid} has no passing check")
    na = {x["id"] for x in report["constraints_na"]}
    for cid in constraint_ids:
        if cid not in passed and cid not in na:
            reasons.append(f"constraint {cid} is neither checked nor marked not-applicable")
    return ("blocked" if reasons else "pass"), reasons


def evidence_errors(report: dict, tool_log, strict: bool = None) -> list:
    """Every pass/fail check must name a command that really ran (from the backend's tool log)."""
    if strict is None:
        strict = os.environ.get("AGENTSYS_EVIDENCE", "strict") != "warn"
    if tool_log is None:
        return ["backend gave no tool log: evidence cannot be verified"] if strict else []
    ran = [norm(t.get("command", "")) for t in tool_log if t.get("tool") == "Bash"]
    errs = []
    for c in report["checks"]:
        if c["result"] == "skipped":
            continue
        cmd = norm(c["command"])
        if not cmd or not any(cmd in r or (r and r in cmd) for r in ran):
            errs.append(f"check '{c['name']}': command was not run (not in tool log)")
    return errs


def process_test_report(report, expected_ids, constraint_ids, tool_log):
    """Used as run_agent post_process for the tester. Overrides the verdict with the derived one.

    Returns a list of hard errors. Reported 'pass' with failing checks is corrected to 'fail'.
    A reported fail/blocked while every check passed is inconsistent: hard error.
    """
    errs = evidence_errors(report, tool_log)
    derived, reasons = derive_verdict(report, expected_ids, constraint_ids)
    reported = report["verdict"]
    if derived == "pass" and reported != "pass":
        errs.append(f"reported verdict '{reported}' but every check passed: add the failing check or report pass")
    if errs:
        return errs
    report["verdict_reported"] = reported
    report["verdict"] = derived
    if reasons and derived == "blocked":
        report["summary"] = (report["summary"] + " | " + "; ".join(reasons))[:600]
    return []
