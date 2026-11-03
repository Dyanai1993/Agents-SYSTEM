"""Pause / stop control files. Written by the dashboard or CLI, read by the loop before every agent call. No LLM."""
import json

from . import config, logger, schema


def path(task_id):
    return config.home() / "tasks" / logger.safe_task_id(task_id) / "control.json"


def set_control(task_id, action):
    doc = {"task_id": task_id, "action": action, "requested_at": logger.now()}
    schema.validate("control", doc)
    p = path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    logger.log_event(task_id, "dashboard", "decision", f"control: {action}", status="ok", payload=doc)
    return doc


def read(task_id):
    """Return 'pause', 'stop' or None. 'resume' (or no file) means run."""
    try:
        doc = json.loads(path(task_id).read_text(encoding="utf-8"))
        return doc["action"] if doc.get("action") in ("pause", "stop") else None
    except (OSError, ValueError, KeyError):
        return None
