"""The worker: the ONE long-running process that makes model calls for the dashboard.

It answers Requirements Assistant messages and runs the tasks the user asked it to run (Start / Continue / Answer in
the dashboard, saved as request files). It never starts a task on its own: a queued task waits for the user's click
(or a `run-next` the user types). The dashboard only reads files and writes requests; it never calls a model.

Seen live, and fixed here:
- the user could not tell whether anything was running: a heartbeat file (state/workers/<instance>.json) is
  rewritten every BEAT_S seconds by a thread, also during a 20-minute agent call, with the job and its start time;
- two `orch.py assistant` processes raced, one running old code: only one worker can run (an OS lock), and every
  heartbeat carries the build id of the code the process started with, so the dashboard can say "restart it".
"""
import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

from . import assistant, billing_guard, config, logger, orchestrator, scheduler
from .filelock import FileLock, LockBusy

BEAT_S = 5
STALE_S = 20           # no heartbeat for this long: the process is gone (closed, crashed, or the machine slept)
ROOT = Path(__file__).resolve().parent.parent
CODE_GLOBS = ("agentsys/*.py", "scripts/*.py", "dashboard/server.py", ".claude/agents/*.md", "schemas/*.json")


class AlreadyRunning(RuntimeError):
    pass


# ---------------------------------------------------------------- build id: which code a process runs
_build_cache = {}


def build_id(root=ROOT):
    """A short hash of the code a running process depends on (index.html is excluded: the server reads it from disk on
    every request). Cached by file sizes and times, so calling it on every dashboard poll stays cheap."""
    files = sorted(p for g in CODE_GLOBS for p in Path(root).glob(g) if p.is_file())
    sig = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
    if _build_cache.get("sig") != sig:
        h = hashlib.sha256()
        for p in files:
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes() + b"\0")
        _build_cache.update(sig=sig, id=h.hexdigest()[:12])
    return _build_cache["id"]


# ---------------------------------------------------------------- heartbeat
def _wdir():
    return config.home() / "state" / "workers"


def _atomic_write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)


class Heartbeat:
    """Publishes that this process is alive and what it is doing, every BEAT_S seconds, from a thread - so a long
    agent call still shows as alive. Removed on a clean exit; a crash leaves it behind and it goes stale."""

    def __init__(self, kind, build=None):
        pid = os.getpid()
        self.doc = {"instance": f"{kind}-{pid}-{uuid.uuid4().hex[:6]}", "kind": kind, "pid": pid,
                    "build": build or build_id(), "started_at": time.time(), "beat_at": None, "job": None}
        self.path = _wdir() / f"{self.doc['instance']}.json"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def set_job(self, kind, job_id):
        with self._lock:
            self.doc["job"] = {"kind": kind, "id": job_id, "since": time.time()}
        self.beat()

    def clear_job(self):
        with self._lock:
            self.doc["job"] = None
        self.beat()

    def beat(self):
        with self._lock:
            self.doc["beat_at"] = time.time()
            doc = dict(self.doc)
        try:
            _atomic_write(self.path, doc)
        except OSError:
            pass               # e.g. the dashboard was reading the file on Windows: the next beat writes it

    def _run(self):
        while not self._stop.wait(BEAT_S):
            self.beat()

    def __enter__(self):
        self.beat()
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat")
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            self.path.unlink()
        except OSError:
            pass


def _read_json(p):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _job_detail(job):
    """What a task job is doing right now: state.json's phase and agent (written before every agent call)."""
    if not job or job.get("kind") != "task":
        return job
    st = _read_json(config.home() / "tasks" / str(job.get("id")) / "state.json") or {}
    return dict(job, state=st.get("state"), agent=st.get("current_agent"))


def status(now=None, server_build=None):
    """Every process that publishes a heartbeat, plus one line for the dashboard: (level, text)."""
    now = time.time() if now is None else now
    disk = build_id()
    procs = []
    for p in sorted(_wdir().glob("*.json")) if _wdir().is_dir() else []:
        d = _read_json(p)
        if not d or not isinstance(d.get("beat_at"), (int, float)):
            continue
        age = now - d["beat_at"]
        procs.append(dict(d, age_s=round(age, 1), alive=age <= STALE_S, old_code=d.get("build") != disk,
                          job=_job_detail(d.get("job"))))
    alive = [w for w in procs if w["alive"]]
    workers = [w for w in alive if w["kind"] == "worker"]
    crashed = sorted((w for w in procs if not w["alive"] and w.get("job")), key=lambda w: w["beat_at"])
    level, text = _summary(workers, alive, crashed, now)
    if server_build and server_build != disk:
        level, text = "warn", "This dashboard server runs old code: restart it (python scripts/up.py). " + text
    return {"processes": procs, "disk_build": disk, "server_build": server_build, "level": level, "text": text}


def _doing(w, now):
    j = w.get("job")
    if not j:
        return "idle, ready"
    mins = (now - j["since"]) / 60
    took = f"{int(mins)} min" if mins >= 1 else "less than a minute"
    if j["kind"] == "draft":
        return f"answering your message in {j['id']} ({took})"
    who = j.get("agent") or j.get("state") or "working"
    return f"running {j['id']}: {who} for {took}"


def _summary(workers, alive, crashed, now):
    if len(workers) > 1:
        return "bad", f"{len(workers)} workers are running (pids {', '.join(str(w['pid']) for w in workers)}): stop all but one"
    if workers:
        w = workers[0]
        if w["old_code"]:
            return "warn", f"Worker: {_doing(w, now)} - it runs code from before the last update: restart it when it is idle"
        return "ok", f"Worker: {_doing(w, now)} (heartbeat {int(w['age_s'])} s ago)"
    others = [w for w in alive if w["kind"] != "worker"]
    if others:
        return "ok", f"No worker; a run-next in a terminal (pid {others[0]['pid']}) is {_doing(others[0], now)}"
    if crashed:
        c = crashed[-1]
        return "bad", (f"The worker stopped while {_doing(c, c['beat_at'])} (last heartbeat {int((now - c['beat_at']) / 60)} min ago): "
                       f"it was closed or crashed. Start it again (python scripts/up.py); press Continue on the task to resume from its checkpoint.")
    return "warn", "Worker offline: messages and Start requests wait. Start it with: python scripts/up.py"


def clean_stale(now=None):
    """A crashed process leaves its heartbeat behind; keep only the newest stale one (it explains the crash)."""
    now = time.time() if now is None else now
    stale = sorted((p for p in _wdir().glob("*.json")), key=lambda p: (_read_json(p) or {}).get("beat_at") or 0) if _wdir().is_dir() else []
    stale = [p for p in stale if now - ((_read_json(p) or {}).get("beat_at") or 0) > STALE_S]
    for p in stale[:-1]:
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------- run requests (Start / Continue from the dashboard)
def _rdir():
    return config.home() / "requests" / "run"


def _rpath(task_id):
    return _rdir() / f"{logger.safe_task_id(task_id)}.json"


def requests():
    out = []
    for p in sorted(_rdir().glob("*.json")) if _rdir().is_dir() else []:
        d = _read_json(p)
        if d:
            out.append(d)
    return sorted(out, key=lambda d: d.get("requested_at", 0))


def get_request(task_id):
    return _read_json(_rpath(task_id))


def request_run(task_id, action="start"):
    """Save the user's Start / Continue for the worker. Refused now (ValueError, with the reason) when the task cannot
    run until the human does something else; otherwise saved, even if it must wait (another run, the usage window).
    Idempotent: a second click returns the same waiting request."""
    entry = orchestrator.load_entry(logger.safe_task_id(task_id))
    if entry is None:
        raise KeyError(f"no task {task_id}")
    why, hard = scheduler.run_blocker(entry)
    if hard:
        raise ValueError(f"{task_id} cannot run yet: {why}")
    old = get_request(task_id)
    if old and old.get("status") in ("waiting", "running"):
        return old
    doc = {"task_id": task_id, "action": action, "requested_at": time.time(), "status": "waiting", "note": why or ""}
    _atomic_write(_rpath(task_id), doc)
    logger.log_event(task_id, "dashboard", "decision", f"{action} requested (the worker runs it)", status="ok", payload=doc)
    return doc


def cancel_run(task_id):
    try:
        _rpath(task_id).unlink()
    except OSError:
        pass


def _mark(task_id, **kw):
    d = get_request(task_id)
    if d:
        d.update(kw)
        _atomic_write(_rpath(task_id), d)


# ---------------------------------------------------------------- the work loop
def step(*, backend, heartbeat=None, env=None, settings_paths=None, now=None):
    """One unit of work, then return: a waiting chat message first (the human is waiting for it), else ONE requested
    task run (as far as it goes without the human). Returns what was done, or None when there was nothing to do.
    One model call at a time across processes: a run-next typed in a terminal holds the same lock."""
    hb = heartbeat
    lock = assistant.exec_lock()
    try:
        lock.__enter__()
    except LockBusy:
        return None
    try:
        on_job = hb.set_job if hb else None
        try:
            turn = assistant.process_next_turn(backend=backend, env=env, settings_paths=settings_paths, locked=True, on_job=on_job)
        finally:
            if hb:
                hb.clear_job()
        if turn is not None:
            return {"assistant_turn": turn}
        for req in requests():
            tid = req["task_id"]
            if req.get("status") not in ("waiting", "running"):
                continue                                   # refused: shown until the user acts again
            entry = orchestrator.load_entry(tid)
            if entry is None:
                cancel_run(tid)
                continue
            why, hard = scheduler.run_blocker(entry, now)
            if hard:
                _mark(tid, status="refused", note=why)
                continue
            if why:
                if why != req.get("note"):
                    _mark(tid, status="waiting", note=why)
                continue
            _mark(tid, status="running", note="", started_at=time.time())
            if hb:
                hb.set_job("task", tid)
            try:
                out = orchestrator.process(tid, backend=backend, env=env, settings_paths=settings_paths)
            except orchestrator.TaskBusy as e:
                _mark(tid, status="waiting", note=f"{e} (a run-next in a terminal?)")
                continue
            except Exception as e:
                _mark(tid, status="refused", note=f"the run failed: {type(e).__name__}: {str(e)[:200]}")
                raise
            finally:
                if hb:
                    hb.clear_job()
            cancel_run(tid)                                # done: the task's own status and note say what happened
            return {"task_id": tid, "status": out.get("status"), "note": out.get("note")}
        return None
    finally:
        lock.__exit__(None, None, None)


def run_forever(*, backend, poll_s=2, log=print, should_stop=None):
    """The worker process. One at a time (an OS lock); a heartbeat for the dashboard; a short sleep when idle - no
    model call is ever made unless a message or a Start/Continue request is waiting."""
    try:
        single = FileLock(config.home() / "state" / "worker.lock")
        single.__enter__()
    except LockBusy:
        raise AlreadyRunning("another worker is already running: use that one (the dashboard shows it), or stop it first")
    try:
        clean_stale()
        with Heartbeat("worker") as hb:
            while not (should_stop and should_stop()):
                try:
                    out = step(backend=backend, heartbeat=hb)
                except billing_guard.BillingGuardError:
                    raise                          # fail closed: a wrong credential source stops the worker
                except Exception as e:             # one broken run (already marked on its request) must not end the worker
                    log(time.strftime("%H:%M:%S") + f" error: {type(e).__name__}: {str(e)[:300]}")
                    time.sleep(poll_s)
                    continue
                if out is None:
                    time.sleep(poll_s)
                else:
                    log(time.strftime("%H:%M:%S") + " " + json.dumps(out, ensure_ascii=False))
    finally:
        single.__exit__(None, None, None)
