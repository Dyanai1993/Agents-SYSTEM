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


def _changed(ws):
    out = _git(ws, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def _is_qa(path):
    return path.replace("\\", "/").startswith(QA_DIR + "/")


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
    files = [f for f in _changed(ws) if only is None or only(f)]
    if files:
        _git(ws, "add", "--", *files)
        _git(ws, "commit", "-q", "-m", message[:100])
    return files


def _load_progress(tdir):
    # Merged over the defaults (not returned as-is) so a progress.json saved by an older version of this
    # code - missing a key added since, like "security_done" in Phase 5 - resumes instead of a KeyError
    # the first time that key is read.
    defaults = {"plan_done": False, "item": 0, "step": "dev", "fix": 0, "last_report": None, "runs": 0, "replans": 0,
                "reports": 0, "completed": {}, "final_done": False, "security_done": False, "last_files": [], "req_hash": None}
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


def run_task(task_id, requirements, *, backend, title=None, env=None, settings_paths=None, limits=None, seed=None):
    """Run or resume one task. Returns {"status", "state", "message", "report"}."""
    logger.safe_task_id(task_id)
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
    dirty = _changed(ws) if (ws / ".git").exists() else []
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

    def tester_call(view, expected, constraint_ids, test_strategy, files):
        payload = {"work_item": view, "constraints": req_view["constraints"], "test_strategy": test_strategy, "files_changed": files}

        def post(res):
            return semantics.process_test_report(res.output, expected, constraint_ids, res.tool_log)
        rep = agent("tester", payload, post_process=post)
        # A report that only MISSES coverage (nothing skipped, nothing failed) is a tester omission, not a question for
        # the user. Ask the tester once to complete it. One extra call is cheaper than stopping the whole task.
        if rep["verdict"] == "blocked" and rep.get("gaps") and not any(c["result"] == "skipped" for c in rep["checks"]):
            logger.log_event(task_id, "orchestrator", "decision", f"tester report incomplete: asking once to complete it ({len(rep['gaps'])} gaps)",
                             status="pending", payload={"gaps": rep["gaps"]})
            rep = agent("tester", dict(payload, rejection={"gaps": rep["gaps"], "instruction":
                        "Your previous report was incomplete. Run the missing checks now and report them in `checks`, with the ids in "
                        "`criterion_ids`. Mark a constraint not applicable only in `constraints_na` with a reason. "
                        "Statements in `summary` do not count."}), post_process=post)
        protect_config(ws, task_id, "tester")
        mutated = [f for f in _changed(ws) if not _is_qa(f)]
        if mutated:                                  # tester must not edit product code
            _git(ws, "checkout", "--", ".", f":(exclude){QA_DIR}")
            _git(ws, "clean", "-fdq", "--", ".", f":(exclude){QA_DIR}")
            logger.log_event(task_id, "orchestrator", "alert", "tester changed product files: reverted, report invalid",
                             status="blocked", payload={"files": mutated})
            raise StepFailed(f"tester modified product files {mutated}: report discarded")
        commit_changes(ws, f"qa: {view['id']}", only=_is_qa)
        prog["reports"] += 1
        (tdir / "reports").mkdir(exist_ok=True)
        _save(tdir / "reports" / f"test_report-{prog['reports']:03d}-{view['id']}.json", rep)
        _save(tdir / "test_report.json", rep)
        if rep.get("verdict_reported") not in (None, rep["verdict"]):
            logger.log_event(task_id, "orchestrator", "decision",
                             f"verdict corrected by rules: reported {rep['verdict_reported']} -> {rep['verdict']}", status="ok")
        logger.log_event(task_id, "tester", "test_run", f"{view['id']}: verdict {rep['verdict']} ({len(rep['checks'])} checks)",
                         status="ok" if rep["verdict"] == "pass" else "error", payload={"verdict": rep["verdict"]})
        return rep

    def security_call():
        base = _git(ws, "rev-list", "--max-parents=0", "HEAD").stdout.split()[0]
        files = _git(ws, "diff", "--name-only", f"{base}..HEAD").stdout.split()
        payload = {"goal": req["goal"], "constraints": req_view["constraints"], "files_changed": files}

        def post(res):
            return semantics.process_security_report(res.output, ws)
        rep = agent("security", payload, post_process=post)
        protect_config(ws, task_id, "security")
        mutated = [f for f in _changed(ws) if not _is_qa(f)]
        if mutated:                                  # the security reviewer must not edit anything either
            _git(ws, "checkout", "--", ".", f":(exclude){QA_DIR}")
            _git(ws, "clean", "-fdq", "--", ".", f":(exclude){QA_DIR}")
            logger.log_event(task_id, "orchestrator", "alert", "security reviewer changed files: reverted, report invalid",
                             status="blocked", payload={"files": mutated})
            raise StepFailed(f"security reviewer modified files {mutated}: report discarded")
        _save(tdir / "security_report.json", rep)
        logger.log_event(task_id, "security", "test_run", f"verdict {rep['verdict']} ({len(rep['findings'])} finding(s))",
                         status="ok" if rep["verdict"] == "pass" else "error", payload={"verdict": rep["verdict"]})
        return rep

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
            rep = tester_call(view, expected, list(cons), plan["test_strategy"], prog["last_files"])
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
        tasks.set_state(task_id, "test", title=title, current_agent="tester")
        final_view = {"id": "FINAL", "title": "Full regression of the finished workspace", "files": [], "depends_on": [],
                      "acceptance_criteria": req_view["acceptance_criteria"], "original_criteria": []}
        rep = tester_call(final_view, list(ac), list(cons), plan["test_strategy"], [])
        if rep["verdict"] == "blocked":
            raise NeedsInput(f"tester could not complete the final regression: {rep['summary']}")
        if rep["verdict"] == "fail":
            raise StepFailed(f"final regression failed: {rep['summary']}")
        prog["final_done"] = True
        save()

    # ---- security review: once, on the whole finished workspace (Phase 5). Never auto-blocks on its own
    # judgment calls ("blocked") or on low/medium findings - those are surfaced to the human at the merge
    # gate instead (see orchestrator._merge_context). A demonstrated high/critical finding DOES stop here,
    # the same way a failed final regression does: a human decides, rather than the task sailing on to ask
    # for merge approval with a confirmed vulnerability already in hand.
    if not prog["security_done"]:
        tasks.set_state(task_id, "security", title=title, current_agent="security")
        rep = security_call()
        if rep["verdict"] == "blocked":
            raise NeedsInput(f"security reviewer needs information to finish: {rep['summary']}")
        if rep["verdict"] == "fail":
            raise StepFailed(f"security review found {sum(1 for f in rep['findings'] if f['severity'] in ('high', 'critical'))} "
                             f"high/critical finding(s): {rep['summary']}")
        prog["security_done"] = True
        save()
    tasks.set_state(task_id, "review", title=title, current_agent=None, waiting_approval=True)
    (tdir / "summary.md").write_text(
        f"# {title}\n\n{plan['summary']}\n\nWork items: {len(plan['work_items'])}, all passed; final regression passed; "
        f"security review: no high/critical findings.\n"
        f"Workspace: `tasks/{task_id}/workspace` (git history = one commit per work item).\n", encoding="utf-8")
