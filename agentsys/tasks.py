"""Task folder helpers. Phase 1 has only state creation; the state machine arrives in Phase 3."""
import json

from . import config, logger, schema


def set_state(task_id, state, *, title=None, current_agent=None, waiting_approval=False):
    logger.safe_task_id(task_id)
    path = config.home() / "tasks" / task_id / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    doc = {"task_id": task_id, "title": title or old.get("title", task_id), "state": state,
           "current_agent": current_agent, "created_at": old.get("created_at", logger.now()),
           "updated_at": logger.now(), "waiting_approval": bool(waiting_approval)}
    schema.validate("task_state", doc)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)
    logger.log_event(task_id, "orchestrator", "state_change", f"state -> {state}",
                     from_=old.get("state"), to=state, status="pending" if waiting_approval else "ok")
    return doc


def short_title(text, n=60):
    """First line, cut at a word boundary, never in the middle of a word."""
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0] or text[:n]
    return cut.rstrip(" ,.;:-") + "\u2026"
