"""Requirements Assistant: a conversation that turns a rough idea into a requirement the human approves.

Draft sessions live in drafts/<D-####>/session.json, apart from runnable tasks. One file holds the draft, its revision,
the conversation and the state of the current assistant turn, and is replaced atomically.

Who does what:
  - the dashboard (actions.py) only WRITES files: a user message (= a turn request), a hand edit, a retry, a
    submission. It never calls a model.
  - process_next_turn() makes the model call. It runs from `orch.py run-next` or `orch.py assistant` (the local
    execution layer), never from an HTTP handler, through runner.run_agent: billing guard, budget, schema check.
  - submit() is the ONLY way a draft becomes a task, and only a human action calls it. The approved draft goes
    through the structured submission path (orchestrator.submit(requirements=...)): no intake, no requirement gate.

Revisions: every change to the draft (an applied assistant turn, a hand edit) bumps `revision`. A turn remembers
the revision it started from; if the draft moved on meanwhile, its proposal is kept in the conversation but NOT
applied - a late reply never overwrites a newer human edit.
"""
import json
import re
import time

from . import billing_guard, config, logger, orchestrator, runner, schema
from .filelock import FileLock, LockBusy
from .ledger import BudgetExceeded, Ledger

SESSION_RE = re.compile(r"^D-\d{4}$")
MESSAGE_MAX = 8000            # a pasted document fits in one message
CONTEXT_MESSAGES = 20         # model input: the recent conversation only...
CONTEXT_CHARS = 16000         # ...and at most this much of it
TURN_STALE_S = 600            # a turn "running" longer than this was interrupted (the runner stopped)
EMPTY_DRAFT = {"goal": "", "constraints": [], "acceptance_criteria": [], "risks": [], "unknowns": []}


class Conflict(ValueError):
    """The request is valid but the state moved on (HTTP 409)."""


def _root():
    d = config.home() / "drafts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(sid):
    if not SESSION_RE.match(sid or ""):
        raise ValueError(f"bad session id: {sid!r}")
    return _root() / sid / "session.json"


class _Locked:
    """A short lock, waited for briefly: every holder only reads and writes one file. `sid` locks one session;
    None locks id allocation (new session ids, and task ids at submission)."""
    def __init__(self, sid):
        self.lock = FileLock(_path(sid).parent / ".lock" if sid else _root() / "_alloc.lock")

    def __enter__(self):
        self.lock.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(60):
            try:
                self.lock.__enter__()
                return self
            except LockBusy:
                time.sleep(0.05)
        raise Conflict("the draft is busy (another action is saving it): try again")

    def __exit__(self, *exc):
        return self.lock.__exit__(*exc)


def exec_lock():
    """One agent call at a time across processes (run-next, the assistant worker): the sequential policy."""
    d = config.home() / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return FileLock(d / "_exec.lock")


def load(sid):
    p = _path(sid)
    if not p.exists():
        raise KeyError(sid)
    return json.loads(p.read_text(encoding="utf-8"))


def _save(s):
    s["updated_at"] = logger.now()
    p = _path(s["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def session_ids():
    return sorted(p.name for p in _root().iterdir() if SESSION_RE.match(p.name) and (p / "session.json").exists())


def _new_id():
    nums = [int(n[2:]) for n in session_ids()]
    return f"D-{max(nums, default=0) + 1:04d}"


def _clean(text):
    text = str(text or "").strip()
    if not text:
        raise ValueError("the message is empty")
    if len(text) > MESSAGE_MAX:
        raise ValueError(f"the message is longer than {MESSAGE_MAX} characters")
    return text


def _validate_draft(draft):
    errs = schema.errors_for("assistant_turn", {"reply": "x", "draft": draft, "suggestions": [], "conflicts": []})
    if errs:
        raise ValueError("the draft is not valid: " + "; ".join(e.replace("draft/", "") for e in errs[:3]))


# ------------------------------------------------------------------ human actions (files only)
def post_message(sid, text):
    """A user message, which requests exactly ONE assistant turn. sid=None starts a new session."""
    text = _clean(text)
    if sid is None:
        with _Locked(None):                           # serializes id allocation
            sid = _new_id()
            _save({"id": sid, "title": " ".join(text.split())[:80], "created_at": logger.now(), "revision": 0,
                   "draft": dict(EMPTY_DRAFT), "suggestions": [], "conflicts": [], "conversation": [], "turn": None,
                   "submitted": None, "submitting": None})
    with _Locked(sid):
        s = load(sid)
        if s["submitted"]:
            raise Conflict(f"this draft was already submitted as {s['submitted']['task_id']}")
        if s["turn"] and s["turn"]["status"] in ("pending", "running"):
            raise Conflict("the assistant is still answering your last message")
        n = 1 + max((m.get("turn", 0) for m in s["conversation"]), default=0)
        s["conversation"].append({"from": "user", "ts": logger.now(), "text": text, "turn": n})
        s["turn"] = {"id": n, "status": "pending", "requested_at": time.time(), "error": None}
        _save(s)
        return s


def retry_turn(sid):
    """Explicit retry of a failed turn. Failures are never retried automatically."""
    with _Locked(sid):
        s = load(sid)
        if not s["turn"] or s["turn"]["status"] != "failed":
            raise Conflict("there is no failed assistant turn to retry")
        if s["submitted"]:
            raise Conflict("this draft was already submitted")
        s["turn"].update(status="pending", requested_at=time.time(), error=None)
        _save(s)
        return s


def save_draft(sid, base_revision, draft):
    """A hand edit. Refused if the draft moved on since the editor loaded it (an assistant turn, another tab)."""
    _validate_draft(draft)
    with _Locked(sid):
        s = load(sid)
        if s["submitted"]:
            raise Conflict("this draft was already submitted: it can no longer be edited")
        if base_revision != s["revision"]:
            raise Conflict(f"the draft changed since you opened it (now revision {s['revision']}): reload it, your edit was not saved")
        s["draft"], s["revision"] = draft, s["revision"] + 1
        s["conversation"].append({"from": "user", "ts": logger.now(), "edit": True, "text": f"(edited the draft by hand: revision {s['revision']})"})
        _save(s)
        return s


def readiness(s):
    """What prevents submission, in plain words. Empty = ready. An application decision, never the model's."""
    if s["submitted"]:
        return []
    out = []
    d = s["draft"]
    if not d["goal"].strip():
        out.append("the goal is empty")
    if not d["acceptance_criteria"]:
        out.append("add at least one acceptance criterion the Tester can check")
    if not out:
        for e in schema.errors_for("requirements", d):
            out.append(f"not a valid requirement yet: {e}")
    out += [f"blocking question open: {u['question']}" for u in d["unknowns"] if u["blocking"]]
    return out                  # a waiting assistant turn does not block: a late reply never touches a submitted draft


def submit(sid, revision):
    """Approve revision `revision` and queue ONE task with exactly that requirement. Idempotent: the same approved
    revision always returns the same task, including a retry after an interrupted response."""
    with _Locked(sid), _Locked(None):                  # task-id allocation is serialized across sessions
        s = load(sid)
        if s["submitted"]:
            if s["submitted"]["revision"] == revision:
                return s["submitted"]["task_id"], False
            raise Conflict(f"already submitted as {s['submitted']['task_id']} (revision {s['submitted']['revision']})")
        pend = s.get("submitting")
        if pend and pend["revision"] != revision:
            raise Conflict(f"a submission of revision {pend['revision']} was interrupted: submit that revision to finish it")
        if not pend:
            if revision != s["revision"]:
                raise Conflict(f"the draft changed since you reviewed it (now revision {s['revision']}): review it again")
            blockers = readiness(s)
            if blockers:
                raise ValueError("not ready to submit: " + "; ".join(blockers))
            pend = s["submitting"] = {"revision": revision, "task_id": orchestrator.next_task_id(), "requirements": s["draft"]}
            _save(s)                                    # journal first: a crash below is finished by the next retry
        task_id, req = pend["task_id"], pend["requirements"]
        entry = orchestrator.load_entry(task_id)
        if entry is None:
            orchestrator.submit(task_id, requirements=req, origin={"draft_session": sid, "revision": revision})
        elif (entry.get("origin") or {}).get("draft_session") != sid:
            raise Conflict(f"task id {task_id} was taken by another submission: contact the builder")
        (_root() / sid / f"approved-r{revision}.json").write_text(json.dumps(req, indent=2, ensure_ascii=False), encoding="utf-8")
        s["submitted"] = {"revision": revision, "task_id": task_id, "at": logger.now()}
        s["submitting"] = None
        s["conversation"].append({"from": "user", "ts": logger.now(), "text": f"(approved revision {revision} and submitted it as {task_id})", "edit": True})
        _save(s)
        logger.log_event(task_id, "human", "decision", f"requirement approved in the assistant ({sid} revision {revision})", status="ok")
        return task_id, True


# ------------------------------------------------------------------ the model call (run-next / the assistant worker)
def _context(conversation):
    msgs = [{"from": m["from"], "text": m["text"]} for m in conversation if m.get("text")][-CONTEXT_MESSAGES:]
    while len(msgs) > 1 and sum(len(m["text"]) for m in msgs) > CONTEXT_CHARS:
        msgs.pop(0)
    return msgs


def _fail(sid, turn_id, error):
    with _Locked(sid):
        s = load(sid)
        if s["turn"] and s["turn"]["id"] == turn_id:
            s["turn"].update(status="failed", error=error[:400], failed_at=time.time())
            _save(s)


def _pending(now):
    """Pending turns, oldest first; a turn left 'running' by a stopped runner is marked failed (explicit retry)."""
    out = []
    for sid in session_ids():
        s = load(sid)
        t = s["turn"]
        if t and t["status"] == "running" and now - t.get("claimed_at", now) > TURN_STALE_S:
            _fail(sid, t["id"], "interrupted: the runner stopped during this turn. Press Retry")
        elif t and t["status"] == "pending" and not s["submitted"]:
            out.append((t["requested_at"], sid))
    return [sid for _, sid in sorted(out)]


def process_next_turn(*, backend, env=None, settings_paths=None, now=None, locked=False, on_job=None):
    """Answer the oldest pending user message: exactly one model call. None when nothing waits.
    locked=True: the caller (scheduler.run_next) already holds exec_lock(). on_job(kind, id) is told which draft is
    being answered (the worker's heartbeat shows it)."""
    queue = _pending(time.time() if now is None else now)
    if not queue:
        return None
    if on_job:
        on_job("draft", queue[0])
    if locked:
        return _run_turn(queue[0], backend, env, settings_paths)
    lock = exec_lock()
    try:
        lock.__enter__()
    except LockBusy:
        return {"skipped": "another agent call is running (one at a time)", "session": queue[0]}
    try:
        return _run_turn(queue[0], backend, env, settings_paths)
    finally:
        lock.__exit__(None, None, None)


def _run_turn(sid, backend, env, settings_paths):
    with _Locked(sid):
        s = load(sid)
        t = s["turn"]
        if not t or t["status"] != "pending" or s["submitted"]:
            return None
        t.update(status="running", claimed_at=time.time(), base_revision=s["revision"])
        _save(s)
        turn_id, base = t["id"], s["revision"]
        payload = {"draft": s["draft"], "conversation": _context(s["conversation"])}
    try:
        Ledger(sid).begin_run()                          # every turn gets its own run clock
        res = runner.run_agent("assistant", payload, task_id=sid, run_id=f"turn-{turn_id}", backend=backend,
                               env=env, settings_paths=settings_paths)
    except runner.UsageLimitReached as e:
        _fail(sid, turn_id, f"usage limit reached ({e}). Your draft is safe. Press Retry after the window resets")
        return {"session": sid, "status": "failed", "reason": "usage limit"}
    except BudgetExceeded as e:
        _fail(sid, turn_id, f"this session reached its limit ({e.reason}). Edit the draft by hand, submit it, or start a new session")
        return {"session": sid, "status": "failed", "reason": "session limit"}
    except billing_guard.BillingGuardError as e:
        _fail(sid, turn_id, f"billing guard refused the call: {e}")
        raise                                            # fail closed: the caller must stop, not carry on
    except Exception as e:                              # a backend failure: recorded, the worker carries on
        _fail(sid, turn_id, f"backend error ({type(e).__name__}: {str(e)[:200]}). Your draft is safe. Press Retry")
        return {"session": sid, "status": "failed", "reason": "backend error"}
    except BaseException:                               # Ctrl+C: recorded, then the worker stops
        _fail(sid, turn_id, "interrupted. Press Retry")
        raise
    if not res.ok:
        _fail(sid, turn_id, f"the assistant's answer could not be used ({res.error[:200]}). Your draft is unchanged. Press Retry")
        return {"session": sid, "status": "failed", "reason": "invalid output"}
    out = res.output
    with _Locked(sid):
        s = load(sid)
        msg = {"from": "assistant", "ts": logger.now(), "text": out["reply"], "turn": turn_id,
               "suggestions": out["suggestions"], "conflicts": out["conflicts"]}
        if s["submitted"] or s["revision"] != base:
            msg.update(draft_not_applied=True, proposed_draft=out["draft"])     # a newer human edit wins
        else:
            s["draft"], s["revision"] = out["draft"], s["revision"] + 1
            s["suggestions"], s["conflicts"] = out["suggestions"], out["conflicts"]
        s["conversation"].append(msg)
        if s["turn"] and s["turn"]["id"] == turn_id:
            s["turn"].update(status="done", done_at=time.time())
        _save(s)
    return {"session": sid, "status": "done", "applied": not msg.get("draft_not_applied")}


# ------------------------------------------------------------------ read views (dashboard)
def summary(s):
    t = s["turn"] or {}
    status = ("submitted" if s["submitted"] else "assistant is answering" if t.get("status") in ("pending", "running")
              else "assistant turn failed" if t.get("status") == "failed" else "waiting for you")
    return {"id": s["id"], "title": s["title"], "status": status, "revision": s["revision"], "updated_at": s.get("updated_at"),
            "task_id": (s["submitted"] or {}).get("task_id")}


def view(sid):
    s = load(sid)
    task = orchestrator.load_entry(s["submitted"]["task_id"]) if s["submitted"] else None
    return dict(s, summary=summary(s), blockers=readiness(s), ready=not readiness(s),
                task_status=task["status"] if task else ("deleted" if s["submitted"] else None))
