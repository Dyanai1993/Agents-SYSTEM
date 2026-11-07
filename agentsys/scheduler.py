"""Usage-aware scheduler. Sequential: run_next() advances exactly ONE task. Parallel runs need explicit approval (not built)."""
import time

from . import approvals, assistant, config, control, ledger, orchestrator
from .filelock import LockBusy

TERMINAL = {"done", "rejected", "stopped"}
NEEDS_HUMAN = {"needs_input", "needs_approval", "blocked"}


def entries():
    out = []
    for p in sorted(orchestrator._qdir().glob("*.json")):
        e = orchestrator.load_entry(p.stem)
        if e:
            out.append(e)
    return out


def _runnable(e, now):
    if e["status"] in TERMINAL:
        return False
    if e["status"] == "paused":
        if control.read(e["task_id"]) == "pause":              # the user's pause is still in force
            return False
        return e.get("resume_after") is None or e["resume_after"] <= now
    if e["status"] == "needs_approval":
        return approvals.latest(e["task_id"], "opus")[1] == "approved"
    if e["status"] == "waiting_approval":                    # cheap: only when a decision file exists
        gate = "requirements" if orchestrator.in_requirement_phase(e) else "merge"
        _, st = approvals.latest(e["task_id"], gate)
        return st in ("approved", "rejected")
    if e["status"] in NEEDS_HUMAN:
        return False                                          # needs a human; the dashboard shows why
    return True                                               # queued


def pick(now=None):
    now = now or time.time()
    c = [e for e in entries() if _runnable(e, now)]
    c.sort(key=lambda e: (e["priority"], e["created_at"]))
    return c[0] if c else None


def run_next(*, backend, env=None, settings_paths=None, now=None, estimate_items=3):
    """Return None (nothing to do), {'skipped': reason}, {'assistant_turn': result} or the updated task entry.

    One agent call at a time across processes (run-next, `orch.py assistant`): the sequential policy. A waiting
    Requirements Assistant message is answered first - one cheap call the human is waiting for."""
    lock = assistant.exec_lock()
    try:
        lock.__enter__()
    except LockBusy:
        return {"skipped": "another agent call is running (one at a time)"}
    try:
        turn = assistant.process_next_turn(backend=backend, env=env, settings_paths=settings_paths, locked=True)
        if turn is not None:
            return {"assistant_turn": turn}
        return _run_next_task(backend=backend, env=env, settings_paths=settings_paths, now=now, estimate_items=estimate_items)
    finally:
        lock.__exit__(None, None, None)


def _run_next_task(*, backend, env, settings_paths, now, estimate_items):
    e = pick(now)
    if e is None:
        return None
    if e["status"] == "queued":                               # a NEW task: check the window first
        ok, why = ledger.Window().can_start_large_task(ledger.estimate_task_units(estimate_items))
        if not ok:
            e["note"] = f"not started: {why}"
            orchestrator.save_entry(e)
            return {"skipped": why, "task_id": e["task_id"]}
    try:
        return orchestrator.process(e["task_id"], backend=backend, env=env, settings_paths=settings_paths)
    except orchestrator.TaskBusy as busy:
        return {"skipped": str(busy), "task_id": e["task_id"]}
