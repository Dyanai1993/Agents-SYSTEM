"""Orchestrator: deterministic task state machine around the core loop.

  queued -> intake -> plan/build/test/fix (loop.run_task) -> review + MERGE GATE -> delivered (done)

process() is idempotent: call it again at any time. It resumes, re-checks the approval files, and does
nothing expensive when nothing changed. States that wait for a human: waiting_approval, needs_input, needs_approval.

Delivery policy (Phase 3): an approved merge produces PATCH FILES and, optionally, a NEW BRANCH `agentsys/<task>`
in a target repo. It never merges into main and never pushes.
"""
import hashlib
import json
import subprocess
from pathlib import Path

from . import approvals, config, control, intake, logger, loop, memory, runner, schema, tasks
from .filelock import FileLock, LockBusy
from .ledger import BudgetExceeded, Ledger

RESET_GUESS_S = 5 * 3600     # (assumption) when a usage limit hits, retry after this long


class TaskBusy(RuntimeError):
    """Another process is advancing this task right now."""


class _Lock:
    """A real OS-level lock (see filelock.FileLock): atomic acquire, auto-released if the owner dies, the
    lock file itself never deleted. Review (fourth round): the previous create-a-file-and-check-its-mtime
    scheme had a real stale-lock-takeover race and a release that could delete a DIFFERENT process's lock."""
    def __init__(self, task_id):
        self._inner = FileLock(_qdir() / f"{task_id}.lock")

    def __enter__(self):
        try:
            self._inner.__enter__()
        except LockBusy as e:
            raise TaskBusy(f"task is being processed by another run ({self._inner.path.name})") from e
        return self

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


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
    try:
        with _Lock(task_id):
            return _retry(task_id, answer)
    except LockBusy as e:
        raise TaskBusy(f"task is being processed by another run ({task_id}.lock)") from e


def _retry(task_id, answer=None):
    # Review (fourth round): retry() used to read/write the queue entry with no lock at all, unlike
    # process() - a retry's freshly-saved answer/constraint could be silently overwritten moments later by
    # an in-flight process() call's own finish(), which holds a copy of the entry read BEFORE the retry and
    # writes it back blindly, discarding the answer. Locked the same way process() is, so the two can never
    # interleave on the same task_id.
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


SECURITY_SHOWN = 10            # findings listed in the approval itself; the full report is always named
SECURITY_DESC_CHARS = 200
SECURITY_FIELD_CHARS = 80
SUMMARY_CHARS = 1500


def _cut(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n] + "..."


def _security_summary(task_id, sec_bytes):
    # A high/critical finding already stopped the task before it could reach review (loop.py's
    # security_call), so only low/medium findings - real but not blocking - can appear here. Surfaced to
    # the human approving the merge instead of being silently dropped once they stop blocking anything.
    # Phase 5 review: every line is bounded (round 2: category and file too) and the full report is named.
    if sec_bytes is None:
        return "Security review: no report (task predates Phase 5, or a prior error)."
    findings = json.loads(sec_bytes.decode("utf-8")).get("findings") or []
    if not findings:
        return "Security review: no findings."
    lines = []
    for f in findings[:SECURITY_SHOWN]:
        lines.append(f"- [{f['severity']}] {_cut(f['category'], SECURITY_FIELD_CHARS)} in {_cut(f['file'], SECURITY_FIELD_CHARS)}"
                     + (f":{f['line']}" if f.get("line") else "") + f" - {_cut(f['description'], SECURITY_DESC_CHARS)}")
    if len(findings) > SECURITY_SHOWN:
        lines.append(f"- ... and {len(findings) - SECURITY_SHOWN} more")
    return (f"Security review: {len(findings)} finding(s), none blocking. Full report: tasks/{task_id}/security_report.json\n"
            + "\n".join(lines))


def _base(ws):
    return loop._git(ws, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]


def _merge_request(task_id, tdir):
    """The merge approval's context AND its identity, built from ONE read of each input. Phase 5 review
    (round 2): the context and the identity read the security report separately, so a report swapped in
    between gave an approval that SHOWED one report and was bound to another.

    The identity covers the workspace commit, the requirement version, and the security report: any of them
    changing (e.g. `orch.py retry --answer`) forces a fresh human approval instead of reusing an old one.
    Security comes FIRST in the context, then a bounded summary, then the (possibly long) file list - round 2:
    a long plan summary pushed the whole security section, report path included, past the 4000-char cut."""
    ws = tdir / "workspace"
    ws_sha = loop._head(ws)
    prog_path = tdir / "progress.json"
    req_hash = json.loads(prog_path.read_text(encoding="utf-8")).get("req_hash") if prog_path.exists() else None
    sec = tdir / "security_report.json"
    sec_bytes = sec.read_bytes() if sec.exists() else None
    sec_hash = hashlib.sha256(sec_bytes).hexdigest()[:16] if sec_bytes is not None else "none"
    summary = (tdir / "summary.md").read_text(encoding="utf-8") if (tdir / "summary.md").exists() else ""
    stat = loop._git(ws, "diff", "--stat", f"{_base(ws)}..{ws_sha}", "--", ".", *JUNK).stdout.strip()
    context = (f"{_security_summary(task_id, sec_bytes)}\n\n{_cut(summary, SUMMARY_CHARS)}\n\nChanged files:\n{stat}\n\n"
               f"Approving prepares patch files (and a branch `agentsys/{task_id}` if a target repo was given). Nothing is merged into main.")
    return context, f"git:{ws_sha}:req:{req_hash}:sec:{sec_hash}"


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
        ws_sha = loop._git(ws, "rev-parse", "HEAD").stdout.strip()
        # "ours" means OUR journal, for this exact target_repo+branch, says we are the ones who made it -
        # never inferred just from the branch existing. Without this, deliver() would delete a same-named
        # branch it never created (e.g. the user's own unrelated work).
        ours = bool(journal) and journal.get("target_repo") == str(repo) and journal.get("branch") == branch
        branch_exists = _branch_exists_in(repo, branch)
        branch_sha = loop._git(repo, "rev-parse", branch).stdout.strip() if branch_exists else None
        # The journal proves we created this branch, not that its CURRENT content is still what we last
        # left it at: someone could commit more work onto it in the target repo directly. branch_sha (the
        # branch's own HEAD, recorded when we last wrote it) catches that drift in BOTH directions - it
        # must never silently count as "still a valid delivery" (ws_sha alone does not prove the branch
        # itself is untouched), and it must never be silently deleted as "our stale leftover" either.
        if ours and branch_exists and journal.get("branch_sha") and journal["branch_sha"] != branch_sha:
            raise RuntimeError(f"branch {branch} in {repo} has changed since this task last delivered to it "
                              f"(expected {journal['branch_sha'][:8]}, found {branch_sha[:8]}): not touching it. "
                              f"Move any work on it first, or delete it yourself for a fresh delivery.")
        if ours and journal.get("state") == "delivered" and journal.get("ws_sha") == ws_sha and branch_exists:
            result["branch"] = branch           # already delivered here before, for this EXACT content: nothing more to do
            return result
        if ours and journal.get("orig_branch"):
            # Trust the journal, not a fresh `rev-parse --abbrev-ref HEAD`: if the previous attempt crashed
            # right after `checkout -b branch` succeeded, current HEAD IS `branch` itself, and recomputing
            # from it would silently adopt the broken branch as its own "original".
            orig_branch = journal["orig_branch"]
        else:
            orig_branch = loop._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "HEAD"
            if orig_branch == branch:
                raise RuntimeError(f"{repo} is on {branch} with no record of how it got there: check it out to a safe branch yourself first")
        if branch_exists:
            if not ours:
                raise RuntimeError(f"branch {branch} already exists in {repo} and was not created by this task's delivery: not touching it")
            # Ours, matching what we last left it at (drift was already ruled out above): a leftover from a
            # crashed or failed attempt, safe to replace.
            current = loop._git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
            if current == branch and loop._git(repo, "checkout", "-q", orig_branch).returncode:
                raise RuntimeError(f"cannot check out {orig_branch} in {repo} to clean up the stale branch {branch}")
            loop._git(repo, "branch", "-D", branch)
        if loop._git(repo, "status", "--porcelain").stdout.strip():
            raise RuntimeError(f"target repo {repo} has uncommitted changes: not touching it")
        jpath.write_text(json.dumps({"target_repo": str(repo), "branch": branch, "orig_branch": orig_branch, "ws_sha": ws_sha, "state": "creating"}), encoding="utf-8")
        if loop._git(repo, "checkout", "-q", "-b", branch).returncode:
            raise RuntimeError(f"cannot create branch {branch} in {repo}")
        am = loop._git(repo, "am", "-q", *[str(out / p) for p in patches])
        if am.returncode:
            loop._git(repo, "am", "--abort")
            loop._git(repo, "checkout", "-q", orig_branch)      # leave the target repo where it was, not on the broken branch
            loop._git(repo, "branch", "-D", branch)
            raise RuntimeError(f"patches do not apply cleanly on {repo}: {am.stderr.strip()[:200]}")
        new_branch_sha = loop._git(repo, "rev-parse", branch).stdout.strip()
        jpath.write_text(json.dumps({"target_repo": str(repo), "branch": branch, "orig_branch": orig_branch,
                                      "ws_sha": ws_sha, "branch_sha": new_branch_sha, "state": "delivered"}), encoding="utf-8")
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
    backend = loop.resolve_backend(backend)          # before intake: intake is a real call too
    tdir = config.home() / "tasks" / task_id
    kw = dict(env=env, settings_paths=settings_paths)

    def finish(status, note="", **extra):
        entry.update(status=status, note=note, **extra)
        save_entry(entry)
        return entry

    def stopped_by_control():
        # Review (fourth round): control.read() was checked ONLY inside loop.py's agent() wrapper, i.e. only
        # when there was still an actual agent call left to make. A task sitting at an already-approved
        # merge gate with nothing left for the build loop to do (or one that hasn't even started intake
        # yet) ran straight through pause/stop untouched - including delivery itself, which a human setting
        # "stop" would reasonably expect to be prevented.
        ctl = control.read(task_id)
        if ctl == "pause":
            tasks.set_state(task_id, "paused", title=entry["title"])
            return finish("paused", "paused by user")
        if ctl == "stop":
            tasks.set_state(task_id, "blocked", title=entry["title"])
            return finish("stopped", "stopped by user")
        return None

    r = stopped_by_control()
    if r is not None:
        return r

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
        except runner.UsageLimitReached as e:
            # Same as the main build loop's own handling: "paused", not "blocked" (a human doesn't need to
            # decide anything), and a resume_after so the scheduler doesn't immediately retry the same call
            # in a tight loop. Previously unhandled here: this would crash _process() on every retry and
            # leave the queue entry stuck on "queued" forever, since "queued" was never set to anything else.
            tasks.set_state(task_id, "paused", title=entry["title"])
            return finish("paused", f"Usage limit reached during intake. State saved. Run the same command again later. ({e})",
                         resume_after=_now_plus(RESET_GUESS_S))
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
    context, diff_ref = _merge_request(task_id, tdir)
    ap = approvals.request(task_id, "merge", context, diff_ref=diff_ref)
    st = approvals.status(ap["id"])
    if st == "pending":
        tasks.set_state(task_id, "review", title=entry["title"], waiting_approval=True)
        return finish("waiting_approval", f"waiting for your decision on {ap['id']}")
    if st == "rejected":
        tasks.set_state(task_id, "blocked", title=entry["title"])
        return finish("rejected", "merge rejected by user")
    r = stopped_by_control()          # approved and about to touch a real target repo: check again right before
    if r is not None:
        return r
    try:
        d = deliver(task_id, entry["target_repo"])
    except RuntimeError as e:
        tasks.set_state(task_id, "blocked", title=entry["title"])
        return finish("blocked", f"delivery failed: {e}")
    # Phase 5: security now runs for real (loop.py, before this point ever gets reached - see
    # security_call()). "integrate" and "deploy" were retired as separate specialists: integration code is
    # just a normal work item for the developer, gated by network/credentials access rather than a role of
    # its own (Phase 6); "deploy" is already this function's own job (git branch/patch delivery).
    tasks.set_state(task_id, "done", title=entry["title"])
    logger.log_event(task_id, "orchestrator", "decision", "delivered", payload=d)
    return finish("done", f"delivered: {d['patches_dir']}" + (f", branch {d['branch']}" if d["branch"] else ""), delivery=d)


def _now_plus(seconds):
    import time
    return time.time() + seconds
