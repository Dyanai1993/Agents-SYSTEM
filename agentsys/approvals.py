"""Approval gates. A request is a file; the human's decision is a file. No LLM in the path.

approvals/pending/<id>.json   written by the orchestrator
approvals/decided/<id>.json   written by the dashboard or CLI (schema approval_decision)
Gates: merge, deploy, delete, send, spend, credentials, opus, other.
"""
import json
import re

from . import config, logger, schema

ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class ApprovalError(ValueError):
    pass


def _dir(kind):
    d = config.home() / "approvals" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def status(approval_id):
    """'pending', 'approved', 'rejected' or None (no such request)."""
    if not ID_RE.match(approval_id or ""):
        return None
    dec = _read(_dir("decided") / f"{approval_id}.json")
    if dec:
        return dec["decision"]
    return "pending" if (_dir("pending") / f"{approval_id}.json").exists() else None


def _requests_for(task_id, gate):
    out = []
    for p in sorted(_dir("pending").glob(f"{task_id}-{gate}-*.json")):
        doc = _read(p)
        if doc and doc.get("task_id") == task_id and doc.get("gate") == gate:
            out.append(doc)
    return out


def latest(task_id, gate):
    reqs = _requests_for(task_id, gate)
    return (reqs[-1], status(reqs[-1]["id"])) if reqs else (None, None)


def request(task_id, gate, context, diff_ref=None, new=False):
    """Create a request, or return the latest one. A rejected request is replaced only when new=True."""
    logger.safe_task_id(task_id)
    doc, st = latest(task_id, gate)
    if doc and (st in ("pending", "approved") or not new):
        return doc
    n = len(_requests_for(task_id, gate)) + 1
    doc = {"id": f"{task_id}-{gate}-{n}", "task_id": task_id, "gate": gate, "context": context[:4000],
           "created_at": logger.now(), "diff_ref": diff_ref}
    schema.validate("approval_request", doc)
    (_dir("pending") / f"{doc['id']}.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")
    logger.log_event(task_id, "orchestrator", "approval", f"approval requested: {gate}", status="pending", payload=doc)
    return doc


def decide(approval_id, decision, note=""):
    if not ID_RE.match(approval_id or ""):
        raise ApprovalError("bad approval id")
    req = _read(_dir("pending") / f"{approval_id}.json")
    if not req:
        raise ApprovalError("no such pending request")
    if status(approval_id) != "pending":
        raise ApprovalError("already decided")
    doc = {"id": approval_id, "decision": decision, "decided_at": logger.now(), "note": note[:500]}
    schema.validate("approval_decision", doc)
    try:                                         # exclusive create: of two simultaneous deciders only one wins
        with open(_dir("decided") / f"{approval_id}.json", "x", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=2))
    except FileExistsError:
        raise ApprovalError("already decided")
    logger.log_event(req["task_id"], "human", "approval", f"{req['gate']} {decision}", status="ok", payload=doc)
    return doc


def get_request(approval_id):
    if not ID_RE.match(approval_id or ""):
        return None
    return _read(_dir("pending") / f"{approval_id}.json")


def pending():
    return [d for p in sorted(_dir("pending").glob("*.json")) if (d := _read(p)) and status(d["id"]) == "pending"]


def history():
    out = []
    for p in sorted(_dir("decided").glob("*.json")):
        dec = _read(p)
        req = _read(_dir("pending") / p.name)
        if dec and req:
            out.append({**req, **{"decision": dec["decision"], "decided_at": dec["decided_at"], "note": dec.get("note", "")}})
    return out


class OpusNotApproved(RuntimeError):
    pass


def require_opus(task_id):
    doc, st = latest(task_id, "opus")
    if st == "approved":
        return
    if doc is None:
        request(task_id, "opus", "An agent wants to run on Opus. Opus uses the usage window fast. Approve for this task?")
    raise OpusNotApproved(f"Opus needs your approval for task {task_id}")
