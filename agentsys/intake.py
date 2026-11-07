"""Intake: plain-language request -> a requirement DRAFT (Haiku, 1 call).

The draft is never built from directly: it waits at the "requirements" approval gate until the human approves it
(or rejects it). To change it from the CLI, `orch.py retry ID --answer "..."` adds the answer to the draft, which
then needs a fresh approval. The conversational way to write a requirement is the Requirements Assistant.
"""
import hashlib
import json

from . import config, runner


def _tdir(task_id):
    return config.home() / "tasks" / task_id


def draft(task_id):
    p = _tdir(task_id) / "requirements.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_draft(task_id, req):
    _tdir(task_id).mkdir(parents=True, exist_ok=True)
    (_tdir(task_id) / "requirements.json").write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")


def draft_ref(task_id):
    """What a requirement approval covers: this exact draft. Any change to it needs a fresh approval."""
    p = _tdir(task_id) / "requirements.json"
    return "draft:" + (hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p.exists() else "none")


def blocking_questions(req):
    return [u["question"] for u in (req or {}).get("unknowns", []) if u.get("blocking")]


def gate_context(task_id, req):
    """The approval text: the draft in plain words. Also readable from `orch.py pending`."""
    def block(title, rows):
        return f"{title}:\n" + ("\n".join(f"- {r}" for r in rows) if rows else "- (none)")
    qs = [("[BLOCKING] " if u["blocking"] else "") + u["question"] for u in req["unknowns"]]
    return (f"Requirement draft - nothing is built until you approve it.\n\nGoal: {req['goal']}\n\n"
            + block("Constraints", req["constraints"]) + "\n\n" + block("Acceptance criteria", req["acceptance_criteria"]) + "\n\n"
            + block("Assumptions the intake made", req["risks"]) + "\n\n" + block("Open questions", qs)
            + f"\n\nTo change it: `python scripts/orch.py retry {task_id} --answer \"...\"` (the draft then needs approval again)."
            + (" Answer the BLOCKING question(s) first." if blocking_questions(req) else ""))


def run_intake(task_id, request_text, *, backend, env=None, settings_paths=None):
    """Return (draft, error). error is None on success."""
    res = runner.run_agent("orchestrator", {"request": request_text}, task_id=task_id, run_id="intake", backend=backend,
                           env=env, settings_paths=settings_paths)
    if not res.ok:
        return None, res.error
    save_draft(task_id, res.output)
    return res.output, None
