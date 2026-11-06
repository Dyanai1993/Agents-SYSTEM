"""Controlled improvement loop. Nothing changes an agent prompt without a human decision AND a valid, paired eval gate.

review()   Haiku clusters lessons -> proposals (files) + one approval request each (gate 'lesson', bound to a content hash)
apply()    only when approved AND the proposal/target are unchanged since approval: new git branch, exact edit, free checks
gate()     validates a BEFORE and an AFTER real eval run (same full suite, tied to the base and branch commits), then keeps or deletes
recover()  cleans up after an interrupted apply
Nothing here merges into main or pushes.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import approvals, config, logger, memory, runner, schema

TARGET_RE = re.compile(r"^(\.claude/agents/(architect|developer|tester|orchestrator)\.md|memory/patterns/[A-Za-z0-9_-]{1,40}\.md)$")
FORBIDDEN_NEW = re.compile(r"\b(skip|ignore|waive|disable|bypass|api[ _-]?key|approve[sd]?|without (test|check|verif)\w*|no need to (test|check))\b", re.I)
MARKERS = re.compile(r"\b(never|do not|must not|only|forbidden)\b", re.I)


def repo() -> Path:
    return Path(os.environ.get("AGENTSYS_REPO") or Path(__file__).resolve().parent.parent)


def _pdir():
    d = config.home() / "memory" / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git(*args):
    return subprocess.run(["git", "-c", "user.name=agentsys", "-c", "user.email=agentsys@localhost", *args], cwd=repo(), capture_output=True, text=True)


def _must(*args):
    """Run git and fail loudly: a silent git failure must never turn into a success state."""
    r = _git(*args)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout.strip()


def _branch_exists(branch):
    return _git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0


# ------------------------------------------------------------------ safe targets and proposals
def _safe_path(rel):
    """Path inside the repo with no symlink in it (file or any parent) and no escape. Returns Path or None."""
    root = repo().resolve()
    p = repo() / rel
    cur = repo()
    for part in Path(rel).parts:
        cur = cur / part
        if os.path.islink(cur):
            return None
    try:
        if not p.resolve().is_relative_to(root):
            return None
    except (OSError, ValueError):
        return None
    return p


def content_hash(doc) -> str:
    """Hash of the proposal AND the current target content: the approval is for exactly this change on exactly this file."""
    p = _safe_path(doc["target"])
    cur = hashlib.sha256(p.read_bytes()).hexdigest() if p and p.is_file() else "none"
    body = {k: doc.get(k) for k in ("target", "kind", "old_text", "new_text", "rationale", "source_lessons")}
    return hashlib.sha256(json.dumps(dict(body, target_sha=cur), sort_keys=True).encode()).hexdigest()


def _section_of(text, old):
    i = text.find(old)
    heads = [(m.start(), m.group(1)) for m in re.finditer(r"^## (.+)$", text, re.M)]
    name = ""
    for pos, h in heads:
        if pos <= i:
            name = h
    return name


def proposal_errors(p, lesson_ids) -> list:
    errs = []
    if not TARGET_RE.match(p["target"]):
        return [f"target not allowed: {p['target']}"]
    path = _safe_path(p["target"])
    if path is None:
        return ["target path escapes the repo or contains a symlink"]
    new, old = p["new_text"], p.get("old_text")
    if FORBIDDEN_NEW.search(new):
        errs.append("new_text contains wording that weakens checks (skip/ignore/waive/disable/bypass/api key/approve/without testing)")
    if old:
        if not path.is_file():
            errs.append("target file does not exist")
        else:
            text = path.read_text(encoding="utf-8")
            if text.count(old) != 1:
                errs.append("old_text must occur exactly once in the target")
            elif p["target"].startswith(".claude/agents/") and not _section_of(text, old).startswith("Rules"):
                errs.append(f"only the 'Rules' section of a prompt may change (old_text is in '{_section_of(text, old) or 'the header'}')")
            if len(MARKERS.findall(new)) < len(MARKERS.findall(old)):
                errs.append("change removes a never/do-not/only rule")
    elif p["target"].startswith(".claude/agents/"):
        errs.append("a change to an agent prompt needs old_text")
    elif path.exists():
        errs.append("pattern file already exists: use old_text to extend it")
    missing = [i for i in p["source_lessons"] if i not in lesson_ids]
    if missing:
        errs.append(f"unknown source lessons: {missing}")
    return errs


def _next_id():
    nums = [int(m.group(1)) for f in _pdir().glob("P-*.json") if (m := re.match(r"P-(\d+)\.json$", f.name))]
    return f"P-{max(nums, default=0) + 1}"


def review(backend, *, force=False, env=None, settings_paths=None):
    """Return {'status', 'proposals': [ids], 'rejected': [reasons]}. Each review gets its own task id and ledger."""
    lessons = memory.inbox_lessons()
    if len(lessons) < config.REVIEW_MIN_LESSONS and not force:
        return {"status": "not_enough_lessons", "have": len(lessons), "need": config.REVIEW_MIN_LESSONS, "proposals": [], "rejected": []}
    groups = {}
    for les in lessons:
        groups.setdefault(les["key"], {"lesson": les, "tasks": set()})["tasks"].add(les["task_id"])
    top = sorted(groups.values(), key=lambda g: -len(g["tasks"]))[: config.REVIEW_MAX_LESSONS]          # bounded input
    payload = {"lessons": [{"id": g["lesson"]["id"], "role": g["lesson"]["role"], "text": g["lesson"]["text"], "seen_in_tasks": len(g["tasks"])} for g in top],
               "prompts": {r: (repo() / ".claude" / "agents" / f"{r}.md").read_text(encoding="utf-8") for r in ("architect", "developer", "tester", "orchestrator")}}
    stamp = int(time.time())
    res = runner.run_agent("reviewer", payload, task_id=f"learning-{stamp}", run_id=f"review-{stamp}", backend=backend, env=env, settings_paths=settings_paths)
    if not res.ok:
        return {"status": "failed", "error": res.error, "proposals": [], "rejected": []}
    ids, rejected = [], []
    known = {les["id"] for les in lessons}
    for p in res.output["proposals"]:
        errs = proposal_errors(p, known)
        if errs:
            rejected.append({"target": p["target"], "reasons": errs})
            continue
        pid = _next_id()
        doc = dict(p, id=pid, created_at=logger.now())
        h = content_hash(doc)
        (_pdir() / f"{pid}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _state(pid, "proposed", content_hash=h)
        approvals.request(f"lesson-{pid}", "lesson", _preview(doc), diff_ref=f"sha256:{h}")
        ids.append(pid)
    return {"status": "ok", "proposals": ids, "rejected": rejected}


def _preview(doc):
    old = f"- {doc['old_text']}\n" if doc.get("old_text") else ""
    return f"{doc['id']} ({doc['kind']}) -> {doc['target']}\nWhy: {doc['rationale']}\n\n{old}+ {doc['new_text']}\n\nLessons: {', '.join(doc['source_lessons'])}"


def _state(pid, state, **extra):
    p = _pdir() / f"{pid}.state.json"
    cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    cur.update(state=state, updated_at=logger.now(), **extra)
    p.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    return cur


def load(pid):
    if not re.match(r"^P-\d+$", pid or ""):
        raise ValueError("bad proposal id")
    p = _pdir() / f"{pid}.json"
    if not p.exists():
        raise ValueError(f"no such proposal {pid}")
    st = _pdir() / f"{pid}.state.json"
    return json.loads(p.read_text(encoding="utf-8")), json.loads(st.read_text(encoding="utf-8")) if st.exists() else {}


# ------------------------------------------------------------------ apply (free checks only) and recovery
def _current_ref():
    ref = _must("rev-parse", "--abbrev-ref", "HEAD")
    return _must("rev-parse", "HEAD") if ref == "HEAD" else ref          # detached HEAD: remember the commit


def _cleanup(start_ref, branch):
    """Best effort, but REPORTED: returns a list of problems. Never claims success silently."""
    problems = []
    for args in (("checkout", "-q", "--", "."), ("clean", "-fdq", "--", "memory/patterns"), ("checkout", "-q", start_ref)):
        r = _git(*args)
        if r.returncode and args[0] == "checkout" and "--" not in args:
            problems.append(f"could not return to {start_ref}: {r.stderr.strip()[:120]}")
    if _branch_exists(branch):
        _git("branch", "-D", branch)
        if _branch_exists(branch):
            problems.append(f"branch {branch} still exists")
    return problems


def apply(pid, *, run_checks=True):
    doc, state = load(pid)
    if state.get("state") != "proposed":
        raise ValueError(f"{pid} is '{state.get('state')}', not 'proposed'")
    approval_id = f"lesson-{pid}-lesson-1"
    if approvals.status(approval_id) != "approved":
        raise PermissionError(f"{pid} is not approved by the user")
    req = approvals.get_request(approval_id) or {}
    if req.get("diff_ref") != f"sha256:{content_hash(doc)}":
        raise PermissionError(f"{pid} or its target file changed after the approval: review and approve again")
    if _git("status", "--porcelain").stdout.strip():
        raise RuntimeError("the repo has uncommitted changes: not touching it")
    errs = [e for e in proposal_errors(doc, {les["id"] for les in memory.inbox_lessons()}) if not e.startswith("unknown source lessons")]
    if errs:
        raise ValueError("proposal no longer applies: " + "; ".join(errs))
    start_ref = _current_ref()
    base_sha = _must("rev-parse", "HEAD")
    branch = f"agentsys/lesson-{pid}"
    _state(pid, "applying", start_ref=start_ref, branch=branch, base_sha=base_sha)      # journal first: recover() uses it after a crash
    try:
        _must("checkout", "-q", "-b", branch)
        path = _safe_path(doc["target"])
        if path is None:
            raise RuntimeError("target path is not safe")
        if doc.get("old_text"):
            path.write_text(path.read_text(encoding="utf-8").replace(doc["old_text"], doc["new_text"], 1), encoding="utf-8")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(doc["new_text"].rstrip() + "\n", encoding="utf-8")
        if run_checks:
            for name, cmd in (("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]),
                              ("eval selfcheck", [sys.executable, "evals/run_evals.py", "selfcheck"])):
                r = subprocess.run(cmd, cwd=repo(), capture_output=True, text=True, env={k: v for k, v in os.environ.items() if k != "ANTHROPIC_BASE_URL"})
                if r.returncode:
                    raise RuntimeError(f"{name} failed: {(r.stdout + r.stderr)[-300:]}")
        _must("add", "--", doc["target"])
        _must("commit", "-q", "-m", f"lesson {pid}: {doc['rationale'][:70]}")
        head_sha = _must("rev-parse", "HEAD")
        _must("checkout", "-q", start_ref)
    except BaseException as e:                                  # includes KeyboardInterrupt
        problems = _cleanup(start_ref, branch)
        _state(pid, "cleanup_failed" if problems else ("interrupted" if not isinstance(e, Exception) else "failed_checks"),
               error=str(e)[:300], cleanup_problems=problems)
        raise
    return _state(pid, "applied_pending_eval", branch=branch, base_sha=base_sha, head_sha=head_sha, start_ref=start_ref)


def recover(pid):
    """After an interrupted or half-cleaned apply: return to the start ref, delete the branch, make the proposal 'proposed' again."""
    doc, state = load(pid)
    if state.get("state") not in ("applying", "interrupted", "cleanup_failed", "failed_checks", "revert_failed"):
        raise ValueError(f"{pid} is '{state.get('state')}': nothing to recover")
    problems = _cleanup(state["start_ref"], state["branch"])
    if problems:
        return _state(pid, "cleanup_failed", cleanup_problems=problems)
    return _state(pid, "proposed", cleanup_problems=[], error=None)


# ------------------------------------------------------------------ eval gate
def compare_results(before: dict, after: dict) -> dict:
    b = {r["id"]: r for r in before["results"]}
    a = {r["id"]: r for r in after["results"]}
    regressions = [i for i in b if b[i]["passed"] and i in a and not a[i]["passed"]]
    missing = [i for i in b if i not in a]
    improved = [i for i in a if i in b and not b[i]["passed"] and a[i]["passed"]]
    tb = sum(r["tokens"] for r in b.values()) or 1
    ta = sum(r["tokens"] for r in a.values())
    return {"regressions": regressions, "improved": improved, "missing_in_after": missing,
            "pass_before": sum(r["passed"] for r in b.values()), "pass_after": sum(r["passed"] for r in a.values()),
            "tokens_before": tb, "tokens_after": ta, "token_change_pct": round(100 * (ta - tb) / tb, 2), "token_change_exact": 100 * (ta - tb) / tb,
            "rows": [{"id": i, "before": b[i]["passed"] if i in b else None, "after": a[i]["passed"] if i in a else None,
                      "fix_before": b[i].get("fix_loops") if i in b else None, "fix_after": a[i].get("fix_loops") if i in a else None} for i in sorted(set(a) | set(b))]}


def suite_ids():
    return sorted(p.name for p in (repo() / "evals" / "tasks").iterdir() if (p / "task.json").exists())


def run_errors(run: dict, name: str) -> list:
    errs = []
    rows = run.get("results") or []
    ids = [r.get("id") for r in rows]
    if not rows:
        errs.append(f"{name}: no results")
    if len(set(ids)) != len(ids):
        errs.append(f"{name}: duplicate task ids")
    if sorted(set(ids)) != suite_ids():
        errs.append(f"{name}: not the full suite (has {len(set(ids))} of {len(suite_ids())} tasks)")
    if any(r.get("status") == "skipped_budget" for r in rows):
        errs.append(f"{name}: some tasks were skipped by the token budget")
    for k in ("run_id", "git_sha", "label"):
        if not run.get(k):
            errs.append(f"{name}: missing {k}")
    if run.get("git_dirty"):
        errs.append(f"{name}: the working tree was dirty when the run started")
    if run.get("complete") is False:
        errs.append(f"{name}: the run did not finish")
    return errs


def gate(pid, before_path, after_path, *, max_token_growth_pct=25):
    """Keep the branch only if the two runs are valid and paired AND nothing regressed. Otherwise delete the branch."""
    doc, state = load(pid)
    if state.get("state") != "applied_pending_eval":
        raise ValueError(f"{pid} is '{state.get('state')}', not 'applied_pending_eval'")
    if Path(before_path).resolve() == Path(after_path).resolve():
        raise ValueError("before and after are the same file")
    before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (before_path, after_path))
    invalid = run_errors(before, "before") + run_errors(after, "after")
    if before.get("run_id") and before.get("run_id") == after.get("run_id"):
        invalid.append("before and after are the same run")
    branch = state["branch"]
    if not _branch_exists(branch):
        invalid.append(f"branch {branch} no longer exists")
    else:
        if before.get("git_sha") != state["base_sha"]:
            invalid.append(f"before run was not made on the base commit {state['base_sha'][:8]}")
        if after.get("git_sha") != _must("rev-parse", f"refs/heads/{branch}"):
            invalid.append(f"after run was not made on the head of {branch}: check it out first, then run the eval")
    if invalid:
        return _state(pid, "applied_pending_eval", gate_rejected_input=invalid)         # not a verdict: the proposal stays open
    cmp = compare_results(before, after)
    reasons = []
    if cmp["regressions"]:
        reasons.append(f"regressions: {cmp['regressions']}")
    if cmp["token_change_exact"] > max_token_growth_pct:
        reasons.append(f"tokens grew {cmp['token_change_pct']}% (limit {max_token_growth_pct}%)")
    if reasons:
        problems = _cleanup(state.get("start_ref") or "main", branch)
        if problems:
            _state(pid, "revert_failed", reasons=reasons, comparison=cmp, cleanup_problems=problems)
            raise RuntimeError("could not delete the branch: " + "; ".join(problems))
        return _state(pid, "reverted", reasons=reasons, comparison=cmp, gate_rejected_input=None)
    return _state(pid, "kept", comparison=cmp, gate_rejected_input=None, note="branch kept; open a PR and merge it yourself")
