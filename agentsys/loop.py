"""Core loop (Phase 2): requirement -> architect -> (developer -> tester -> fix loop) per work item -> final regression.

Deterministic code decides every transition. Agents only fill documents. Rules the prompts ask for
are enforced in code (agentsys/semantics.py): coverage of requirements, verdict derivation, evidence vs tool log.

Checkpoint: tasks/<id>/progress.json is written after every completed step. Re-running run_task()
with the same task id resumes at the step that did not finish. Ends at state 'review'.

Git policy in the task workspace:
  * one commit per successful developer run  ("W1: title")
  * one commit per tester run that created qa_tests/ files ("qa: W1")
  * a blocked/failed developer run is NOT committed (changes stay in the working tree, listed in the status report)
  * qa_tests/ is protected: developer edits to it are reverted; tester edits to product files invalidate the report
"""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from . import approvals, billing_guard, config, control, logger, memory, runner, schema, semantics, tasks
from .ledger import BudgetExceeded, Ledger, Window, estimate_task_units

QA_DIR = "qa_tests"


class StepFailed(RuntimeError):
    pass


class NeedsInput(RuntimeError):
    """A human must answer. Used only for missing information, never for budget or environment problems."""


class Paused(RuntimeError):
    """The user asked to pause (control file). Progress is saved at the last finished step."""


class Stopped(RuntimeError):
    """The user asked to stop (control file)."""


class EnvBlocked(RuntimeError):
    """The environment cannot do the work (missing tool, no network...). A human must fix the setup."""


def _save(path, doc):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _git(ws, *args, check=False):
    cmd = ["git", "-c", "user.name=agentsys", "-c", "user.email=agentsys@localhost", *args]
    return subprocess.run(cmd, cwd=ws, capture_output=True, text=True, check=check)


def ensure_workspace(tdir, seed=None):
    """Create the task workspace (a git repo). `seed` = folder copied in BEFORE the first commit (eval fixtures)."""
    ws = tdir / "workspace"
    if not (ws / ".git").exists():
        ws.mkdir(parents=True, exist_ok=True)
        if seed:
            import shutil
            shutil.copytree(seed, ws, dirs_exist_ok=True)
        gi = ws / ".gitignore"                      # build junk never becomes part of a delivery
        if not gi.exists():
            gi.write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.mypy_cache/\n.ruff_cache/\nnode_modules/\n.venv/\n.DS_Store\n", encoding="utf-8")
        _git(ws, "init", "-q", check=True)
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "--allow-empty", "-m", "workspace start", check=True)
    return ws


def _git_ok(ws, *args, env=None):
    """A git call whose answer DECIDES something: a failure raises instead of reading as 'no changes'.
    Phase 5 review (round 2): a failed `git status` returned '' and passed as a clean workspace."""
    cmd = ["git", "-c", "user.name=agentsys", "-c", "user.email=agentsys@localhost", *args]
    r = subprocess.run(cmd, cwd=ws, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise StepFailed(f"git {' '.join(args[:3])} failed in {ws}: {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def _changed(ws):
    """Every changed path, both sides of a rename or copy. -z: no quoting, no ' -> ' joined paths.
    Phase 5 review (round 2): 'R  qa_tests/q.py -> app.py' was read as ONE path inside qa_tests/."""
    parts = _git_ok(ws, "status", "--porcelain=v1", "-z", "--untracked-files=all").split("\0")
    files, i = [], 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        files.append(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:      # the next field is the source path
            files.append(parts[i])
            i += 1
    return files


def _is_qa(path):
    return path.replace("\\", "/").startswith(QA_DIR + "/")


def _head(ws):
    sha = _git_ok(ws, "rev-parse", "HEAD").strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise StepFailed(f"cannot read HEAD in {ws}")
    return sha


def _worktree_id(ws):
    """A git tree id of the workspace AS IT IS, uncommitted changes included (a throwaway index, so the real
    index is never touched). Equal ids = the same content. Every step is checked: a failed `add -A` after a
    good `read-tree` would otherwise return a valid id of the OLD content."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        env = dict(os.environ, GIT_INDEX_FILE=os.path.join(d, "index"))
        _git_ok(ws, "read-tree", "HEAD", env=env)
        _git_ok(ws, "add", "-A", env=env)
        tree = _git_ok(ws, "write-tree", env=env).strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", tree):
        raise StepFailed(f"cannot compute the workspace content id in {ws}")
    return tree


def _tree_diff(ws, a, b):
    """Paths whose content differs between two tree ids. --no-renames: a move lists BOTH paths."""
    if a == b:
        return []
    return [p for p in _git_ok(ws, "diff-tree", "-r", "--no-renames", "--name-only", "-z", a, b).split("\0") if p]


def _restore(ws, head):
    """Back to `head` exactly: commits, index and files. Only ever called on a workspace that was CLEAN at
    `head` before the agent ran (guarded_call checks that), so this removes the agent's work and nothing else."""
    _git(ws, "reset", "-q", "--hard", head)
    _git(ws, "clean", "-fdq")


def protect_qa(ws, task_id):
    """Developer must not touch qa_tests/. Revert any change and raise an alert."""
    bad = [f for f in _changed(ws) if _is_qa(f)]
    if bad:
        # Same staging gap protect_config had: `checkout -- QA_DIR` restores from the INDEX, not HEAD, so a
        # weakened test the developer already `git add`ed survives as a no-op and gets committed anyway.
        # Unstage first so the index itself is back to HEAD before restoring the working tree from it.
        _git(ws, "reset", "-q", "--", QA_DIR)
        _git(ws, "checkout", "-q", "--", QA_DIR)
        _git(ws, "clean", "-fdq", "--", QA_DIR)
        logger.log_event(task_id, "orchestrator", "alert", f"developer changed protected {QA_DIR}/: reverted", status="blocked",
                         payload={"files": bad})
    return bad


def _config_leak_paths(ws):
    """Walk the real filesystem for a top-level .claude/ or any CLAUDE.md, ignoring git status.

    A user's global git ignore commonly excludes .claude/settings.local.json (the recommended
    setup to avoid committing personal Claude Code settings), which makes `git status` blind to
    it. Scanning the filesystem directly means the guard still catches it.

    Review (fourth round): if an agent makes .claude (or any directory under the workspace) a SYMLINK to
    somewhere outside it, `Path.is_dir()`/`rglob()` follow the link transparently - they would enumerate
    the EXTERNAL target's real files and hand back paths that only LOOK like they are inside the workspace
    (e.g. ".claude/secret.txt"), because pathlib builds those paths by joining name components, never by
    resolving the symlink. protect_config() then happily "removes the leak" by unlinking that path - which
    the OS resolves through the symlink for real, deleting the EXTERNAL file. A symlinked .claude is
    therefore never followed at all: it is itself the whole leak, removed as a single entry (unlinking a
    symlink never touches whatever it points to). The same applies to any OTHER symlinked subdirectory
    found while walking for a stray CLAUDE.md: never descend into one.
    """
    bad = []
    cfg = ws / ".claude"
    if os.path.islink(cfg):
        bad.append(".claude")                    # the symlink itself is the leak; never follow it to look inside
    elif cfg.is_dir():
        found = False
        for root, dirs, files in os.walk(cfg):
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for fn in files:
                p = Path(root) / fn
                if os.path.islink(p):
                    continue                     # a symlinked "file" entry is handled as its own leak below if it matters
                bad.append(p.relative_to(ws).as_posix())
                found = True
        if not found:
            bad.append(".claude")               # empty .claude/ with no files inside
    elif cfg.exists():
        bad.append(".claude")                    # not a directory (unexpected, but still a leak)
    for root, dirs, files in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in (".git", ".claude") and not os.path.islink(os.path.join(root, d))]
        rel_root = os.path.relpath(root, ws).replace("\\", "/")
        prefix = "" if rel_root == "." else rel_root + "/"
        for fn in files:
            if fn.lower().endswith("claude.md"):
                bad.append(prefix + fn)
    return bad


def protect_config(ws, task_id, who):
    """No agent may create or change .claude/ or CLAUDE.md in the workspace: Claude Code would read them as settings."""
    bad = _config_leak_paths(ws)
    if bad:
        for f in bad:
            path = ws / f
            if os.path.islink(path):
                # Remove the LINK itself, never its target (which _config_leak_paths never followed to
                # begin with - see its docstring). os.unlink fails on a Windows directory-symlink/junction;
                # os.rmdir removes the reparse point there without touching whatever it points to.
                _git(ws, "reset", "-q", "--", f)
                try:
                    os.unlink(path)
                except OSError:
                    try:
                        os.rmdir(path)
                    except OSError:
                        pass
                continue
            # `git ls-files` lists INDEX entries, not HEAD: a file the agent just `git add`ed is "tracked" by
            # that check, and `checkout -- f` would only restore it from the (still bad) index - a no-op that
            # leaves it staged, so the next commit_changes() call commits it anyway. Unstage unconditionally,
            # then restore from HEAD only if it safely existed there; otherwise delete it outright.
            existed_at_head = _git(ws, "cat-file", "-e", f"HEAD:{f}").returncode == 0
            _git(ws, "reset", "-q", "--", f)
            if existed_at_head:
                _git(ws, "checkout", "-q", "HEAD", "--", f)
            elif path.is_dir():
                import shutil
                shutil.rmtree(path, ignore_errors=True)
            else:
                path.unlink(missing_ok=True)
        cfg = ws / ".claude"
        if not os.path.islink(cfg) and cfg.is_dir() and not any(cfg.rglob("*")):
            import shutil
            shutil.rmtree(cfg, ignore_errors=True)               # no empty settings folder is left behind
        logger.log_event(task_id, "orchestrator", "alert", f"{who} wrote Claude settings in the workspace: removed", status="blocked", payload={"files": bad})
    return bad


def commit_changes(ws, message, only=None):
    """Commit exactly `files` (`-- paths` = --only: anything else already staged stays out of this commit).
    Phase 5 review (round 2): a failed `git add` on a bad path left the index as the agent staged it, and a
    commit without paths committed that instead."""
    files = [f for f in _changed(ws) if only is None or only(f)]
    if files:
        _git_ok(ws, "add", "-A", "--", *files)
        _git_ok(ws, "commit", "-q", "-m", message[:100], "--", *files)
    return files


def _load_progress(tdir):
    # Merged over the defaults (not returned as-is) so a progress.json saved by an older version of this
    # code - missing a key added since, like "security_done" in Phase 5 - resumes instead of a KeyError
    # the first time that key is read.
    defaults = {"plan_done": False, "item": 0, "step": "dev", "fix": 0, "last_report": None, "runs": 0, "replans": 0,
                "reports": 0, "completed": {}, "final_done": False, "security_done": False, "final_tree": None,
                "security_tree": None, "last_files": [], "req_hash": None}
    p = tdir / "progress.json"
    if p.exists():
        defaults.update(json.loads(p.read_text(encoding="utf-8")))
    return defaults


def _ids(req):
    ac = {f"AC{i + 1}": t for i, t in enumerate(req["acceptance_criteria"])}
    cons = {f"C{i + 1}": t for i, t in enumerate(req["constraints"])}
    return ac, cons


def _item_hash(wi):
    return hashlib.sha256(json.dumps(wi, sort_keys=True).encode()).hexdigest()[:12]


UNVERIFIABLE_BACKENDS = {"sdk"}      # builds no tool log (live-verified): every tester report would fail closed


def resolve_backend(backend):
    """The backend every call of this task will use, checked BEFORE the first call. Phase 5 review: `--backend
    sdk` spent real calls before the tester failed closed on the missing tool log; round 2: backend=None was
    passed through unchecked and resolved later from AGENTSYS_BACKEND. live_smoke.py keeps sdk."""
    backend = backend or runner.get_backend()
    if getattr(backend, "name", None) in UNVERIFIABLE_BACKENDS:
        raise ValueError(f"backend '{backend.name}' builds no tool log, so no tester report can be verified: use the cli backend")
    return backend


def run_task(task_id, requirements, *, backend, title=None, env=None, settings_paths=None, limits=None, seed=None):
    """Run or resume one task. Returns {"status", "state", "message", "report"}."""
    logger.safe_task_id(task_id)
    backend = resolve_backend(backend)              # the caller's bug, like bad requirements: raise before any call
    tdir = config.home() / "tasks" / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    schema.validate("requirements", requirements)   # bad input is the caller's bug: raise, do not log a task failure
    title = title or tasks.short_title(requirements["goal"])
    Ledger(task_id).begin_run()                    # the timeout clock restarts with every run, not with the task
    try:
        _run(task_id, tdir, requirements, backend, title, env, settings_paths, limits, seed)
        status, state, msg = "ready_for_review", "review", "All work items and the final regression passed. Waiting for your review."
    except runner.UsageLimitReached as e:
        status, state, msg = "paused", "paused", f"Usage limit reached. State saved. Run the same command again later. ({e})"
    except Paused as e:
        status, state, msg = "paused", "paused", f"{e}. Resume from the dashboard or `orch.py resume`."
    except Stopped as e:
        status, state, msg = "stopped", "blocked", str(e)
    except approvals.OpusNotApproved as e:
        status, state, msg = "needs_approval", "blocked", f"{e}. Approve it in the dashboard, then run the task again."
    except NeedsInput as e:
        status, state, msg = "needs_input", "blocked", str(e)
    except EnvBlocked as e:
        status, state, msg = "blocked", "blocked", f"Environment problem (not a question for the user): {e}"
    except BudgetExceeded as e:
        status, state, msg = "blocked", "blocked", f"Limit hit: {e.reason}. I stopped and need your decision."
    except billing_guard.BillingGuardError as e:
        status, state, msg = "blocked", "blocked", str(e)
    except (StepFailed, schema.SchemaError) as e:
        status, state, msg = "blocked", "blocked", f"Step failed: {e}"
    report = None
    if status != "ready_for_review":
        tasks.set_state(task_id, state, title=title, current_agent=None)
        report = _status_report(tdir, task_id, status, msg)
    logger.log_event(task_id, "orchestrator", "decision", f"task ends: {status}",
                     status="ok" if status in ("ready_for_review", "paused") else "blocked", payload={"status": status, "message": msg})
    return {"status": status, "state": state, "message": msg, "report": str(report) if report else None}


def _status_report(tdir, task_id, status, msg):
    led = Ledger(task_id).data
    prog = _load_progress(tdir)
    ws = tdir / "workspace"
    try:                                            # a report must never raise over the error it reports
        dirty = _changed(ws) if (ws / ".git").exists() else []
    except StepFailed:
        dirty = ["(unknown: git status failed)"]
    p = tdir / "status_report.md"
    p.write_text(
        f"# Status report: {task_id}\n\nStatus: **{status}**\n\n{msg}\n\n"
        f"- agent calls: {led['calls']}/{led['call_cap']}\n- fix loops: {led['fix_loops']}/{led['fix_cap']}\n"
        f"- re-plans: {led['replans']}/{led['replan_cap']}\n- completed items: {', '.join(prog['completed']) or 'none'}\n"
        f"- current step: {prog['step']}\n- limit hits: {len(led['limit_hits'])}\n"
        f"- uncommitted files in workspace: {', '.join(dirty) or 'none'}\n\n"
        f"See `logs/{task_id}.jsonl` and `scripts/timeline.py`. Checkpoint: `tasks/{task_id}/progress.json`.\n", encoding="utf-8")
    return p


def _run(task_id, tdir, req, backend, title, env, settings_paths, limits, seed=None):
    ac, cons = _ids(req)
    blocking = [u["question"] for u in req["unknowns"] if u["blocking"]]
    if blocking:                                    # no agent call needed to know a human must answer
        raise NeedsInput("Blocking unknowns in the requirement: " + " | ".join(blocking))
    ws = ensure_workspace(tdir, seed)
    prog = _load_progress(tdir)
    req_hash = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()[:16]
    if prog.get("req_hash") != req_hash:
        # The requirement changed since the last run that reached this point (e.g. `orch.py retry --answer`
        # added a constraint after the final regression had already passed). Completed work items were never
        # checked against the new requirement, so the final regression must run again before review.
        # The security review looks at the same finished workspace, so new commits (a fresh final regression
        # can trigger more developer work) invalidate it exactly the same way.
        prog["final_done"] = False
        prog["security_done"] = False
        prog["req_hash"] = req_hash
    # A guarded call whose changes could not be reverted leaves this marker. Nothing runs until the workspace
    # is back to the state from before that call (checked here, not trusted) - or a human deletes the marker
    # to keep the workspace as it is, deliberately. Phase 5 review (round 2): a later retry used to commit
    # the leftovers as if they were a human's fix.
    tainted = tdir / "workspace_tainted.json"
    if tainted.exists():
        t = json.loads(tainted.read_text(encoding="utf-8"))
        if _head(ws) == t["head"] and not _changed(ws):
            tainted.unlink()
            logger.log_event(task_id, "orchestrator", "decision", "workspace restored after a failed revert: continuing", status="ok")
        else:
            raise StepFailed(f"the workspace still holds changes a {t['role']} call made and could not revert. Restore it: "
                             f"`git -C tasks/{task_id}/workspace reset --hard {t['head'][:12]}` and `git clean -fd`, "
                             f"or delete tasks/{task_id}/workspace_tainted.json to keep it as it is. Then retry")
    # Phase 5 review: both checks were tied to the requirement only, not to the code they checked. A manual fix
    # after a security failure re-ran security but not the regression; a changed workspace after both passed
    # reached review with no check at all. Each pass now records the exact content it checked.
    wid = _worktree_id(ws)
    if prog["final_done"] and prog.get("final_tree") != wid:
        prog["final_done"] = prog["security_done"] = False
    elif prog["security_done"] and prog.get("security_tree") != wid:
        prog["security_done"] = False
    save = lambda: _save(tdir / "progress.json", prog)
    _save(tdir / "requirements.json", req)
    req_view = {"goal": req["goal"], "risks": req["risks"],
                "constraints": [{"id": k, "text": v} for k, v in cons.items()],
                "acceptance_criteria": [{"id": k, "text": v} for k, v in ac.items()]}
    call_cap = Ledger(task_id).data["call_cap"]

    def agent(role, payload, post_process=None):
        ctl = control.read(task_id)               # checked before EVERY agent call
        if ctl == "pause":
            raise Paused("paused by user")
        if ctl == "stop":
            raise Stopped("stopped by user")
        prog["runs"] += 1
        save()
        lessons = memory.retrieve(role, req["goal"])          # <= 5 short hints, plain text
        if lessons:
            payload = dict(payload, lessons=lessons)
        res = runner.run_agent(role, payload, task_id=task_id, run_id=f"r{prog['runs']}", backend=backend, cwd=str(ws),
                               env=env, settings_paths=settings_paths, limits=limits, post_process=post_process)
        if not res.ok:
            raise StepFailed(f"{role}: {res.error}")
        return res.output

    def accept_plan(plan):
        if plan["status"] == "needs_input":
            _save(tdir / "plan.json", plan)
            raise NeedsInput(f"{plan['blocking_reason']} Questions: " + " | ".join(plan["questions"]))
        errs = semantics.plan_errors(plan, set(ac), call_cap)
        if errs:
            raise StepFailed("plan rejected: " + "; ".join(errs))
        if prog["plan_done"] is False:             # the window check happens once, before any build work
            ok, why = Window().can_start_large_task(estimate_task_units(len(plan["work_items"])))
            if not ok:
                _save(tdir / "plan.json", plan)
                raise BudgetExceeded(f"{why}: this plan needs about {int(estimate_task_units(len(plan['work_items'])))} units. "
                                     f"Wait for the window to reset, or ask me to start anyway")
        _save(tdir / "plan.json", plan)
        _save(tdir / f"plan-v{prog['replans'] + 1}.json", plan)

    # ---- plan
    if not prog["plan_done"]:
        tasks.set_state(task_id, "plan", title=title, current_agent="architect")
        accept_plan(agent("architect", {"mode": "initial", "requirements": req_view}))
        prog["plan_done"] = True
        save()
    plan = json.loads((tdir / "plan.json").read_text(encoding="utf-8"))

    def item_view(wi):
        return {"id": wi["id"], "title": wi["title"], "files": wi["files"], "depends_on": wi["depends_on"],
                "acceptance_criteria": [{"id": f"{wi['id']}.{i + 1}", "text": t} for i, t in enumerate(wi["acceptance_criteria"])],
                "original_criteria": [{"id": c, "text": ac[c]} for c in wi["covers"]]}

    def guarded_call(role, call, keep_qa):
        """Run ONE agent call of a role that must not change product files; check the workspace afterwards, also
        when the call failed (a rejected report, a timeout, a usage limit, a pause).

        Compared against the state from right BEFORE the call, never against HEAD alone - Phase 5 review
        (round 2): a human's uncommitted edit was taken for the agent's and deleted (even on a pause before any
        call). So the workspace must be clean when the call starts: anything uncommitted is a human's edit or
        the leftover of a failed revert, and guessing which is how both went wrong. keep_qa: the tester may
        write qa_tests/ (its earlier call's qa files may still be uncommitted); the security reviewer may
        write nothing. Returns (output, the content id the call started from)."""
        ctl = control.read(task_id)          # a pause/stop wins, and touches nothing
        if ctl == "pause":
            raise Paused("paused by user")
        if ctl == "stop":
            raise Stopped("stopped by user")
        dirty = [f for f in _changed(ws) if not (keep_qa and _is_qa(f))]
        if dirty:
            raise NeedsInput(f"uncommitted changes in the workspace before the {role} run: {dirty[:10]}. Agents never leave "
                             f"changes behind, so these are a manual edit or the leftover of a failed revert. If they are "
                             f"your fix, commit them in tasks/{task_id}/workspace; otherwise discard them. Then retry")
        head, tree = _head(ws), _worktree_id(ws)
        try:
            out, err = call(), None
        except BaseException as e:           # every exception: KeyboardInterrupt included, it is re-raised below
            out, err = None, e
        protect_config(ws, task_id, role)
        changed = _tree_diff(ws, tree, _worktree_id(ws))
        product = [f for f in changed if not (keep_qa and _is_qa(f))]
        moved = _head(ws) != head            # a commit it made itself, even one that changed nothing
        if product or moved or (err is not None and changed):    # a discarded report keeps none of its files
            _restore(ws, head)
            # Verify, never trust the restore. Uncommitted qa_tests/ files of the tester's earlier call may go
            # with it (its reports are discarded too); every other path must be exactly as before the call.
            left = [f for f in _tree_diff(ws, tree, _worktree_id(ws)) if not (keep_qa and _is_qa(f))]
            if _head(ws) != head or left:
                _save(tdir / "workspace_tainted.json", {"role": role, "head": head})
                logger.log_event(task_id, "orchestrator", "alert", f"{role} changed files and they could NOT be reverted",
                                 status="blocked", payload={"files": product or changed})
                raise StepFailed(f"{role} changed {product or changed} and they could NOT be reverted: the workspace "
                                 f"needs a human before anything else runs")
        if product or moved:
            what = product + ([f"(a commit on top of {head[:8]})"] if moved else [])
            logger.log_event(task_id, "orchestrator", "alert", f"{role} changed files it must not change: reverted, report invalid",
                             status="blocked", payload={"files": what, "call_error": str(err)[:300] if err else None})
            if err is None or isinstance(err, StepFailed):
                raise StepFailed(f"{role} modified product files {what}: report discarded" + (f" (and: {err})" if err else ""))
        if err is not None:
            raise err
        return out, tree

    def tester_call(view, expected, constraint_ids, test_strategy, files):
        payload = {"work_item": view, "constraints": req_view["constraints"], "test_strategy": test_strategy, "files_changed": files}

        def post(res):
            return semantics.process_test_report(res.output, expected, constraint_ids, res.tool_log)

        # Each call is guarded on its own. Phase 5 review (round 2): with one check around both calls, a tester
        # could change a product file in the first call and put it back in the second, unseen.
        rep, start = guarded_call("tester", lambda: agent("tester", payload, post_process=post), keep_qa=True)
        # A report that only MISSES coverage (nothing skipped, nothing failed) is a tester omission, not a question
        # for the user. Ask the tester once to complete it. One extra call is cheaper than stopping the whole task.
        if rep["verdict"] == "blocked" and rep.get("gaps") and not any(c["result"] == "skipped" for c in rep["checks"]):
            logger.log_event(task_id, "orchestrator", "decision", f"tester report incomplete: asking once to complete it ({len(rep['gaps'])} gaps)",
                             status="pending", payload={"gaps": rep["gaps"]})
            rep, _ = guarded_call("tester", lambda: agent("tester", dict(payload, rejection={"gaps": rep["gaps"], "instruction":
                                  "Your previous report was incomplete. Run the missing checks now and report them in `checks`, with the ids in "
                                  "`criterion_ids`. Mark a constraint not applicable only in `constraints_na` with a reason. "
                                  "Statements in `summary` do not count."}), post_process=post), keep_qa=True)
        commit_changes(ws, f"qa: {view['id']}", only=_is_qa)
        # The content this report is about: the product files as they were when the first call started (both calls
        # were checked not to change them) plus the qa files just committed. Verified here, not assumed - Phase 5
        # review (round 2): the id was read after the check, so a change in between was recorded as checked.
        checked = _worktree_id(ws)
        moved = [f for f in _tree_diff(ws, start, checked) if not _is_qa(f)]
        if moved:
            raise StepFailed(f"the workspace changed while the tester checked it ({moved[:10]}): report discarded")
        prog["reports"] += 1
        (tdir / "reports").mkdir(exist_ok=True)
        _save(tdir / "reports" / f"test_report-{prog['reports']:03d}-{view['id']}.json", rep)
        _save(tdir / "test_report.json", rep)
        if rep.get("verdict_reported") not in (None, rep["verdict"]):
            logger.log_event(task_id, "orchestrator", "decision",
                             f"verdict corrected by rules: reported {rep['verdict_reported']} -> {rep['verdict']}", status="ok")
        logger.log_event(task_id, "tester", "test_run", f"{view['id']}: verdict {rep['verdict']} ({len(rep['checks'])} checks)",
                         status="ok" if rep["verdict"] == "pass" else "error", payload={"verdict": rep["verdict"]})
        return rep, checked

    def security_call():
        base = _git_ok(ws, "rev-list", "--max-parents=0", "HEAD").split()[0]
        files = [p for p in _git_ok(ws, "diff", "--name-only", "-z", f"{base}..HEAD").split("\0") if p]
        payload = {"goal": req["goal"], "constraints": req_view["constraints"], "files_changed": files}

        def post(res):
            return semantics.process_security_report(res.output, ws)
        rep, checked = guarded_call("security", lambda: agent("security", payload, post_process=post), keep_qa=False)
        if _worktree_id(ws) != checked:
            raise StepFailed("the workspace changed while the security reviewer checked it: report discarded")
        _save(tdir / "security_report.json", rep)
        logger.log_event(task_id, "security", "test_run", f"verdict {rep['verdict']} ({len(rep['findings'])} finding(s))",
                         status="ok" if rep["verdict"] == "pass" else "error", payload={"verdict": rep["verdict"]})
        return rep, checked

    # ---- build / test / fix, one work item at a time
    # No long-lived Ledger here: a fresh instance is constructed right before each record_*() call below, so
    # it always loads the current on-disk budget.json. A Ledger held across agent() calls would snapshot
    # calls/tokens at creation time and overwrite the ones runner.run_agent recorded in between on .save().
    while True:
        todo = [i for i, w in enumerate(plan["work_items"]) if prog["completed"].get(w["id"]) != _item_hash(w)]
        if not todo:
            break
        prog["item"] = todo[0]
        wi = plan["work_items"][prog["item"]]
        view = item_view(wi)
        if prog["step"] == "dev":
            tasks.set_state(task_id, "fix" if prog["fix"] else "build", title=title, current_agent="developer")
            out = agent("developer", {"work_item": view, "constraints": req_view["constraints"], "plan_summary": plan["summary"],
                                      "tech_choices": plan["tech_choices"], "test_strategy": plan["test_strategy"],
                                      "previous_test_report": prog["last_report"]})
            if out["status"] == "blocked":
                reason = out["blocking_reason"] + (" Questions: " + " | ".join(out.get("questions", [])) if out.get("questions") else "")
                raise (NeedsInput if out["blocking_kind"] == "user_input" else EnvBlocked)(f"developer blocked on {wi['id']}: {reason}")
            protect_qa(ws, task_id)
            protect_config(ws, task_id, "developer")
            files = commit_changes(ws, f"{wi['id']}: {wi['title']}")
            claimed = set(out["files_changed"])
            if claimed and not claimed <= set(files) | set(_changed(ws)):
                logger.log_event(task_id, "orchestrator", "alert", "developer reported files that git does not show as changed",
                                 status="blocked", payload={"claimed": sorted(claimed), "git": files})
            with (tdir / "dev_log.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {wi['id']} {wi['title']} (fix {prog['fix']})\n{out['summary']}\n\n{out.get('notes', '')}\n")
            logger.log_event(task_id, "developer", "file_edit", f"{len(files)} file(s) changed for {wi['id']} (from git)", payload={"files": files})
            prog["step"], prog["last_files"] = "test", files
            save()
        if prog["step"] == "test":
            tasks.set_state(task_id, "test", title=title, current_agent="tester")
            # The tester sees the item, constraints and changed files. It never sees the developer's notes or reasoning.
            expected = [c["id"] for c in view["acceptance_criteria"]]
            rep, _ = tester_call(view, expected, list(cons), plan["test_strategy"], prog["last_files"])
            if rep["verdict"] == "pass":
                prog["completed"][wi["id"]] = _item_hash(wi)
                prog.update(step="dev", fix=0, last_report=None)
            elif rep["verdict"] == "blocked":
                raise NeedsInput(f"tester could not verify {wi['id']}: {rep['summary']}")
            elif rep["design_problem"]:
                Ledger(task_id).record_replan()      # raises BudgetExceeded at the cap
                prog["replans"] += 1
                tasks.set_state(task_id, "plan", title=title, current_agent="architect")
                accept_plan(agent("architect", {"mode": "replan", "requirements": req_view, "current_plan": plan,
                                                "failing_item": view, "failing_report": rep,
                                                "completed_item_ids": sorted(prog["completed"]), "attempt": prog["replans"]}))
                plan = json.loads((tdir / "plan.json").read_text(encoding="utf-8"))
                prog.update(step="dev", fix=0, last_report=None)
            else:
                Ledger(task_id).record_fix_loop()    # raises BudgetExceeded at the cap
                prog.update(fix=prog["fix"] + 1, step="dev", last_report=rep)
            save()

    # ---- final regression: every original criterion and constraint, on the finished workspace
    if not prog["final_done"]:
        # A human's manual fix must be COMMITTED by the human first: uncommitted changes stop the tester call
        # (guarded_call). Phase 5 review (round 2): auto-committing them here also committed the leftovers of a
        # failed revert, as a "manual" fix.
        tasks.set_state(task_id, "test", title=title, current_agent="tester")
        final_view = {"id": "FINAL", "title": "Full regression of the finished workspace", "files": [], "depends_on": [],
                      "acceptance_criteria": req_view["acceptance_criteria"], "original_criteria": []}
        rep, checked = tester_call(final_view, list(ac), list(cons), plan["test_strategy"], [])
        if rep["verdict"] == "blocked":
            raise NeedsInput(f"tester could not complete the final regression: {rep['summary']}")
        if rep["verdict"] == "fail":
            raise StepFailed(f"final regression failed: {rep['summary']}")
        prog["final_done"], prog["final_tree"] = True, checked
        save()

    # ---- security review: once, on the whole finished workspace (Phase 5). Never auto-blocks on its own
    # judgment calls ("blocked") or on low/medium findings - those are surfaced to the human at the merge
    # gate instead (see orchestrator._merge_request). A demonstrated high/critical finding DOES stop here,
    # the same way a failed final regression does: a human decides, rather than the task sailing on to ask
    # for merge approval with a confirmed vulnerability already in hand.
    if not prog["security_done"]:
        tasks.set_state(task_id, "security", title=title, current_agent="security")
        rep, checked = security_call()
        if rep["verdict"] == "blocked":
            raise NeedsInput(f"security reviewer needs information to finish: {rep['summary']}")
        if rep["verdict"] == "fail":
            raise StepFailed(f"security review found {sum(1 for f in rep['findings'] if f['severity'] in ('high', 'critical'))} "
                             f"high/critical finding(s): {rep['summary']}")
        prog["security_done"], prog["security_tree"] = True, checked
        save()
    tasks.set_state(task_id, "review", title=title, current_agent=None, waiting_approval=True)
    (tdir / "summary.md").write_text(
        f"# {title}\n\n{plan['summary']}\n\nWork items: {len(plan['work_items'])}, all passed; final regression passed; "
        f"security review: no high/critical findings.\n"
        f"Workspace: `tasks/{task_id}/workspace` (git history = one commit per work item).\n", encoding="utf-8")
