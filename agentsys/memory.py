"""Shared memory: lessons. Deterministic code only, no LLM.

memory/inbox/<id>.json   one lesson per file (schema lesson), written after each task from FACTS in the task folder
memory/lessons.md        consolidated, deduplicated, at most LESSON_MAX_LINES lines
retrieve()               picks at most K relevant lines for one agent call (keyword overlap, no LLM)

Lesson text is built from TEMPLATES plus a sanitized short detail. Model-written text is never copied into prompts
unfiltered: lessons are an injection path into future prompts, so they are treated as hints, not instructions.
"""
import hashlib
import json
import re

from . import config, logger, schema

TEMPLATES = {
    "fix_loop": ("developer", "A fix loop was needed after a failed check. Re-read the criterion and run the check yourself before answering."),
    "replan": ("architect", "A design problem forced a re-plan. Check every interface against the acceptance criteria before you finish the plan."),
    "coverage_gap": ("tester", "A tester report missed constraint or criterion ids. List every input id in criterion_ids or constraints_na before answering."),
    "evidence_rejected": ("tester", "A tester report was rejected: evidence was not found in the tool log. Quote real output and the command you ran."),
    "schema_failure": ("all", "An agent reply failed validation. Reply with one valid JSON object that matches the example exactly."),
    "blocked": ("all", "A task stopped and needed a human. State the missing fact as a question instead of guessing."),
    "limit_hit": ("orchestrator", "A usage limit stopped a task. Keep plans small and avoid wide exploration."),
}
_SAFE = re.compile(r"[^A-Za-z0-9 .,_/:'-]")


def sanitize(text, n=60):
    return _SAFE.sub("", (text or "").replace("\n", " "))[:n].strip()


def _dirs():
    base = config.home() / "memory"
    inbox = base / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return base, inbox


def _lesson(task_id, kind, detail, evidence_ref=None):
    role, text = TEMPLATES[kind]
    detail = sanitize(detail)
    key = f"{kind}:{detail.lower()}" if detail else kind
    if detail:
        text = f"{text} ({detail})"
    return {"id": f"{task_id}-{hashlib.sha256(key.encode()).hexdigest()[:8]}", "task_id": task_id, "ts": logger.now(),
            "kind": kind, "role": role, "key": key, "text": text[:200], "evidence_ref": evidence_ref}


def extract_lessons(task_id):
    """Build lessons from facts in the task folder and log. Idempotent. Returns the lessons written."""
    from . import dashboard_data as dd
    tdir = config.home() / "tasks" / task_id
    _, inbox = _dirs()
    found = []
    budget = _read(tdir / "budget.json") or {}
    events, _ = dd.all_events(task_id)
    reports = sorted((tdir / "reports").glob("test_report-*.json")) if (tdir / "reports").is_dir() else []
    failed_names = [c["name"] for p in reports if (r := _read(p)) and r.get("verdict") == "fail" for c in r["checks"] if c["result"] == "fail"]
    if budget.get("fix_loops", 0) > 0:
        found.append(_lesson(task_id, "fix_loop", failed_names[0] if failed_names else ""))
    if budget.get("replans", 0) > 0:
        found.append(_lesson(task_id, "replan", ""))
    if budget.get("limit_hits"):
        found.append(_lesson(task_id, "limit_hit", ""))
    for e in events:
        s, ref = e["summary"], e.get("payload_ref")
        if "tester report incomplete" in s:
            found.append(_lesson(task_id, "coverage_gap", "", ref))
        elif e["event_type"] == "error" and "semantic checks" in s and "tool log" in json.dumps(_payload(ref)):
            found.append(_lesson(task_id, "evidence_rejected", "", ref))
        elif e["event_type"] == "error" and "failed schema" in s:
            found.append(_lesson(task_id, "schema_failure", e["agent"], ref))
    state = _read(tdir / "state.json") or {}
    if state.get("state") in ("blocked", "failed"):
        found.append(_lesson(task_id, "blocked", ""))
    written = []
    for les in found:
        path = inbox / f"{les['id']}.json"
        if path.exists():
            continue
        schema.validate("lesson", les)
        path.write_text(json.dumps(les, indent=2), encoding="utf-8")
        written.append(les)
    if written:
        consolidate()
    return written


def _read(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _payload(ref):
    return (_read(config.home() / ref) if ref else None) or {}


def inbox_lessons():
    _, inbox = _dirs()
    out = []
    for p in sorted(inbox.glob("*.json")):
        les = _read(p)
        if les and not schema.errors_for("lesson", les):
            out.append(les)
    return out


def consolidate():
    """Group inbox lessons by key, count them, write a short lessons.md."""
    groups = {}
    for les in inbox_lessons():
        g = groups.setdefault(les["key"], {"lesson": les, "tasks": set()})
        g["tasks"].add(les["task_id"])
    rows = sorted(groups.values(), key=lambda g: (-len(g["tasks"]), g["lesson"]["key"]))[: config.LESSON_MAX_LINES]
    base, _ = _dirs()
    lines = ["# Lessons (generated, deduplicated; do not edit by hand)", ""]
    lines += [f"- [{g['lesson']['role']}] {g['lesson']['text']} (seen in {len(g['tasks'])} task(s))" for g in rows]
    (base / "lessons.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


_LINE = re.compile(r"^- \[(\w+)\] (.*?) \(seen in (\d+) task\(s\)\)$")


def _tokens(text):
    return {t for t in re.findall(r"[a-z0-9]{4,}", (text or "").lower())}


def retrieve(role, text, k=None):
    """At most K lessons for this role (or 'all'), best keyword overlap first. Returns plain strings <= 200 chars."""
    k = config.LESSON_RETRIEVE_K if k is None else k
    p = config.home() / "memory" / "lessons.md"
    if not p.exists():
        return []
    want = _tokens(text)
    scored = []
    for line in p.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line)
        if not m or m.group(1) not in (role, "all"):
            continue
        scored.append((len(want & _tokens(m.group(2))) + int(m.group(3)) * 0.1 + (0.5 if m.group(1) == role else 0), m.group(2)))
    scored.sort(key=lambda x: -x[0])
    return [t[:200] for _, t in scored[:k]]
