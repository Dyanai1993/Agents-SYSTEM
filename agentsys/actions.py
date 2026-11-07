"""The ONLY write actions of the dashboard. No LLM: every action writes files, and model calls happen only in the
local execution layer (`orch.py run-next` / `orch.py assistant`: billing guard, caps).
  - approval decisions, and pause/stop/resume
  - Requirements Assistant: a message (= one turn request), a hand edit of the draft, a retry of a failed turn,
    and the approval of a draft revision - the only action that queues a task

Local-only protections: the server binds to 127.0.0.1; every POST must carry the custom header
`X-Agentsys: 1` (browsers cannot send it cross-site without a CORS preflight, which is refused),
a JSON body, and a Host/Origin that is localhost.
"""
import json
from urllib.parse import urlparse

from . import approvals, assistant, config, control, intake, logger, orchestrator, schema


def _host_ok(value):
    if not value:
        return True
    h = urlparse(value if "//" in value else "//" + value).hostname
    return h in ("127.0.0.1", "localhost")


def check_headers(headers):
    h = {k.lower(): v for k, v in headers.items()}
    if h.get("x-agentsys") != "1":
        return "missing X-Agentsys header"
    if "application/json" not in h.get("content-type", ""):
        return "content-type must be application/json"
    if not _host_ok(h.get("host")) or not _host_ok(h.get("origin")):
        return "host or origin is not localhost"
    return None


def handle_post(path, body, headers):
    """Return (http_code, json_obj)."""
    err = check_headers(headers)
    if err:
        return 403, {"error": err}
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}
    try:
        if path == "/api/approval/decide":
            if body.get("decision") not in ("approved", "rejected"):
                return 400, {"error": "decision must be approved or rejected"}
            aid = str(body.get("id", ""))
            if body["decision"] == "approved":
                err = _requirement_not_approvable(aid)
                if err:
                    return 400, {"error": err}
            return 200, approvals.decide(aid, body["decision"], str(body.get("note", "")))
        if path == "/api/control":
            if body.get("action") not in ("pause", "stop", "resume"):
                return 400, {"error": "action must be pause, stop or resume"}
            try:
                return 200, orchestrator.apply_control(str(body.get("task_id", "")), body["action"])
            except KeyError as e:
                return 404, {"error": str(e).strip("'\"")}
        if path in ("/api/task/run", "/api/task/answer"):
            return _task_action(path, body)
        if path == "/api/task/requirement":
            try:
                e = orchestrator.edit_requirement(str(body.get("task_id", "")), body.get("requirement"))
                return 200, {"task_id": e["task_id"], "revision": e["req_revision"], "note": e["note"]}
            except orchestrator.TaskBusy as e:
                return 409, {"error": f"{e}: wait until the current run ends"}
            except KeyError as e:
                return 404, {"error": str(e).strip("'\"")}
        if path == "/api/task/delete":
            try:
                return 200, orchestrator.delete_task(str(body.get("task_id", "")))
            except orchestrator.TaskBusy as e:
                return 409, {"error": f"{e}. Stop it and wait for the run to end, then delete."}
            except KeyError as e:
                return 404, {"error": str(e).strip("'\"")}
            except RuntimeError as e:
                return 500, {"error": str(e)[:200]}
        if path.startswith("/api/assistant/"):
            return _assistant(path, body)
    except (approvals.ApprovalError, ValueError, schema.SchemaError) as e:
        return 400, {"error": str(e)[:200]}
    return 404, {"error": "not found"}


ANSWER_MAX = 300      # orchestrator.retry keeps at most this much of an answer: refuse longer, never cut silently


def _task_action(path, body):
    """Start / Continue / Retry and Answer: files only. The worker does the run; the dashboard never calls a model."""
    from . import worker
    tid = str(body.get("task_id", ""))
    try:
        if path == "/api/task/run":
            act = body.get("action", "start")
            if act not in ("start", "continue", "retry"):
                return 400, {"error": "action must be start, continue or retry"}
            if act == "retry":                                 # blocked / needs_approval -> queued, from the checkpoint
                note = " ".join(str(body.get("answer") or "").split())   # optional: what the agents should do differently
                if len(note) > ANSWER_MAX:
                    return 400, {"error": f"notes are limited to {ANSWER_MAX} characters (yours has {len(note)}): shorten it"}
                orchestrator.retry(tid, note or None)
            return 200, {"request": worker.request_run(tid, act)}
        answer = " ".join(str(body.get("answer", "")).split())
        if not answer:
            return 400, {"error": "write your answer first"}
        if len(answer) > ANSWER_MAX:
            return 400, {"error": f"answers are limited to {ANSWER_MAX} characters (yours has {len(answer)}): shorten it"}
        e = orchestrator.retry(tid, answer)
        if e["status"] != "queued":            # before the requirement is approved, the answer changes the DRAFT
            return 200, {"status": e["status"], "note": e["note"]}
        return 200, {"status": e["status"], "note": e["note"], "request": worker.request_run(tid, "continue")}
    except orchestrator.TaskBusy as e:
        return 409, {"error": f"{e}: try again when the current run ends"}
    except KeyError as e:
        return 404, {"error": str(e).strip("'\"")}


def _assistant(path, body):
    sid = body.get("session")
    sid = None if sid in (None, "") else str(sid)
    try:
        if path == "/api/assistant/message":
            s = assistant.post_message(sid, body.get("text"))
            return 200, {"session": s["id"], "status": s["turn"]["status"]}
        if sid is None:
            return 400, {"error": "session is required"}
        if path == "/api/assistant/draft":
            s = assistant.save_draft(sid, _int(body.get("revision")), body.get("draft"))
            return 200, {"session": sid, "revision": s["revision"]}
        if path == "/api/assistant/retry":
            s = assistant.retry_turn(sid)
            return 200, {"session": sid, "status": s["turn"]["status"]}
        if path == "/api/assistant/submit":
            task_id, new = assistant.submit(sid, _int(body.get("revision")))
            return 200, {"session": sid, "task_id": task_id, "new": new}
    except assistant.Conflict as e:
        return 409, {"error": str(e)[:300]}
    except KeyError:
        return 400, {"error": "no such draft session"}
    except (ValueError, TypeError, schema.SchemaError) as e:
        return 400, {"error": str(e)[:400]}
    except Exception as e:                       # e.g. an interrupted submission: the retry finishes it
        return 500, {"error": f"{type(e).__name__}: {str(e)[:200]} - your draft is saved; try again"}
    return 404, {"error": "not found"}


def _int(v):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("revision must be a number")
    return v


def _requirement_not_approvable(approval_id):
    """A requirement draft with an open BLOCKING question cannot be approved here: building on it would stop at
    once to ask the same question. (`orch.py approve` still can; the build then stops with needs_input.)"""
    if not approvals.ID_RE.match(approval_id):
        return None
    p = config.home() / "approvals" / "pending" / f"{approval_id}.json"
    try:
        req_doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None                                   # approvals.decide reports the missing request itself
    if req_doc.get("gate") != "requirements":
        return None
    entry = orchestrator.load_entry(req_doc["task_id"]) or {}
    if entry.get("status") != "waiting_approval":
        return "this draft is not waiting for a decision"
    q = intake.blocking_questions(intake.draft(req_doc["task_id"]))
    return (f"answer the blocking question(s) first (python scripts/orch.py retry {req_doc['task_id']} --answer \"...\"): "
            f"{' | '.join(q)}") if q else None
