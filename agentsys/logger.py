"""Structured event log. One JSON line per event in logs/<task_id>.jsonl.

Large payloads go to tasks/<task_id>/payloads/*.json and the line keeps payload_ref only.
"""
import itertools
import json
import re
from datetime import datetime, timezone

from . import config, schema

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_counter = itertools.count()


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def safe_task_id(task_id: str) -> str:
    if not TASK_ID_RE.match(task_id or "") or task_id in (".", ".."):
        raise ValueError(f"bad task id: {task_id!r}")
    return task_id


def write_payload(task_id: str, payload) -> str:
    safe_task_id(task_id)
    rel = f"tasks/{task_id}/payloads/{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-{next(_counter)}.json"
    path = config.home() / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return rel


def log_event(task_id, agent, event_type, summary, *, run_id="r0", model="none", from_=None, to=None,
              payload=None, tokens_estimate=0, status="ok") -> dict:
    safe_task_id(task_id)
    ref = write_payload(task_id, payload) if payload is not None else None
    event = {
        "ts": now(), "task_id": task_id, "run_id": run_id, "agent": agent, "model": model,
        "event_type": event_type, "from": from_, "to": to, "summary": str(summary)[:300],
        "payload_ref": ref, "tokens_estimate": int(tokens_estimate), "status": status,
    }
    schema.validate("event", event)  # never write a bad line: the dashboard relies on it
    path = config.home() / "logs" / f"{task_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event
