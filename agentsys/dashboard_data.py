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


def _live_tasks():
    """Task ids a LIVE process (worker or run-next, fresh heartbeat) is working on right now."""
    from . import worker
    try:
        return {p["job"]["id"] for p in worker.status()["processes"]
                if p["alive"] and p.get("job") and p["job"].get("kind") == "task"}
    except Exception:          # the dashboard must never crash on bad data
        return set()


def _attention(task_id, d, st, idle_min, live=None):
    """What needs a human for this task, as (level, text), or None. Read-only: files only, no lock probing.
    `live`: task ids a process with a fresh heartbeat works on - the truth about "running", instead of guessing
    "stuck?" after 30 quiet minutes (a 20-minute agent call is quiet too)."""
    from . import worker
    live = _live_tasks() if live is None else live
    entry = _json(config.home() / "queue" / f"{task_id}.json") or {}
    state, status, note = st.get("state"), entry.get("status"), (entry.get("note") or "")[:300]
    if (d / "workspace_tainted.json").exists():
        return "bad", "an agent's changes could NOT be reverted: restore the workspace before anything else runs (see the task's alert)"
    ctl = _json(d / "control.json") or {}
    if task_id in live:                                # running: only a pending pause/stop needs saying
        if ctl.get("action") in ("pause", "stop"):
            return "warn", f"{ctl['action']} requested: it takes effect before the next agent call"
        return None
    req = worker.get_request(task_id) if logger.TASK_ID_RE.match(task_id) else None
    if req and req.get("status") == "refused":
        return "bad", f"your {req.get('action', 'start')} request was refused: {req.get('note')}"
    if req and req.get("status") in ("waiting", "running"):
        return "warn", f"{req.get('action', 'start')} requested: waiting for the worker" + (f" ({req['note']})" if req.get("note") else "")
    req_phase = entry.get("requirements") is None and bool(entry.get("request_text"))
    if status == "waiting_approval":
        from . import approvals
        decided = approvals.latest(task_id, "requirements" if req_phase else "merge")[1]
        if decided == "approved":                  # decided, but nothing runs until the user presses Continue
            return "warn", ("requirement approved: press Continue to plan and build it" if req_phase
                            else "merge approved: press Continue to deliver the patch files")
    if req_phase and status == "waiting_approval":
        return "warn", "review the requirement draft: approve or reject it" + (
            " - it has a BLOCKING question" if "blocking question" in note else "")
    if status == "queued":
        started = (d / "progress.json").exists()          # the same rule as the Run card's Start / Continue button
        return "warn", ("not running: press Continue to resume from its last checkpoint" if started else "queued: press Start")
    if status == "stopped":
        return "warn", "stopped: press Resume to continue from the last checkpoint"
    if st.get("waiting_approval") or status == "waiting_approval":
        return "warn", "waiting for your approval (Approvals tab)"
    if status == "needs_input":
        return "warn", "a question for you - answer it in the Task monitor" + (f": {note}" if note else "")
    if status == "needs_approval":
        return "warn", f"needs your approval: {note}" if note else "needs your approval (Approvals tab)"
    if (state in ("blocked", "failed") or status == "blocked") and "not covered by any work item" in note:
        return "bad", ("the plan cannot cover all acceptance criteria in one task - a Retry fails the same way. "
                       "Narrow the requirement (Task monitor) to what fits, and put the rest in new tasks")
    if state in ("blocked", "failed") or status == "blocked":
        return "bad", (note or "blocked: see status_report.md in the task folder") + " - fix the cause, then press Retry"
    if state == "paused" or status == "paused":
        when = entry.get("resume_after")
        return "warn", "paused" + (f" until about {_hhmm(when)}" if when else "") + (f": {note}" if note else "")
    if state in ACTIVE_STATES and status not in ("done", "rejected"):
        quiet = f" (last activity {int(idle_min)} min ago)" if idle_min is not None else ""
        return "bad", f"not running: no worker or run-next is working on it{quiet}. Press Continue to resume from its checkpoint"
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
    live = _live_tasks()
    if tasks_dir.is_dir():
        for d in sorted(tasks_dir.iterdir()):
            # a task has a state.json; an assistant session (D-####) or a smoke run only keeps a budget/log there
            if not d.is_dir() or not logger.TASK_ID_RE.match(d.name) or not (d / "state.json").exists():
                continue
            st, bud = _json(d / "state.json") or {}, _json(d / "budget.json") or {}
            t = _epoch(last.get(d.name) or st.get("updated_at"))
            idle = None if t is None else max(0.0, (now - t) / 60)
            att = _attention(d.name, d, st, idle, live)
            entry = _json(config.home() / "queue" / f"{d.name}.json") or {}
            rows.append({
                "task_id": d.name, "title": st.get("title", d.name), "state": st.get("state", "unknown"),
                "status": entry.get("status"), "running": d.name in live,
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
    live = _live_tasks()
    att = _attention(task_id, d, st, idle, live)
    return {"task_id": task_id, "state": st, "budget": bud, "progress": prog, "plan": plan,
            "events": events, "bad_lines": bad, "states": MAIN_STATES,
            "has_status_report": (d / "status_report.md").exists(),
            "work": work_items(plan, prog, st.get("state")), "costs": _costs(events, bud),
            "security": _json(d / "security_report.json"), "tainted": _json(d / "workspace_tainted.json"),
            "idle_min": None if idle is None else round(idle, 1),
            "attention": None if att is None else {"level": att[0], "text": att[1]},
            "requirement": requirement_view(task_id), "run": run_view(task_id, live), "live": logger.live_tail(task_id, 150)}


REVIEW_DIFF_MAX = 300_000          # characters of diff shown; the patch files always hold all of it
_diff_cache = {}                   # (task, commit) -> files + diff: a commit never changes, so it is computed once


def _final_report(tdir):
    finals = sorted((tdir / "reports").glob("test_report-*-FINAL.json")) if (tdir / "reports").is_dir() else []
    return _json(finals[-1]) if finals else None


def review_view(task_id):
    """One review screen for the merge decision (master prompt, phase B): every acceptance criterion with the final
    regression checks that cover it (command, result, evidence), the security report, the changed files and the
    real diff - all of the APPROVAL's commit, so what is shown is exactly what an approval delivers. `stale`: the
    workspace, requirement or security report changed after the approval was requested; Continue asks again."""
    from . import approvals, loop, orchestrator
    if not logger.TASK_ID_RE.match(task_id or ""):
        return None
    tdir = config.home() / "tasks" / task_id
    ws = tdir / "workspace"
    entry = orchestrator.load_entry(task_id) or {}
    ap, st = approvals.latest(task_id, "merge")
    delivery = entry.get("delivery")
    if ap is None and not delivery:
        return None
    sha = orchestrator.approved_sha(ap.get("diff_ref")) if ap else (delivery or {}).get("commit")
    stale = None
    if ap and st == "pending" and ws.is_dir():
        try:
            stale = orchestrator._merge_request(task_id, tdir)[1] != ap["diff_ref"]
        except Exception:
            stale = None
    req = entry.get("requirements") or _json(tdir / "requirements.json") or {}
    final = _final_report(tdir) or {}
    checks = final.get("checks") or []

    def covered(cid):
        return [{k: c.get(k) for k in ("name", "result", "command", "evidence", "exit_code", "not_verified_because")}
                for c in checks if cid in (c.get("criterion_ids") or [])]
    criteria = [{"id": f"AC{i}", "text": t, "checks": covered(f"AC{i}")} for i, t in enumerate(req.get("acceptance_criteria") or [], 1)]
    for c in criteria:
        c["passed"] = bool(c["checks"]) and all(x["result"] == "pass" for x in c["checks"])
        c["unverified"] = bool(c["checks"]) and all(x["result"] == "unverified" for x in c["checks"])   # the human decides
    constraints = [{"id": f"C{i}", "text": t, "checks": covered(f"C{i}")} for i, t in enumerate(req.get("constraints") or [], 1)]
    files, diff, cut = [], "", False
    if sha and ws.is_dir():
        key = (task_id, sha)
        if key not in _diff_cache:
            try:
                base = orchestrator._base(ws)
                num = loop._git(ws, "diff", "--numstat", f"{base}..{sha}", "--", ".", *orchestrator.JUNK).stdout
                fl = []
                for line in num.splitlines():
                    a, r_, path = (line.split("\t", 2) + ["", ""])[:3]
                    fl.append({"path": path, "added": a, "removed": r_})
                text = loop._git(ws, "diff", "--no-color", f"{base}..{sha}", "--", ".", *orchestrator.JUNK).stdout
                _diff_cache[key] = (fl, text[:REVIEW_DIFF_MAX], len(text) > REVIEW_DIFF_MAX)
            except Exception:
                _diff_cache[key] = ([], "", False)
        files, diff, cut = _diff_cache[key]
    return {"task_id": task_id, "approval": ap and {"id": ap["id"], "status": st, "created_at": ap.get("created_at")},
            "commit": sha, "stale": stale, "goal": req.get("goal"), "criteria": criteria, "constraints": constraints,
            "final": {"verdict": final.get("verdict"), "summary": final.get("summary"), "unmapped": [
                {k: c.get(k) for k in ("name", "result", "command", "evidence")} for c in checks if not c.get("criterion_ids")]},
            "security": _json(tdir / "security_report.json"), "files": files, "diff": diff, "diff_cut": cut,
            "delivery": delivery, "status": entry.get("status")}


def run_view(task_id, live=None):
    """What the Task monitor needs for its buttons: the queue status, whether a live process runs it, the user's
    Start/Continue request, a pending control, and why it cannot run now (scheduler.run_blocker)."""
    from . import orchestrator, scheduler, worker
    entry = orchestrator.load_entry(task_id)
    if entry is None:
        return None
    live = _live_tasks() if live is None else live
    why, hard = scheduler.run_blocker(entry)
    ctl = _json(config.home() / "tasks" / task_id / "control.json") or {}
    applied = {"stop": "stopped", "pause": "paused"}.get(ctl.get("action"))
    edit_why = "it is running" if task_id in live else (
        orchestrator.requirement_edit_blocker(task_id, entry) if entry["status"] in ("needs_input", "blocked", "queued", "stopped", "paused")
        else "only before anything is built, while the task waits")
    started_ws = (config.home() / "tasks" / task_id / "workspace" / ".git").exists()
    project = {"repo": entry.get("target_repo"), "ref": entry.get("base_ref"), "base": entry.get("project_base"),
               "editable": not started_ws and task_id not in live and entry["status"] not in ("done", "rejected")}
    return {"project": project, "requirement": entry.get("requirements"), "req_revision": entry.get("req_revision") or 1,
            "can_edit_requirement": edit_why is None, "edit_blocker": edit_why,
            "status": entry["status"], "note": entry.get("note", ""), "running": task_id in live,
            "request": worker.get_request(task_id), "blocker": why, "blocker_needs_you": hard,
            "control": ctl.get("action"), "control_pending": bool(applied) and entry["status"] != applied and task_id in live,
            "started": (config.home() / "tasks" / task_id / "progress.json").exists()}


def requirement_view(task_id):
    """The original request, the draft (or the approved requirement) and the latest requirement approval.
    phase=True while it still waits for the human."""
    from . import approvals, intake
    entry = _json(config.home() / "queue" / f"{task_id}.json") or {}
    phase = entry.get("requirements") is None and bool(entry.get("request_text"))
    ap, st = approvals.latest(task_id, "requirements")
    draft = intake.draft(task_id)
    return {"phase": phase, "status": entry.get("status"), "request": entry.get("request_text"), "origin": entry.get("origin"),
            "requirement": draft if phase or draft else entry.get("requirements"),
            "blocking": intake.blocking_questions(draft) if phase else [],
            "approval": {"id": ap["id"], "status": st} if ap else None}


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
        entry = _json(config.home() / "queue" / f"{a['task_id']}.json") or {}
        if a["gate"] == "requirements" and entry.get("status") != "waiting_approval":
            continue                                   # a reply is pending: this draft is about to be revised
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


def assistant_sessions():
    from . import assistant
    out = []
    for sid in assistant.session_ids():
        try:
            out.append(assistant.summary(assistant.load(sid)))
        except (OSError, ValueError, KeyError):
            continue
    return {"sessions": out}


def assistant_view(sid):
    """One draft session as the dashboard shows it: conversation, draft, revision, what blocks submission."""
    from . import assistant
    try:
        return assistant.view(sid)
    except (KeyError, ValueError):
        return None
