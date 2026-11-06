"""Rules a JSON schema cannot express. Deterministic code, no LLM.

The prompts ask for these things. This module enforces them.
"""
import difflib
import os
import re
from pathlib import Path


# Still open (noted since the second review): an honestly-run command or its real output can be quoted
# with different characters than what a model writes when paraphrasing it back in a report (single vs
# double vs backtick, e.g. `python -c 'print(1)'` vs `python -c "print(1)"`), which used to break both the
# command-match and the evidence-in-output match outright. Quote characters carry no command/output
# semantics of their own here (unlike whitespace or path separators, which can also legitimately differ -
# see _squash below), so dropping all of them before comparing closes this without weakening what the
# checks actually verify: a fabricated claim still has to match everything else in the real text.
_QUOTE_CHARS = str.maketrans("", "", "'\"`")


def norm(cmd: str) -> str:
    return re.sub(r"\s+", " ", (cmd or "").strip()).translate(_QUOTE_CHARS)


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
    need = 1 + 2 * len(items) + 1 + 1      # architect + (developer+tester) per item + final regression + security
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
    """Drop whitespace, mask path-like tokens, and drop quote characters.

    Git Bash on Windows rewrites /tmp/f.txt to C:/Users/.../f.txt in the output, so a quoted
    path never equals the real one. Both sides are masked the same way: the numbers and words
    around the path must still match, the path itself does not. Quote characters ('/"/`) are
    dropped too: a model paraphrasing a real command or output can freely swap one quote style
    for another with no change in meaning, which used to break an exact-match comparison.
    """
    return re.sub(r"\s+", "", _PATH_TOKEN.sub(" <P> ", text or "")).translate(_QUOTE_CHARS)


MIN_SHARE = 0.75


def _evidence_in_output(evidence: str, outputs_squashed: str) -> bool:
    """True when the evidence excerpt is really in a tool output.

    Models add labels BEFORE real output ("stderr: ...", "exit code 1, ..."), shorten it with "...", or
    quote a path that Git Bash rewrote. So a run of evidence characters that appears unchanged in the
    output, at least 6 characters and 75% of the evidence, is allowed - PROVIDED that run reaches all the
    way to the END of the evidence. A label/prefix before the real content can be ignored; the payload
    (the actual value or verdict, which normally comes last) cannot.

    Review (fourth round): without the "reaches the end" requirement, a long shared LABEL could dominate
    the ratio while a short but different trailing value slipped through unverified - "test_result: PASS"
    matched against a real "test_result: FAIL" shared a 12-of-16-character prefix ("test_result:"), clearing
    75%, while the actual verdict word was never checked against reality at all. Requiring the match to
    reach the evidence's own end means whatever is claimed LAST - the part doing the actual claiming - must
    be an exact, unbroken match; only text BEFORE it may be fuzzy/absent from the real output.
    """
    ev = _squash(re.sub(r"\.\.\.|\u2026", "", evidence or ""))      # '...' marks a cut, not content
    if len(ev) < MIN_EVIDENCE_CHARS or not outputs_squashed:
        return False
    m = difflib.SequenceMatcher(None, ev, outputs_squashed, autojunk=False).find_longest_match(0, len(ev), 0, len(outputs_squashed))
    if m.a + m.size != len(ev):      # evidence has its own unmatched tail: the claimed payload itself differs
        return False
    return m.size >= max(MIN_EVIDENCE_CHARS, MIN_SHARE * len(ev))


def evidence_errors(report: dict, tool_log, strict: bool = None) -> list:
    """Every pass/fail check must be backed by a real Bash run in the backend's tool log.

    A check is backed when its evidence excerpt is found in a logged Bash call's own output (see
    _evidence_in_output) - checked first against the output of calls whose command matches the claim, then,
    if none match, against the combined output of every Bash call (this is what lets one batched script
    verify several checks at once). A command match ALONE, with no output check at all, is only enough when
    the evidence is too short/generic to independently verify either way (below MIN_EVIDENCE_CHARS, e.g.
    'OK'): a command really matching the log proves the right thing ran, but proves nothing about the
    claimed OUTCOME. Requiring substantive evidence to match real output closes that gap: a claim like
    "all 50 tests passed" quoted against a real run that actually printed "FAILED (failures=3)" is rejected
    even though the command itself genuinely ran, and a command merely echoed as literal text (not run)
    is rejected too once the evidence is specific enough to check.
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
        # Only "claimed command is a substring of something really logged" counts. The reverse ("logged
        # command is a substring of the claim") is unsound: any real but trivial logged command (e.g. an
        # earlier `echo setup`) would then back ANY longer fabricated claim that happens to contain it
        # verbatim, such as `echo setup && python -m unittest` when only `echo setup` was ever run.
        matched = [t for t in bash if cmd and cmd in norm(t.get("command", ""))]
        by_command = bool(matched)
        if by_command:
            own_output = _squash(" ".join(t.get("output", "") for t in matched))
            if _evidence_in_output(c["evidence"], own_output):
                continue
        if _evidence_in_output(c["evidence"], outputs):     # batched-script case: no single command matches, evidence is real somewhere in the log
            continue
        ev_len = len(_squash(re.sub(r"\.\.\.|…", "", c["evidence"] or "")))
        if by_command and ev_len < MIN_EVIDENCE_CHARS:
            continue   # the command really ran; evidence is too short/generic to independently check either way
        shown = " || ".join(r[:80] for r in ran[:6]) or "no Bash commands"
        if by_command:
            errs.append(f"check '{c['name']}': its command was really run, but the claimed evidence does not match that "
                        f"call's output (or any other logged output). Claimed evidence: {c['evidence'][:160]!r}")
        else:
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
        report["gaps"] = reasons
    return []


# ------------------------------------------------------------------ security report (Phase 5)
def security_evidence_errors(report: dict, ws: Path) -> list:
    """Every high/critical finding must quote real text from the file it names.

    Unlike the tester, the security reviewer is not claiming a command ran with a given output - it is
    claiming a line of code exists and means something. The matching check here is correspondingly simpler
    than evidence_errors(): just confirm the quoted evidence is really, contiguously present in the named
    file (whitespace- and quote-normalized via norm(), the same way a Bash command/output is), and that the
    file is really inside the workspace. A hallucinated finding - a file that does not exist, or a quote
    that is not really there - is rejected before it can send a human chasing something that is not real.
    """
    errs = []
    ws = Path(ws).resolve()
    for f in report["findings"]:
        if f["severity"] not in ("high", "critical"):
            continue
        rel = f["file"]
        try:
            resolved = (ws / rel).resolve()
            resolved.relative_to(ws)
        except (ValueError, OSError):
            errs.append(f"finding in {rel!r}: path escapes the workspace or cannot be resolved")
            continue
        if not resolved.is_file():
            errs.append(f"finding in {rel!r}: no such file in the workspace")
            continue
        try:
            content = norm(resolved.read_text(encoding="utf-8", errors="replace"))
        except OSError as e:
            errs.append(f"finding in {rel!r}: cannot read the file ({e})")
            continue
        ev = norm(f["evidence"])
        if len(ev) < MIN_EVIDENCE_CHARS or ev not in content:
            errs.append(f"finding in {rel!r}: evidence does not appear in the file. Claimed: {f['evidence'][:160]!r}")
    return errs


def derive_security_verdict(findings) -> str:
    return "fail" if any(f["severity"] in ("high", "critical") for f in findings) else "pass"


def process_security_report(report: dict, ws: Path) -> list:
    """Used as run_agent post_process for the security role.

    'blocked' is a genuine self-report (the reviewer needs information only a human has) and is never
    overridden. Otherwise the verdict is derived from finding severities, the same way the tester's verdict
    is derived from its checks: a report that under-states its own findings (says 'pass' despite a high/
    critical one) is silently corrected, since the right data to decide is already there; one that over-
    states them (says 'fail'/'blocked' with nothing high/critical) is a hard error asking it to reconsider,
    since nothing demonstrates the claim.
    """
    errs = security_evidence_errors(report, ws)
    reported = report["verdict"]
    if reported == "blocked":
        return errs
    derived = derive_security_verdict(report["findings"])
    if derived == "pass" and reported != "pass":
        errs.append(f"reported verdict '{reported}' but no finding is high/critical severity: add the finding or report pass")
    if errs:
        return errs
    report["verdict_reported"] = reported
    report["verdict"] = derived
    return []
