"""Read-only data layer for the dashboard. Never calls an LLM. Tolerates bad lines."""
import json

from . import config, logger
from .ledger import Window


def _read_jsonl(path):
    good, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if isinstance(e, dict) and "ts" in e and "summary" in e:
                        good.append(e)
                    else:
                        bad += 1
                except ValueError:
                    bad += 1
    except OSError:
        pass
    return good, bad


def _json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def all_events(task=None):
    logs = config.home() / "logs"
    events, bad = [], 0
    for p in sorted(logs.glob("*.jsonl")):
        if task and p.stem != task:
            continue
        g, b = _read_jsonl(p)
        events += g
        bad += b
    events.sort(key=lambda e: e.get("ts", ""))
    return events, bad


def overview():
    tasks_dir = config.home() / "tasks"
    rows = []
    if tasks_dir.is_dir():
        for d in sorted(tasks_dir.iterdir()):
            if not d.is_dir() or not logger.TASK_ID_RE.match(d.name):
                continue
            st, bud = _json(d / "state.json") or {}, _json(d / "budget.json") or {}
            rows.append({
                "task_id": d.name, "title": st.get("title", d.name), "state": st.get("state", "unknown"),
                "current_agent": st.get("current_agent"), "waiting_approval": st.get("waiting_approval", False),
                "updated_at": st.get("updated_at"), "created_at": st.get("created_at"),
                "calls": bud.get("calls", 0), "call_cap": bud.get("call_cap"),
                "fix_loops": bud.get("fix_loops", 0), "fix_cap": bud.get("fix_cap"),
                "tokens_est": bud.get("tokens_est", 0), "limit_hits": len(bud.get("limit_hits", [])),
            })
    events, bad = all_events()
    alerts = [e for e in events if e["event_type"] in ("alert", "limit_hit", "error") or e["status"] == "blocked"][-20:]
    return {"tasks": rows, "window": Window().used(), "alerts": alerts, "bad_lines": bad}


def query_events(task=None, agent=None, event_type=None, q=None, limit=300):
    events, bad = all_events(task)
    if agent:
        events = [e for e in events if e.get("agent") == agent]
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]
    if q:
        ql = q.lower()
        events = [e for e in events if ql in json.dumps(e).lower()]
    return {"events": events[-limit:], "bad_lines": bad}


def messages(task=None, limit=300):
    r = query_events(task=task, event_type="message", limit=limit)
    return r


def payload(ref):
    """Serve a payload only if it lives inside tasks/*/payloads/."""
    parts = ref.split("/")
    if len(parts) != 4 or parts[0] != "tasks" or parts[2] != "payloads" or not logger.TASK_ID_RE.match(parts[1]) \
            or not parts[3].endswith(".json") or ".." in ref:
        return None
    return _json(config.home() / ref)
