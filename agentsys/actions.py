"""The ONLY write actions of the dashboard: approval decisions and pause/stop/resume. No LLM.

Local-only protections: the server binds to 127.0.0.1; every POST must carry the custom header
`X-Agentsys: 1` (browsers cannot send it cross-site without a CORS preflight, which is refused),
a JSON body, and a Host/Origin that is localhost.
"""
from urllib.parse import urlparse

from . import approvals, control, logger, schema


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
            return 200, approvals.decide(str(body.get("id", "")), body["decision"], str(body.get("note", "")))
        if path == "/api/control":
            if body.get("action") not in ("pause", "stop", "resume"):
                return 400, {"error": "action must be pause, stop or resume"}
            return 200, control.set_control(str(body.get("task_id", "")), body["action"])
    except (approvals.ApprovalError, ValueError, schema.SchemaError) as e:
        return 400, {"error": str(e)[:200]}
    return 404, {"error": "not found"}
