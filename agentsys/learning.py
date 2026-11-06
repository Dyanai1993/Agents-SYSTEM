"""Controlled improvement loop. Nothing changes an agent prompt without a human decision AND a green gate.

review()   Haiku clusters lessons -> proposals (files) + one approval request each (gate 'lesson')
apply()    only when approved: new git branch `agentsys/lesson-<id>`, exact text edit, free checks (unit tests + eval selfcheck)
gate()     compares a BEFORE and an AFTER real eval run: any regression -> the branch is deleted (reverted); else it is kept
Nothing here merges into main or pushes.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import approvals, config, logger, memory, prompts, runner, schema

TARGET_RE = re.compile(r"^(\.claude/agents/(architect|developer|tester|orchestrator)\.md|memory/patterns/[A-Za-z0-9_-]{1,40}\.md)$")
PROTECTED = ("DATA", "billing", "approval", "ONE valid JSON object", "limit")


def repo() -> Path:
    return Path(os.environ.get("AGENTSYS_REPO") or Path(__file__).resolve().parent.parent)


def _pdir():
    d = config.home() / "memory" / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _git(*args, cwd=None):
    return subprocess.run(["git", "-c", "user.name=agentsys", "-c", "user.email=agentsys@localhost", *args],
                          cwd=cwd or repo(), capture_output=True, text=True)


# ------------------------------------------------------------------ proposals
def proposal_errors(p, lesson_ids) -> list:
    errs = []
    if not TARGET_RE.match(p["target"]):
        errs.append(f"target not allowed: {p['target']}")
        return errs
    path = repo() / p["target"]
    old = p.get("old_text")
    if old:
        if not path.is_file():
            errs.append("target file does not exist")
        elif path.read_text(encoding="utf-8").count(old) != 1:
            errs.append("old_text must occur exactly once in the target")
        else:
            removed = [w for w in PROTECTED if w in old and w not in p["new_text"]]
            if removed:
                errs.append(f"change would remove protected wording: {removed}")
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
    """Return {'status': ..., 'proposals': [ids], 'rejected': [reasons]}."""
    lessons = memory.inbox_lessons()
    if len(lessons) < config.REVIEW_MIN_LESSONS and not force:
        return {"status": "not_enough_lessons", "have": len(lessons), "need": config.REVIEW_MIN_LESSONS, "proposals": [], "rejected": []}
    counts = {}
    for les in lessons:
        counts.setdefault(les["key"], {"lesson": les, "tasks": set()})["tasks"].add(les["task_id"])
    payload = {"lessons": [{"id": g["lesson"]["id"], "role": g["lesson"]["role"], "text": g["lesson"]["text"], "key": g["lesson"]["key"],
                            "seen_in_tasks": len(g["tasks"])} for g in counts.values()],
               "prompts": {r: (repo() / ".claude" / "agents" / f"{r}.md").read_text(encoding="utf-8") for r in ("architect", "developer", "tester", "orchestrator")}}
    res = runner.run_agent("reviewer", payload, task_id="_learning", run_id="review", backend=backend, env=env, settings_paths=settings_paths)
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
        (_pdir() / f"{pid}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
        _state(pid, "proposed")
        approvals.request(f"lesson-{pid}", "lesson", _preview(doc))
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


# ------------------------------------------------------------------ apply (free checks only)
def apply(pid, *, run_checks=True):
    doc, state = load(pid)
    if state.get("state") != "proposed":
        raise ValueError(f"{pid} is '{state.get('state')}', not 'proposed'")
    if approvals.status(f"lesson-{pid}-lesson-1") != "approved":
        raise PermissionError(f"{pid} is not approved by the user")
    if _git("status", "--porcelain").stdout.strip():
        raise RuntimeError("the repo has uncommitted changes: not touching it")
    errs = proposal_errors(doc, {les["id"] for les in memory.inbox_lessons()})
    errs = [e for e in errs if not e.startswith("unknown source lessons")]       # lessons may have been consolidated since
    if errs:
        raise ValueError("proposal no longer applies: " + "; ".join(errs))
    start = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    branch = f"agentsys/lesson-{pid}"
    if _git("checkout", "-q", "-b", branch).returncode:
        raise RuntimeError(f"cannot create branch {branch}")
    path = repo() / doc["target"]
    try:
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
        _git("add", "--", doc["target"])
        _git("commit", "-q", "-m", f"lesson {pid}: {doc['rationale'][:70]}")
    except Exception as e:
        _git("checkout", "-q", "--", ".")
        _git("clean", "-fdq", "--", "memory/patterns")
        _git("checkout", "-q", start)
        _git("branch", "-D", branch)
        _state(pid, "failed_checks", error=str(e)[:300])
        raise
    _git("checkout", "-q", start)
    return _state(pid, "applied_pending_eval", branch=branch, base=start)


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
            "tokens_before": tb, "tokens_after": ta, "token_change_pct": round(100 * (ta - tb) / tb, 1),
            "rows": [{"id": i, "before": b[i]["passed"] if i in b else None, "after": a[i]["passed"] if i in a else None,
                      "fix_before": b[i].get("fix_loops") if i in b else None, "fix_after": a[i].get("fix_loops") if i in a else None} for i in sorted(set(a) | set(b))]}


def gate(pid, before_path, after_path, *, max_token_growth_pct=25):
    """Keep the branch only if nothing regressed and tokens did not grow past the limit. Otherwise delete the branch."""
    doc, state = load(pid)
    if state.get("state") != "applied_pending_eval":
        raise ValueError(f"{pid} is '{state.get('state')}', not 'applied_pending_eval'")
    before, after = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (before_path, after_path))
    cmp = compare_results(before, after)
    reasons = []
    if cmp["regressions"]:
        reasons.append(f"regressions: {cmp['regressions']}")
    if cmp["missing_in_after"]:
        reasons.append(f"tasks missing in the after run: {cmp['missing_in_after']}")
    if cmp["token_change_pct"] > max_token_growth_pct:
        reasons.append(f"tokens grew {cmp['token_change_pct']}% (limit {max_token_growth_pct}%)")
    if reasons:
        branch = state["branch"]
        if _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch:
            _git("checkout", "-q", state.get("base", "main"))
        _git("branch", "-D", branch)
        return _state(pid, "reverted", reasons=reasons, comparison=cmp)
    return _state(pid, "kept", comparison=cmp, note="branch kept; open a PR and merge it yourself")
