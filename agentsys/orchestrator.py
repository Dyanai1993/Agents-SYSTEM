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
import time
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


def project_check(path, ref=None):
    """A local git repository a task builds on: (top-level path, ref, commit sha), or ValueError with a plain reason.
    Master prompt follow-up: tasks used to start from an empty workspace, so a later task could not build on an
    earlier one's code. The agents never touch this repository - only a copy of one of its commits."""
    raw = str(path or "").strip().strip('"')
    if not raw:
        raise ValueError("give the full path of the project repository")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("give the FULL path of the project repository, e.g. C:\\Users\\you\\Projects\\FirstWords")
    if not p.is_dir():
        raise ValueError(f"{p} does not exist")
    if loop._git(p, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        raise ValueError(f"{p} is not a git repository (run `git init` there and make a first commit)")
    top = loop._git(p, "rev-parse", "--show-toplevel").stdout.strip() or str(p)
    ref = str(ref or "").strip() or loop._git(p, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not ref or ref.startswith("-") or any(c in ref for c in " \t\n\\:?*[~^"):
        raise ValueError(f"'{ref}' is not a branch name")
    sha = loop._git(p, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").stdout.strip()
    if not sha:
        raise ValueError(f"no commit at '{ref}' in {top}" + (" - the repository has no commits yet: make a first commit "
                         "(for example a README) so the agents have something to start from" if ref == "HEAD" else ""))
    return str(Path(top)), ref, sha


def _export_commit(repo, sha, dest):
    """The files of one commit (tracked files only, no .git, nothing untracked) into `dest`, for a task workspace."""
    import io
    import tarfile
    dest.mkdir(parents=True, exist_ok=True)
    if not loop._git(repo, "ls-tree", "--name-only", sha).stdout.strip():
        return dest                                      # an empty commit (git commit --allow-empty): nothing to copy
    r = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", sha], capture_output=True)
    if r.returncode:
        raise ValueError(f"cannot read commit {sha[:12]} of {repo}: {r.stderr.decode('utf-8', 'replace').strip()[:200]}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
        tf.extractall(dest, filter="data")               # refuses absolute paths and '..' in the archive
    return dest


def create_project_repo(task_id, title, goal=""):
    """A new local git repository for a new task (the owner's choice: one per task, unless an existing one is picked):
    <projects_dir>/<task>-<slug>, branch main, a README and a .gitignore in a first commit. Returns its path.
    Nothing is pushed anywhere; the delivery adds the branch agentsys/<task> here."""
    import re as _re
    logger.safe_task_id(task_id)
    slug = _re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:40].strip("-") or "project"
    root = config.projects_dir()
    root.mkdir(parents=True, exist_ok=True)
    path, n = root / f"{task_id}-{slug}", 2
    while path.exists():
        path, n = root / f"{task_id}-{slug}-{n}", n + 1
    path.mkdir()
    loop._git(path, "init", "-q")
    loop._git(path, "symbolic-ref", "HEAD", "refs/heads/main")          # main, whatever the local git default is
    (path / "README.md").write_text(f"# {title or task_id}\n\nCreated by Agents-SYSTEM for task {task_id}.\n\n{goal}\n", encoding="utf-8")
    (path / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\nnode_modules/\n.venv/\n.DS_Store\n",
                                     encoding="utf-8")
    loop._git(path, "add", "-A")
    if loop._git(path, "commit", "-q", "-m", f"Start: {title or task_id} ({task_id})").returncode:
        raise RuntimeError(f"could not make the first commit in {path}")
    return str(path)


def set_project(task_id, path, ref=None):
    """Set (or with an empty path, clear) the project repository of a task that has not started yet: its workspace
    is a copy taken at the start, so the choice cannot change afterwards."""
    task_id = logger.safe_task_id(task_id)
    with _Lock(task_id):
        e = load_entry(task_id)
        if e is None:
            raise KeyError(f"no task {task_id}")
        if (config.home() / "tasks" / task_id / "workspace" / ".git").exists():
            raise ValueError("this task already started from its own workspace: set the project repository on a new task")
        if str(path or "").strip():
            repo, ref, sha = project_check(path, ref)
            e.update(target_repo=repo, base_ref=ref)
            note = f"project repository {repo} @ {ref} ({sha[:10]})"
        else:
            e.update(target_repo=None, base_ref=None)
            note = "no project repository: it starts empty and delivers patch files only"
        save_entry(e)
        logger.log_event(task_id, "human", "decision", note, status="ok")
        return e


def submit(task_id, *, request_text=None, requirements=None, title=None, target_repo=None, priority=5, origin=None, base_ref=None):
    logger.safe_task_id(task_id)
    if load_entry(task_id):
        raise ValueError(f"task {task_id} already exists")
    if bool(request_text) == bool(requirements):
        raise ValueError("give exactly one of request_text or requirements")
    if requirements is not None:
        schema.validate("requirements", requirements)
    entry = {"task_id": task_id, "title": title or tasks.short_title((requirements or {}).get("goal") or request_text), "status": "queued",
             "priority": priority, "created_at": logger.now(), "request_text": request_text, "requirements": requirements,
             "target_repo": str(target_repo) if target_repo else None, "base_ref": base_ref or None, "resume_after": None, "note": "",
             "origin": origin}                 # e.g. {"draft_session": "D-0001", "revision": 3}: the Requirements Assistant
    save_entry(entry)
    tasks.set_state(task_id, "intake", title=entry["title"])
    logger.log_event(task_id, "orchestrator", "state_change", "task queued", status="ok")
    return entry


# Build junk is never delivered, even when an older workspace committed it before the workspace .gitignore existed.
JUNK = [":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.pyc", ":(exclude,glob)**/.pytest_cache/**",
        ":(exclude,glob)**/.mypy_cache/**", ":(exclude,glob)**/.ruff_cache/**", ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/.venv/**", ":(exclude,glob)**/.DS_Store"]

RETRYABLE = {"needs_input", "needs_approval", "blocked", "paused", "queued", "stopped"}   # stop is reversible: resume = retry


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
    if answer and in_requirement_phase(entry) and entry["status"] in REQUIREMENT_PHASE_STATUSES and intake.draft(task_id):
        # Before approval the answer changes the DRAFT, not the requirement: it used to be written straight into an
        # unapproved requirement and built from - a gate bypass. The changed draft needs approval again (its hash
        # changed). No model call: the clarification is added as a constraint, exactly as after approval.
        answer = " ".join(answer.split())[:300]
        req = intake.draft(task_id)
        req = dict(req, constraints=list(req["constraints"]) + [f"Clarification from the user: {answer}"],
                   unknowns=[dict(u, blocking=False) for u in req["unknowns"]])
        schema.validate("requirements", req)
        intake.save_draft(task_id, req)
        ap = approvals.request(task_id, "requirements", intake.gate_context(task_id, req), diff_ref=intake.draft_ref(task_id))
        entry.update(status="waiting_approval", resume_after=None, note=f"your answer was added to the draft: approve or reject it ({ap['id']})")
        save_entry(entry)
        tasks.set_state(task_id, "intake", title=entry["title"], waiting_approval=True)
        logger.log_event(task_id, "human", "decision", "answer added to the requirement draft", status="ok")
        return entry
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


def approved_sha(diff_ref):
    """The workspace commit a merge approval is bound to ('git:<sha>:req:...:sec:...'), or None."""
    parts = str(diff_ref or "").split(":")
    ok = len(parts) > 1 and parts[0] == "git" and 40 <= len(parts[1]) <= 64 and all(c in "0123456789abcdef" for c in parts[1])
    return parts[1] if ok else None


def deliver(task_id, target_repo=None, commit=None, approval_id=None, branch_from=None):
    """Export the workspace history up to `commit` (the APPROVED commit; HEAD when not given) as patches;
    optionally apply them on a NEW branch of a target repo. Returns the delivery receipt.

    Master prompt, phase B: the patches are cut from the commit the human approved, never from whatever HEAD is
    at delivery time. And the folder holds exactly this delivery's patches: they are written to a fresh folder
    first, then replace the old set (stale patches from an earlier delivery used to be listed AND applied).

    Idempotent and self-cleaning: a crash or process failure (ours or git's) must never leave the target
    repo checked out on a half-finished branch that blocks every later retry, and re-running after a
    successful delivery must not try to recreate the branch and fail.
    """
    import shutil
    tdir = config.home() / "tasks" / task_id
    ws = tdir / "workspace"
    out = config.home() / "deliveries" / task_id
    out.mkdir(parents=True, exist_ok=True)
    ws_sha = commit or loop._git(ws, "rev-parse", "HEAD").stdout.strip()
    if loop._git(ws, "cat-file", "-e", f"{ws_sha}^{{commit}}").returncode:
        raise RuntimeError(f"the approved commit {ws_sha[:12]} is not in the workspace")
    fresh = out / f".new-{time.strftime('%Y%m%d-%H%M%S')}"
    r = loop._git(ws, "format-patch", "-q", "-o", str(fresh), f"{_base(ws)}..{ws_sha}", "--", ".", *JUNK)
    new = sorted(fresh.glob("*.patch")) if fresh.is_dir() else []
    if r.returncode or not new:
        shutil.rmtree(fresh, ignore_errors=True)
        raise RuntimeError(f"nothing to deliver: {r.stderr.strip() or 'no commits'}")
    for old in out.glob("*.patch"):
        old.unlink()
    for p in new:
        p.replace(out / p.name)
    shutil.rmtree(fresh, ignore_errors=True)
    patches = [p.name for p in new]
    result = {"patches_dir": str(out), "patches": patches, "branch": None, "commit": ws_sha,
              "approval_id": approval_id, "delivered_at": logger.now()}
    jpath = _delivery_journal(tdir)
    journal = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else None
    if target_repo:
        repo = Path(target_repo)
        if not repo.is_dir():
            raise RuntimeError(f"target repo {repo} does not exist")
        if loop._git(repo, "rev-parse", "--is-inside-work-tree").returncode:
            raise RuntimeError(f"target repo {repo} is not a git repository")
        branch = f"agentsys/{task_id}"
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
        # A task that started from this repository branches from the exact commit its workspace was copied from, so its
        # patches apply cleanly even if the repository moved on since; otherwise from the current HEAD.
        start = [branch_from] if branch_from and not loop._git(repo, "cat-file", "-e", f"{branch_from}^{{commit}}").returncode else []
        if loop._git(repo, "checkout", "-q", "-b", branch, *start).returncode:
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


def _requirement_phase(task_id, entry, tdir, backend, kw, finish):
    """Run the intake if no draft exists yet, then stop at the "requirements" gate until the human decides.
    Returns a finished entry (stop here) or None (approved: build from intake.draft())."""
    if not (tdir / "requirements.json").exists():
        Ledger(task_id).begin_run()       # a retried intake attempt gets its own fresh run clock, like the build loop
        try:
            _, err = intake.run_intake(task_id, entry["request_text"], backend=backend, **kw)
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
    req = intake.draft(task_id)
    ap = approvals.request(task_id, "requirements", intake.gate_context(task_id, req), diff_ref=intake.draft_ref(task_id))
    st = approvals.status(ap["id"])
    if st == "pending":
        tasks.set_state(task_id, "intake", title=entry["title"], waiting_approval=True)
        q = intake.blocking_questions(req)
        return finish("waiting_approval", "review the requirement draft: approve or reject it"
                      + (f" - blocking question(s): {' | '.join(q)}" if q else "") + f" ({ap['id']})")
    if st == "rejected":
        tasks.set_state(task_id, "blocked", title=entry["title"])
        return finish("rejected", "requirement rejected by user")
    logger.log_event(task_id, "orchestrator", "decision", f"requirement approved ({ap['id']}): building from it", status="ok")
    return None


REQUIREMENT_PHASE_STATUSES = {"waiting_approval", "queued", "blocked", "needs_input", "paused"}


def in_requirement_phase(entry):
    return entry is not None and entry.get("requirements") is None and bool(entry.get("request_text"))


def next_task_id():
    """The next free T-#### id (dashboard submissions). A deleted task's id is never reused: its trash folder and its
    deliveries still carry it."""
    import re as _re
    h = config.home()
    nums = [int(m.group(1)) for d in (_qdir(), h / "tasks", h / "trash", h / "deliveries") if d.is_dir()
            for p in d.iterdir() if (m := _re.fullmatch(r"T-(\d{4})(\.json|-.+)?", p.name))]
    return f"T-{max(nums, default=0) + 1:04d}"


def requirement_edit_blocker(task_id, entry=None):
    """Why the human cannot edit this task's requirement in place now (None = they can). Only before anything is built:
    a new scope after real work is a new task, not a silent rewrite of the one already built against."""
    entry = entry or load_entry(task_id)
    if entry is None:
        return "no such task"
    if entry["status"] in ("done", "rejected"):
        return f"the task is {entry['status']}"
    if in_requirement_phase(entry):
        return "the requirement is still a draft waiting for approval: change it there"
    tdir = config.home() / "tasks" / task_id
    prog = json.loads((tdir / "progress.json").read_text(encoding="utf-8")) if (tdir / "progress.json").exists() else {}
    if prog.get("completed"):
        return "work items were already built against this requirement: start a new task for a new scope"
    ws = tdir / "workspace"
    if (ws / ".git").exists():
        if loop._git(ws, "status", "--porcelain").stdout.strip():
            return "the workspace has changes from a started work item: start a new task for a new scope"
        if loop._git(ws, "rev-list", "--count", f"{_base(ws)}..HEAD").stdout.strip() not in ("", "0"):
            return "the workspace already has commits: start a new task for a new scope"
    return None


def edit_requirement(task_id, req):
    """The human narrows or corrects the requirement of a task that has built nothing yet. Seen live (T-0025): the
    brief was too big for one task; an answer can only ADD a clarifying constraint, never remove a criterion, so the
    Architect asked the same question round after round. Saving makes a new revision (the old one is kept in
    requirement_history.json), forgets the planning progress so the Architect plans the new revision from scratch,
    and puts the task back in the queue: press Continue. Calls already spent stay counted (the task budget)."""
    schema.validate("requirements", req)
    task_id = logger.safe_task_id(task_id)
    with _Lock(task_id):
        e = load_entry(task_id)
        if e is None:
            raise KeyError(f"no task {task_id}")
        why = requirement_edit_blocker(task_id, e)
        if why:
            raise ValueError(f"cannot edit the requirement: {why}")
        tdir = config.home() / "tasks" / task_id
        hist_path = tdir / "requirement_history.json"
        hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.exists() else []
        rev = int(e.get("req_revision") or 1) + 1
        hist.append({"revision": rev - 1, "replaced_at": logger.now(), "requirement": e["requirements"]})
        tdir.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(json.dumps(hist, indent=2, ensure_ascii=False), encoding="utf-8")
        for f in ("progress.json",):                    # nothing was built: plan the new revision from scratch
            try:
                (tdir / f).unlink()
            except OSError:
                pass
        e.update(requirements=req, req_revision=rev, resume_after=None,
                 status="queued" if e["status"] not in ("waiting_approval", "needs_approval") else e["status"],
                 note=f"requirement revision {rev} saved by you: press Continue to plan it")
        save_entry(e)
        tasks.set_state(task_id, "plan" if (tdir / "plan.json").exists() else "intake", title=e["title"])
        logger.log_event(task_id, "human", "decision", f"requirement edited: revision {rev}", status="ok",
                         payload={"revision": rev, "acceptance_criteria": len(req["acceptance_criteria"])})
    from . import worker
    worker.cancel_run(task_id)
    return e


def apply_control(task_id, action):
    """Pause / stop / resume, as the user means it. control.json is always written (the loop reads it before every
    agent call). When no run holds the task, the status changes NOW - seen live: a stopped task still said 'queued'
    until the next run. While a run holds it, the request is pending and takes effect before the next agent call.
    Stop is reversible: resume puts a stopped task back in the queue, continuing from its checkpoint."""
    if action not in ("pause", "stop", "resume"):
        raise ValueError("action must be pause, stop or resume")
    task_id = logger.safe_task_id(task_id)
    if load_entry(task_id) is None:
        raise KeyError(f"no task {task_id}")
    control.set_control(task_id, action)
    try:
        with _Lock(task_id):
            e = load_entry(task_id)
            st, new = e["status"], None
            if action == "stop" and st not in ("done", "rejected", "stopped"):
                new = dict(status="stopped", note="stopped by you: Resume continues from the last checkpoint", resume_after=None)
            elif action == "pause" and st == "queued":
                new = dict(status="paused", note="paused by you: Resume to continue", resume_after=None)
            elif action == "resume" and (st == "stopped" or (st == "paused" and (e.get("resume_after") or 0) <= time.time())):
                new = dict(status="queued", note="resumed by you: continues from the last checkpoint", resume_after=None)
            if new:
                e.update(new)
                save_entry(e)
                logger.log_event(task_id, "human", "decision", f"{action}: status is now {new['status']}", status="ok")
            from . import worker                       # a stopped/paused task must not start from an older request,
            req = worker.get_request(task_id)          # and after Resume an old refusal ("it is paused") is stale
            if action in ("stop", "pause") or (req and req.get("status") == "refused"):
                worker.cancel_run(task_id)
            return {"task_id": task_id, "action": action, "applied": True, "status": e["status"],
                    "note": e.get("note", "")}
    except TaskBusy:
        return {"task_id": task_id, "action": action, "applied": False, "status": load_entry(task_id)["status"],
                "note": f"{action} requested: a run is working on this task, it takes effect before its next agent call"}


def delete_task(task_id):
    """Take a task off the queue, the dashboard and the approvals list. Recoverable: everything is MOVED, not erased,
    to trash/<task>-<time>/ (the task folder with its workspace, its approval files, its queue entry). Refused while a
    run is processing it (TaskBusy). Kept: deliveries you already received and the usage history (state/window.json)."""
    import shutil
    import time
    task_id = logger.safe_task_id(task_id)
    h = config.home()
    with _Lock(task_id):                               # no run can start or continue on it while we move it
        q, tdir = _qdir() / f"{task_id}.json", h / "tasks" / task_id
        if not q.exists() and not (tdir / "state.json").exists():   # a task, not e.g. a draft session's ledger folder
            raise KeyError(f"no task {task_id}")
        dest = h / "trash" / f"{task_id}-{time.strftime('%Y%m%d-%H%M%S')}"
        dest.mkdir(parents=True)
        if tdir.exists():                              # first: the step most likely to fail (a file open on Windows),
            try:                                       # and if it does, nothing else has changed yet
                shutil.move(str(tdir), str(dest / "task"))
            except OSError as e:
                dest.rmdir()
                raise RuntimeError(f"could not move tasks/{task_id} (is a file in it open?): {e}") from e
        for kind in ("pending", "decided"):
            for p in (h / "approvals" / kind).glob(f"{task_id}-*.json"):
                (dest / "approvals" / kind).mkdir(parents=True, exist_ok=True)
                p.replace(dest / "approvals" / kind / p.name)
        if q.exists():
            q.replace(dest / "queue.json")
    from . import worker
    worker.cancel_run(task_id)                         # a pending Start/Continue for a deleted task is void
    return {"task_id": task_id, "moved_to": str(dest)}


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

    # ---- requirement (only for a plain-language request): intake drafts it, the human approves it or replies
    # and intake revises it. Nothing is built from an unapproved draft. entry["requirements"] is set only once
    # approved (a structured requirement given at submit needs no approval: the human wrote it).
    req = entry["requirements"]
    if req is None:
        r = _requirement_phase(task_id, entry, tdir, backend, kw, finish)
        if r is not None:
            return r
        req = intake.draft(task_id)
    entry["requirements"] = req

    # ---- a project repository: the workspace starts as a copy of its chosen commit (once, at the first run)
    seed = None
    if entry.get("target_repo") and not (tdir / "workspace" / ".git").exists():
        import shutil
        try:
            repo, ref, sha = project_check(entry["target_repo"], entry.get("base_ref"))
            shutil.rmtree(tdir / ".seed", ignore_errors=True)
            seed = _export_commit(repo, sha, tdir / ".seed")
        except ValueError as e:
            tasks.set_state(task_id, "blocked", title=entry["title"])
            return finish("blocked", f"project repository: {e}")
        entry["project_base"] = {"repo": repo, "ref": ref, "sha": sha}
        save_entry(entry)
        logger.log_event(task_id, "orchestrator", "decision", f"workspace starts from {repo} @ {ref} ({sha[:10]})", status="ok")

    # ---- core loop (resumes from progress.json)
    try:
        res = loop.run_task(task_id, req, backend=backend, title=entry["title"], seed=seed, **kw)
    finally:
        if seed is not None:
            import shutil
            shutil.rmtree(seed, ignore_errors=True)
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
        d = deliver(task_id, entry["target_repo"], commit=approved_sha(ap.get("diff_ref")), approval_id=ap["id"],
                    branch_from=(entry.get("project_base") or {}).get("sha"))
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
