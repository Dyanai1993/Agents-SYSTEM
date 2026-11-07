"""Read-only data layer for the dashboard. Never calls an LLM. Tolerates bad lines."""
import json
from pathlib import Path

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


ACTIVE_STATES = {"intake", "plan", "build", "test", "fix", "security"}
# Longer than any single agent call may take (the longest role limit) plus a margin: past this, an "active"
# task with no new event is not working - its run most likely stopped (crashed, killed, laptop asleep).
STUCK_MIN = max(lim["timeout_s"] for lim in config.ROLE_LIMITS.values()) // 60 + 10


def _epoch(ts):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _attention(task_id, d, st, idle_min):
    """What needs a human for this task, as (level, text), or None. Read-only: files only, no lock probing."""
    entry = _json(config.home() / "queue" / f"{task_id}.json") or {}
    state, status, note = st.get("state"), entry.get("status"), (entry.get("note") or "")[:300]
    if (d / "workspace_tainted.json").exists():
        return "bad", "an agent's changes could NOT be reverted: restore the workspace before anything else runs (see the task's alert)"
    if st.get("waiting_approval"):
        return "warn", "waiting for your approval"
    if status in ("needs_input", "needs_approval"):
        return "warn", f"{status}: {note}" if note else status
    if state in ("blocked", "failed"):
        return "bad", note or "blocked: see status_report.md in the task folder"
    if state == "paused":
        when = entry.get("resume_after")
        return "warn", "paused" + (f" until about {_hhmm(when)}" if when else "") + (f": {note}" if note else "")
    if state in ACTIVE_STATES and idle_min is not None and idle_min > STUCK_MIN:
        return "bad", (f"stuck? no activity for {int(idle_min)} min in '{state}' - the run probably stopped. "
                       f"`python scripts/orch.py run-next` resumes it from its checkpoint")
    return None


def _hhmm(epoch):
    import time
    return time.strftime("%H:%M", time.localtime(epoch))


def _last_events(events):
    last = {}
    for e in events:
        last[e.get("task_id")] = e.get("ts")
    return last


def overview(now=None):
    import time
    now = time.time() if now is None else now
    tasks_dir = config.home() / "tasks"
    events, bad = all_events()
    last = _last_events(events)
    rows = []
    if tasks_dir.is_dir():
        for d in sorted(tasks_dir.iterdir()):
            if not d.is_dir() or not logger.TASK_ID_RE.match(d.name):
                continue
            st, bud = _json(d / "state.json") or {}, _json(d / "budget.json") or {}
            t = _epoch(last.get(d.name) or st.get("updated_at"))
            idle = None if t is None else max(0.0, (now - t) / 60)
            att = _attention(d.name, d, st, idle)
            rows.append({
                "task_id": d.name, "title": st.get("title", d.name), "state": st.get("state", "unknown"),
                "current_agent": st.get("current_agent"), "waiting_approval": st.get("waiting_approval", False),
                "updated_at": st.get("updated_at"), "created_at": st.get("created_at"),
                "calls": bud.get("calls", 0), "call_cap": bud.get("call_cap"),
                "fix_loops": bud.get("fix_loops", 0), "fix_cap": bud.get("fix_cap"),
                "tokens_est": bud.get("tokens_est", 0), "limit_hits": len(bud.get("limit_hits", [])),
                "last_event": last.get(d.name), "idle_min": None if idle is None else round(idle, 1),
                "attention": None if att is None else {"level": att[0], "text": att[1]},
            })
    alerts = [e for e in events if e["event_type"] in ("alert", "limit_hit", "error") or e["status"] == "blocked"][-20:]
    return {"tasks": rows, "window": Window().used(), "alerts": alerts, "bad_lines": bad, "stuck_min": STUCK_MIN}


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


MAIN_STATES = ["intake", "plan", "build", "test", "fix", "security", "review", "done"]


def work_items(plan, prog, state):
    """Each work item with its dependencies and where it stands, then the steps after the plan."""
    from .loop import _item_hash                   # the loop's own notion of 'done' (a re-planned item is not done)
    if not isinstance(plan, dict) or plan.get("status") != "ok":
        return {"items": [], "steps": []}
    prog = prog or {}
    completed = prog.get("completed") or {}
    done = {w["id"] for w in plan["work_items"] if completed.get(w["id"]) == _item_hash(w)}
    stopped = state in ("blocked", "failed", "paused")
    cur = next((w["id"] for w in plan["work_items"] if w["id"] not in done), None)
    items = []
    for w in plan["work_items"]:
        waiting = [dep for dep in w["depends_on"] if dep not in done]
        if w["id"] in done:
            status = "done"
        elif w["id"] == cur:
            step = {"dev": "fix " + str(prog.get("fix")) if prog.get("fix") else "build", "test": "test"}.get(prog.get("step"), prog.get("step"))
            status = ("stopped at " if stopped else "in progress: ") + str(step)
        elif waiting:
            status = "waiting for " + ", ".join(waiting)
        else:
            status = "pending"
        items.append({"id": w["id"], "title": w["title"], "depends_on": w["depends_on"], "files": w.get("files", []),
                      "criteria": len(w.get("acceptance_criteria", [])), "status": status})
    all_done = len(done) == len(plan["work_items"])
    steps = [{"id": "FINAL", "title": "final regression (all original criteria)",
              "status": "done" if prog.get("final_done") else ("next" if all_done else "after all items")},
             {"id": "SECURITY", "title": "security review",
              "status": "done" if prog.get("security_done") else ("next" if prog.get("final_done") else "after the final regression")}]
    return {"items": items, "steps": steps}


def _costs(events, budget):
    """Tokens per agent and per model, from the task's own log (each call's tokens are logged exactly once,
    on agent_end or on its error event). budget.json only counts CALLS per agent/model."""
    by_agent, by_model = {}, {}
    for e in events:
        n = e.get("tokens_estimate") or 0
        if n and e.get("event_type") in ("agent_end", "error"):
            by_agent[e.get("agent")] = by_agent.get(e.get("agent"), 0) + n
            if e.get("model"):
                by_model[e["model"]] = by_model.get(e["model"], 0) + n
    return {"tokens_by_agent": by_agent, "tokens_by_model": by_model, "calls_by_agent": budget.get("by_agent", {}),
            "tokens_total": budget.get("tokens_est", 0), "token_cap": budget.get("token_cap"),
            "agent_seconds": budget.get("active_s", 0)}


def task_detail(task_id, now=None):
    import time
    if not logger.TASK_ID_RE.match(task_id or ""):
        return None
    d = config.home() / "tasks" / task_id
    if not d.is_dir():
        return None
    events, bad = all_events(task_id)
    st, bud, prog, plan = _json(d / "state.json") or {}, _json(d / "budget.json") or {}, _json(d / "progress.json") or {}, _json(d / "plan.json")
    t = _epoch(events[-1]["ts"] if events else st.get("updated_at"))
    idle = None if t is None else max(0.0, ((time.time() if now is None else now) - t) / 60)
    att = _attention(task_id, d, st, idle)
    return {"task_id": task_id, "state": st, "budget": bud, "progress": prog, "plan": plan,
            "events": events, "bad_lines": bad, "states": MAIN_STATES,
            "has_status_report": (d / "status_report.md").exists(),
            "work": work_items(plan, prog, st.get("state")), "costs": _costs(events, bud),
            "security": _json(d / "security_report.json"), "tainted": _json(d / "workspace_tainted.json"),
            "idle_min": None if idle is None else round(idle, 1),
            "attention": None if att is None else {"level": att[0], "text": att[1]}}


def quality():
    out = []
    tdir = config.home() / "tasks"
    if not tdir.is_dir():
        return {"tasks": out}
    for d in sorted(tdir.iterdir()):
        if not d.is_dir() or not logger.TASK_ID_RE.match(d.name):
            continue
        reports = []
        for p in sorted((d / "reports").glob("test_report-*.json")) if (d / "reports").is_dir() else []:
            r = _json(p)
            if not isinstance(r, dict):
                continue
            checks = r.get("checks", [])
            reports.append({"file": p.name, "item": p.stem.split("-", 2)[-1], "verdict": r.get("verdict"),
                            "summary": r.get("summary"), "design_problem": bool(r.get("design_problem")),
                            "pass": sum(c.get("result") == "pass" for c in checks),
                            "fail": sum(c.get("result") == "fail" for c in checks),
                            "skipped": sum(c.get("result") == "skipped" for c in checks), "checks": checks})
        bud = _json(d / "budget.json") or {}
        out.append({"task_id": d.name, "reports": reports, "fix_loops": bud.get("fix_loops", 0), "fix_cap": bud.get("fix_cap"),
                    "replans": bud.get("replans", 0)})
    return {"tasks": out}


def approvals_view():
    from . import approvals
    pend = []
    for a in approvals.pending():
        st = _json(config.home() / "tasks" / a["task_id"] / "state.json") or {}
        pend.append(dict(a, task_title=st.get("title", a["task_id"]), task_state=st.get("state")))
    return {"pending": pend, "history": approvals.history()[-30:][::-1]}


def usage_view():
    import json as _j
    win = Window()
    rows = win._load()
    by_agent, by_model = {}, {}
    for r in rows:
        by_agent[r["agent"]] = by_agent.get(r["agent"], 0) + r["tokens"]
        by_model[r["model"]] = by_model.get(r["model"], 0) + r["tokens"]
    tasks_out, hits = [], []
    tdir = config.home() / "tasks"
    for d in sorted(tdir.iterdir()) if tdir.is_dir() else []:
        b = _json(d / "budget.json")
        if not b:
            continue
        tasks_out.append({k: b.get(k) for k in ("task_id", "calls", "call_cap", "fix_loops", "fix_cap", "replans", "replan_cap", "tokens_est", "by_model", "by_agent")})
        hits += [dict(h, task_id=d.name) for h in b.get("limit_hits", [])]
    return {"window": win.used(), "tasks": tasks_out, "by_agent_7d": by_agent, "by_model_7d": by_model,
            "limit_hits": hits[-20:], "note": "Window sizes are estimates, not official numbers."}


def alerts_view():
    from . import approvals
    items = []
    ov = overview()
    for t in ov["tasks"]:
        if t["attention"] and not t["waiting_approval"]:     # waiting approvals are listed below, once each
            items.append({"level": t["attention"]["level"], "task_id": t["task_id"], "text": t["attention"]["text"]})
        if t["call_cap"] and t["calls"] >= 0.8 * t["call_cap"]:
            items.append({"level": "warn", "task_id": t["task_id"], "text": f"agent calls {t['calls']}/{t['call_cap']}"})
        if t["fix_cap"] and t["fix_loops"] >= t["fix_cap"]:
            items.append({"level": "bad", "task_id": t["task_id"], "text": "fix-loop cap reached"})
    w = ov["window"]
    if w["share_5h"] >= 0.7:
        items.append({"level": "warn", "task_id": None, "text": f"5-hour window about {int(w['share_5h'] * 100)}% used (estimate)"})
    for a in approvals.pending():
        items.append({"level": "warn", "task_id": a["task_id"], "text": f"pending approval: {a['gate']}"})
    for e in ov["alerts"]:
        items.append({"level": "bad" if e["status"] in ("blocked", "error") else "warn", "task_id": e["task_id"],
                      "text": f"{e['ts'][11:19]} {e['agent']}: {e['summary']}"})
    return {"items": items}


def learning_view():
    from . import approvals, memory
    base = config.home() / "memory"
    props = []
    pdir = base / "proposals"
    for p in (sorted(pdir.glob("P-*.json")) if pdir.is_dir() else []):
        if p.name.endswith(".state.json"):
            continue
        doc = _json(p)
        if not doc:
            continue
        st = _json(pdir / f"{doc['id']}.state.json") or {}
        props.append(dict(doc, state=st.get("state"), branch=st.get("branch"), reasons=st.get("reasons"), error=st.get("error"),
                          comparison=st.get("comparison"), approval=approvals.status(f"lesson-{doc['id']}-lesson-1")))
    results = []
    rdir = Path(__file__).resolve().parent.parent / "evals" / "results"
    files = sorted(rdir.glob("real-*.json"), key=lambda f: f.stat().st_mtime)[-6:] if rdir.is_dir() else []
    for f in files:
        r = _json(f)
        if r:
            rows = r["results"]
            results.append({"file": f.name, "label": r.get("label"), "ts": r.get("ts"), "passed": sum(x["passed"] for x in rows),
                            "total": len(rows), "tokens": sum(x["tokens"] for x in rows), "fix_loops": sum(x.get("fix_loops", 0) for x in rows), "rows": rows})
    comparison = None
    if len(results) >= 2 and sorted(x["id"] for x in results[-2]["rows"]) == sorted(x["id"] for x in results[-1]["rows"]) and results[-2]["label"] != results[-1]["label"]:
        from . import learning
        a, b = _json(files[-2]), _json(files[-1])
        comparison = dict(learning.compare_results(a, b), before=results[-2]["label"], after=results[-1]["label"])
    lessons_md = base / "lessons.md"
    return {"inbox": len(memory.inbox_lessons()), "need_for_review": config.REVIEW_MIN_LESSONS,
            "lessons": lessons_md.read_text(encoding="utf-8").splitlines()[2:] if lessons_md.exists() else [],
            "proposals": props[::-1], "eval_runs": results[::-1], "comparison": comparison}
