"""Rules a JSON schema cannot express. Deterministic code, no LLM.

The prompts ask for these things. This module enforces them.
"""
import difflib
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


MIN_EVIDENCE_CHARS = 6


_PATH_TOKEN = re.compile(r"\S*[/\\]\S*|\b[A-Za-z]:\S*")


def _squash(text: str) -> str:
    """Drop whitespace and mask path-like tokens.

    Git Bash on Windows rewrites /tmp/f.txt to C:/Users/.../f.txt in the output, so a quoted
    path never equals the real one. Both sides are masked the same way: the numbers and words
    around the path must still match, the path itself does not.
    """
    return re.sub(r"\s+", "", _PATH_TOKEN.sub(" <P> ", text or ""))


MIN_SHARE = 0.75


def _evidence_in_output(evidence: str, outputs_squashed: str) -> bool:
    """True when the evidence excerpt is really in a tool output.

    Models add labels around real output ("stderr: ...", "exit code 1, ..."), shorten it with "...",
    or quote a path that Git Bash rewrote. So the test is: the longest run of evidence characters that
    appears unchanged in the output must be at least 6 characters AND 75% of the evidence.
    Short evidence ('OK', '0') can never pass this: it needs a matching command instead.
    """
    ev = _squash(re.sub(r"\.\.\.|\u2026", "", evidence or ""))      # '...' marks a cut, not content
    if len(ev) < MIN_EVIDENCE_CHARS or not outputs_squashed:
        return False
    m = difflib.SequenceMatcher(None, ev, outputs_squashed, autojunk=False).find_longest_match(0, len(ev), 0, len(outputs_squashed))
    return m.size >= max(MIN_EVIDENCE_CHARS, MIN_SHARE * len(ev))


def evidence_errors(report: dict, tool_log, strict: bool = None) -> list:
    """Every pass/fail check must be backed by a real Bash run in the backend's tool log.

    A check is backed when EITHER its command text matches a logged Bash command
    OR its evidence excerpt is found in the output of a logged Bash call (see _evidence_in_output).
    The second rule allows one script to verify several checks, while fabricated output is still caught.
    """
    if strict is None:
        strict = os.environ.get("AGENTSYS_EVIDENCE", "strict") != "warn"
    if tool_log is None:
        return ["backend gave no tool log: evidence cannot be verified"] if strict else []
    bash = [t for t in tool_log if t.get("tool") == "Bash"]
    ran = [norm(t.get("command", "")) for t in bash]
    outputs = _squash(" ".join(t.get("output", "") for t in bash))
    errs = []
    for c in report["checks"]:
        if c["result"] == "skipped":
            continue
        cmd = norm(c["command"])
        by_command = bool(cmd) and any(cmd in r or (r and r in cmd) for r in ran)
        if by_command or _evidence_in_output(c["evidence"], outputs):
            continue
        shown = " || ".join(r[:80] for r in ran[:6]) or "no Bash commands"
        errs.append(f"check '{c['name']}': neither its command nor its evidence appears in the tool log. "
                    f"Claimed command: {cmd[:120]!r}. Logged Bash calls ({len(ran)}): {shown}")
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
