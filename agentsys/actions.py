"""The ONLY write actions of the dashboard: approval decisions and pause/stop/resume. No LLM: every action writes
files, and model calls happen only when `orch.py run-next` runs (billing guard, caps).

Local-only protections: the server binds to 127.0.0.1; every POST must carry the custom header
`X-Agentsys: 1` (browsers cannot send it cross-site without a CORS preflight, which is refused),
a JSON body, and a Host/Origin that is localhost.
"""
import json
from urllib.parse import urlparse

from . import approvals, config, control, intake, logger, orchestrator, schema


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
            return 200, control.set_control(str(body.get("task_id", "")), body["action"])
    except (approvals.ApprovalError, ValueError, schema.SchemaError) as e:
        return 400, {"error": str(e)[:200]}
    return 404, {"error": "not found"}


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
