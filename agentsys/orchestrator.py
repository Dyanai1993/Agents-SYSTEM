"""Orchestrator: deterministic task state machine around the core loop.

  queued -> intake -> plan/build/test/fix (loop.run_task) -> review + MERGE GATE -> delivered (done)

process() is idempotent: call it again at any time. It resumes, re-checks the approval files, and does
nothing expensive when nothing changed. States that wait for a human: waiting_approval, needs_input, needs_approval.

Delivery policy (Phase 3): an approved merge produces PATCH FILES and, optionally, a NEW BRANCH `agentsys/<task>`
in a target repo. It never merges into main and never pushes.
"""
import json
import subprocess
from pathlib import Path

from . import approvals, config, intake, logger, loop, memory, schema, tasks
from .ledger import BudgetExceeded, Ledger

RESET_GUESS_S = 5 * 3600     # (assumption) when a usage limit hits, retry after this long
LOCK_STALE_S = 2 * 3600      # a lock older than this belongs to a dead run


class TaskBusy(RuntimeError):
    """Another process is advancing this task right now."""


class _Lock:
    def __init__(self, task_id):
        self.path = _qdir() / f"{task_id}.lock"

    def __enter__(self):
        import os, time
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.time() - self.path.stat().st_mtime > LOCK_STALE_S:
                    self.path.unlink(missing_ok=True)        # stale: the owner died
                    continue
                raise TaskBusy(f"task is being processed by another run ({self.path.name})")
        raise TaskBusy("could not take the task lock")

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


def _qdir():
    d = config.home() / "queue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_entry(task_id):
    p = _qdir() / f"{logger.safe_task_id(task_id)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_entry(entry):
    p = _qdir() / f"{entry['task_id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def submit(task_id, *, request_text=None, requirements=None, title=None, target_repo=None, priority=5):
    logger.safe_task_id(task_id)
    if load_entry(task_id):
        raise ValueError(f"task {task_id} already exists")
    if bool(request_text) == bool(requirements):
        raise ValueError("give exactly one of request_text or requirements")
    if requirements is not None:
        schema.validate("requirements", requirements)
    entry = {"task_id": task_id, "title": title or tasks.short_title((requirements or {}).get("goal") or request_text), "status": "queued",
             "priority": priority, "created_at": logger.now(), "request_text": request_text, "requirements": requirements,
             "target_repo": str(target_repo) if target_repo else None, "resume_after": None, "note": ""}
    save_entry(entry)
    tasks.set_state(task_id, "intake", title=entry["title"])
    logger.log_event(task_id, "orchestrator", "state_change", "task queued", status="ok")
    return entry


# Build junk is never delivered, even when an older workspace committed it before the workspace .gitignore existed.
JUNK = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/.pytest_cache/**",
        ":(exclude,glob)**/.mypy_cache/**", ":(exclude,glob)**/.ruff_cache/**", ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/.DS_Store"]

RETRYABLE = {"needs_input", "needs_approval", "blocked", "paused", "queued"}


def retry(task_id, answer=None):
    """Put a stopped task back in the queue. It resumes from its checkpoint (progress.json), not from the start.

    `answer` is the human's reply to a question: it is added as a constraint ("Clarification: ...") and any blocking
    unknown in the requirement is marked answered. A task waiting for an approval is decided, not retried.
    """
    entry = load_entry(task_id)
    if entry is None:
        raise KeyError(task_id)
    if entry["status"] not in RETRYABLE:
        raise ValueError(f"task is '{entry['status']}': retry applies to {sorted(RETRYABLE)}"
                         + (" (decide the pending approval instead)" if entry["status"] == "waiting_approval" else ""))
    if answer:
        answer = " ".join(answer.split())[:300]
        req = entry["requirements"]
        if req is None:
            p = config.home() / "tasks" / task_id / "requirements.json"
            req = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        if req is None:
            raise ValueError("this task has no structured requirement yet: submit it again with the answer in the request")
        req = dict(req, constraints=list(req["constraints"]) + [f"Clarification from the user: {answer}"],
                   unknowns=[dict(u, blocking=False) for u in req["unknowns"]])
        schema.validate("requirements", req)
        entry["requirements"] = req
    entry.update(status="queued", note="retry requested" + (" with an answer" if answer else ""), resume_after=None)
    save_entry(entry)
    logger.log_event(task_id, "human", "decision", "retry requested" + (" with an answer" if answer else ""), status="ok")
    return entry


def _merge_context(task_id, tdir):
    summary = (tdir / "summary.md").read_text(encoding="utf-8") if (tdir / "summary.md").exists() else ""
    ws = tdir / "workspace"
    stat = loop._git(ws, "diff", "--stat", f"{_base(ws)}..HEAD", "--", ".", *JUNK).stdout.strip()
    return f"{summary}\nChanged files:\n{stat}\n\nApproving prepares patch files (and a branch `agentsys/{task_id}` if a target repo was given). Nothing is merged into main."


def _base(ws):
    return loop._git(ws, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]


def _merge_diff_ref(task_id, tdir):
    """Identifies exactly what a merge approval covers: the workspace commit AND the requirement version it
    was built against. Either changing (e.g. `orch.py retry --answer` adding a constraint, which forces a new
    final regression and possibly new commits) must force a fresh human approval instead of reusing an old
    'approved' decision made for different content."""
    ws = tdir / "workspace"
    ws_sha = loop._git(ws, "rev-parse", "HEAD").stdout.strip()
    prog_path = tdir / "progress.json"
    req_hash = json.loads(prog_path.read_text(encoding="utf-8")).get("req_hash") if prog_path.exists() else None
    return f"git:{ws_sha}:req:{req_hash}"


def _delivery_journal(tdir):
    return tdir / "delivery.json"


def deliver(task_id, target_repo=None):
    """Export the workspace history as patches; optionally apply them on a NEW branch of a target repo.

    Idempotent and self-cleaning: a crash or process failure (ours or git's) must never leave the target
    repo checked out on a half-finished branch that blocks every later retry, and re-running after a
    successful delivery must not try to recreate the branch and fail.
    """
    tdir = config.home() / "tasks" / task_id
    ws = tdir / "workspace"
    out = config.home() / "deliveries" / task_id
    out.mkdir(parents=True, exist_ok=True)
    r = loop._git(ws, "format-patch", "-q", "-o", str(out), f"{_base(ws)}..HEAD", "--", ".", *JUNK)
    patches = sorted(p.name for p in out.glob("*.patch"))
    if r.returncode or not patches:
        raise RuntimeError(f"nothing to deliver: {r.stderr.strip() or 'no commits'}")
    result = {"patches_dir": str(out), "patches": patches, "branch": None}
    jpath = _delivery_journal(tdir)
    journal = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else None
    if target_repo:
        repo = Path(target_repo)
        if not repo.is_dir():
            raise RuntimeError(f"target repo {repo} does not exist")
        if loop._git(repo, "rev-parse", "--is-inside-work-tree").returncode:
            raise RuntimeError(f"target repo {repo} is not a git repository")
        branch = f"agentsys/{task_id}"
        if journal and journal.get("target_repo") == str(repo) and journal.get("branch") == branch and journal.get("state") == "delivered":
            result["branch"] = branch           # already delivered here before (e.g. a crash after success): nothing more to do
            return result
        orig_branch = loop._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
        if _branch_exists_in(repo, branch):
            # Leftover from an earlier attempt that failed before cleaning up (git am failed, or a crash).
            # It was never recorded as 'delivered', so it is ours to remove before trying again.
            loop._git(repo, "checkout", "-q", orig_branch)
            loop._git(repo, "branch", "-D", branch)
        if loop._git(repo, "status", "--porcelain").stdout.strip():
            raise RuntimeError(f"target repo {repo} has uncommitted changes: not touching it")
        jpath.write_text(json.dumps({"target_repo": str(repo), "branch": branch, "orig_branch": orig_branch, "state": "creating"}), encoding="utf-8")
        if loop._git(repo, "checkout", "-q", "-b", branch).returncode:
            raise RuntimeError(f"cannot create branch {branch} in {repo}")
        am = loop._git(repo, "am", "-q", *[str(out / p) for p in patches])
        if am.returncode:
            loop._git(repo, "am", "--abort")
            loop._git(repo, "checkout", "-q", orig_branch)      # leave the target repo where it was, not on the broken branch
            loop._git(repo, "branch", "-D", branch)
            raise RuntimeError(f"patches do not apply cleanly on {repo}: {am.stderr.strip()[:200]}")
        jpath.write_text(json.dumps({"target_repo": str(repo), "branch": branch, "orig_branch": orig_branch, "state": "delivered"}), encoding="utf-8")
        result["branch"] = branch
    return result


def _branch_exists_in(repo, branch):
    return loop._git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0


def process(task_id, *, backend, env=None, settings_paths=None):
    """Advance one task as far as it can go without a human. Returns the updated queue entry. Raises TaskBusy if locked."""
    with _Lock(task_id):
        return _process(task_id, backend=backend, env=env, settings_paths=settings_paths)


def _process(task_id, *, backend, env=None, settings_paths=None):
    entry = load_entry(task_id)
    if entry is None:
        raise KeyError(task_id)
    if entry["status"] in ("done", "rejected"):
        return entry
    tdir = config.home() / "tasks" / task_id
    kw = dict(env=env, settings_paths=settings_paths)

    def finish(status, note="", **extra):
        entry.update(status=status, note=note, **extra)
        save_entry(entry)
        return entry

    # ---- intake (only when no structured requirement exists yet)
    req = entry["requirements"]
    if req is None and (tdir / "requirements.json").exists():
        req = json.loads((tdir / "requirements.json").read_text(encoding="utf-8"))
    if req is None:
        Ledger(task_id).begin_run()       # a retried intake attempt gets its own fresh run clock, like the build loop
        try:
            req, err = intake.run_intake(task_id, entry["request_text"], backend=backend, **kw)
        except BudgetExceeded as e:
            tasks.set_state(task_id, "blocked", title=entry["title"])
            return finish("blocked", f"Limit hit during intake: {e.reason}. I stopped and need your decision.")
        if err:
            tasks.set_state(task_id, "blocked", title=entry["title"])
            return finish("blocked", f"intake failed: {err}")
    entry["requirements"] = req

    # ---- core loop (resumes from progress.json)
    res = loop.run_task(task_id, req, backend=backend, title=entry["title"], **kw)
    if res["status"] != "paused":
        try:
            memory.extract_lessons(task_id)           # facts -> lessons, no LLM; never breaks the run
        except Exception as e:
            logger.log_event(task_id, "orchestrator", "alert", f"lesson extraction failed: {e}"[:200], status="error")
    if res["status"] == "paused":
        return finish("paused", res["message"], resume_after=_now_plus(RESET_GUESS_S) if "limit" in res["message"].lower() else None)
    if res["status"] != "ready_for_review":
        if res["status"] == "needs_approval":
            pass                                    # the Opus request already exists
        return finish(res["status"], res["message"])

    # ---- merge gate
    ap = approvals.request(task_id, "merge", _merge_context(task_id, tdir), diff_ref=_merge_diff_ref(task_id, tdir))
    st = approvals.status(ap["id"])
    if st == "pending":
        tasks.set_state(task_id, "review", title=entry["title"], waiting_approval=True)
        return finish("waiting_approval", f"waiting for your decision on {ap['id']}")
    if st == "rejected":
        tasks.set_state(task_id, "blocked", title=entry["title"])
        return finish("rejected", "merge rejected by user")
    try:
        d = deliver(task_id, entry["target_repo"])
    except RuntimeError as e:
        tasks.set_state(task_id, "blocked", title=entry["title"])
        return finish("blocked", f"delivery failed: {e}")
    for gate_state in ("security", "integrate", "deploy"):
        logger.log_event(task_id, "orchestrator", "state_change", f"{gate_state}: skipped (no specialist yet)", from_=gate_state, status="skipped")
    tasks.set_state(task_id, "done", title=entry["title"])
    logger.log_event(task_id, "orchestrator", "decision", "delivered", payload=d)
    return finish("done", f"delivered: {d['patches_dir']}" + (f", branch {d['branch']}" if d["branch"] else ""), delivery=d)


def _now_plus(seconds):
    import time
    return time.time() + seconds
