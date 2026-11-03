"""Core loop (Phase 2): requirement -> architect -> (developer -> tester -> fix loop) per work item.

Deterministic code decides every transition. Agents only fill documents.
Checkpoint: tasks/<id>/progress.json is written after every completed step. Re-running
run_task() with the same task id resumes at the step that did not finish.
Ends at state 'review' (waiting for the human). Merge/deploy gates arrive in Phase 3.
"""
import json
import subprocess

from . import billing_guard, config, logger, runner, schema, tasks
from .ledger import BudgetExceeded, Ledger


class StepFailed(RuntimeError):
    pass


class NeedsInput(RuntimeError):
    pass


def _save(path, doc):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _git(ws, *args, check=False):
    cmd = ["git", "-c", "user.name=agentsys", "-c", "user.email=agentsys@localhost", *args]
    return subprocess.run(cmd, cwd=ws, capture_output=True, text=True, check=check)


def ensure_workspace(tdir):
    ws = tdir / "workspace"
    if not (ws / ".git").exists():
        ws.mkdir(parents=True, exist_ok=True)
        _git(ws, "init", "-q", check=True)
        _git(ws, "commit", "-q", "--allow-empty", "-m", "workspace start", check=True)
    return ws


def commit_changes(ws, message):
    """Commit whatever the developer changed. Returns the changed file list."""
    out = _git(ws, "status", "--porcelain").stdout
    files = [line[3:] for line in out.splitlines() if line.strip()]
    if files:
        _git(ws, "add", "-A")
        _git(ws, "commit", "-q", "-m", message[:100])
    return files


def _load_progress(tdir):
    p = tdir / "progress.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"plan_done": False, "item": 0, "step": "dev", "fix": 0, "last_report": None, "runs": 0, "replans": 0, "reports": 0}


def run_task(task_id, requirements, *, backend, title=None, env=None, settings_paths=None, limits=None):
    """Run or resume one task. Returns {"status", "state", "message", "report"}."""
    logger.safe_task_id(task_id)
    tdir = config.home() / "tasks" / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    schema.validate("requirements", requirements)   # bad input is the caller's bug: raise, do not log a task failure
    try:
        _run(task_id, tdir, requirements, backend, title or requirements["goal"][:60], env, settings_paths, limits)
        status, state, msg = "ready_for_review", "review", "All work items passed. Waiting for your review."
    except runner.UsageLimitReached as e:
        status, state, msg = "paused", "paused", f"Usage limit reached. State saved. Run the same command again later. ({e})"
    except NeedsInput as e:
        status, state, msg = "needs_input", "blocked", str(e)
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
    logger.log_event(task_id, "orchestrator", "decision", f"task ends: {status}", status="ok" if status in ("ready_for_review", "paused") else "blocked",
                     payload={"status": status, "message": msg})
    return {"status": status, "state": state, "message": msg, "report": str(report) if report else None}


def _status_report(tdir, task_id, status, msg):
    led = Ledger(task_id).data
    prog = _load_progress(tdir)
    p = tdir / "status_report.md"
    p.write_text(
        f"# Status report: {task_id}\n\nStatus: **{status}**\n\n{msg}\n\n"
        f"- agent calls: {led['calls']}/{led['call_cap']}\n- fix loops: {led['fix_loops']}/{led['fix_cap']}\n"
        f"- re-plans: {led['replans']}/{led['replan_cap']}\n- work item index: {prog['item']} (step {prog['step']})\n"
        f"- limit hits: {len(led['limit_hits'])}\n\nSee `logs/{task_id}.jsonl` and `scripts/timeline.py`.\n"
        f"Checkpoint: `tasks/{task_id}/progress.json`.\n", encoding="utf-8")
    return p


def _run(task_id, tdir, req, backend, title, env, settings_paths, limits):
    ws = ensure_workspace(tdir)
    prog = _load_progress(tdir)
    save = lambda: _save(tdir / "progress.json", prog)
    _save(tdir / "requirements.json", req)

    def agent(role, payload):
        prog["runs"] += 1
        save()
        res = runner.run_agent(role, payload, task_id=task_id, run_id=f"r{prog['runs']}", backend=backend, cwd=str(ws),
                               env=env, settings_paths=settings_paths, limits=limits)
        if not res.ok:
            raise StepFailed(f"{role}: {res.error}")
        return res.output

    def set_plan(plan, mode):
        for r in plan.get("risks", []):
            if str(r).startswith("QUESTION:"):
                _save(tdir / "plan.json", plan)
                raise NeedsInput(str(r)[len("QUESTION:"):].strip())
        _save(tdir / "plan.json", plan)
        _save(tdir / f"plan-v{prog['replans'] + 1}.json", plan)

    # ---- plan
    if not prog["plan_done"]:
        tasks.set_state(task_id, "plan", title=title, current_agent="architect")
        plan = agent("architect", {"mode": "initial", "requirements": req})
        set_plan(plan, "initial")
        prog["plan_done"] = True
        save()
    plan = json.loads((tdir / "plan.json").read_text(encoding="utf-8"))

    # ---- build / test / fix, one work item at a time
    ledger = Ledger(task_id)
    while prog["item"] < len(plan["work_items"]):
        wi = plan["work_items"][prog["item"]]
        if prog["step"] == "dev":
            tasks.set_state(task_id, "fix" if prog["fix"] else "build", title=title, current_agent="developer")
            out = agent("developer", {"work_item": wi, "plan_summary": plan["summary"], "tech_choices": plan["tech_choices"],
                                      "test_strategy": plan["test_strategy"], "previous_test_report": prog["last_report"]})
            if str(out.get("summary", "")).startswith("BLOCKED:"):
                raise NeedsInput(f"developer blocked on {wi['id']}: {out.get('notes') or out['summary']}")
            files = commit_changes(ws, f"{wi['id']}: {wi['title']}")
            with (tdir / "dev_log.md").open("a", encoding="utf-8") as f:
                f.write(f"\n## {wi['id']} {wi['title']} (fix {prog['fix']})\n{out['summary']}\n\n{out.get('notes', '')}\n")
            logger.log_event(task_id, "developer", "file_edit", f"{len(files)} file(s) changed for {wi['id']}", payload={"files": files})
            prog["step"], prog["last_files"] = "test", files
            save()
        if prog["step"] == "test":
            tasks.set_state(task_id, "test", title=title, current_agent="tester")
            # The tester sees the plan item and the code. It never sees the developer's notes or reasoning.
            rep = agent("tester", {"work_item": {k: wi[k] for k in ("id", "title", "acceptance_criteria")},
                                   "test_strategy": plan["test_strategy"], "files_changed": prog.get("last_files", [])})
            prog["reports"] += 1
            (tdir / "reports").mkdir(exist_ok=True)
            _save(tdir / "reports" / f"test_report-{prog['reports']:03d}-{wi['id']}.json", rep)
            _save(tdir / "test_report.json", rep)
            logger.log_event(task_id, "tester", "test_run", f"{wi['id']}: verdict {rep['verdict']} ({len(rep['checks'])} checks)",
                             status="ok" if rep["verdict"] == "pass" else "error", payload={"verdict": rep["verdict"]})
            if rep["verdict"] == "pass":
                prog.update(item=prog["item"] + 1, step="dev", fix=0, last_report=None)
            elif rep["verdict"] == "blocked":
                raise NeedsInput(f"tester blocked on {wi['id']}: {rep['summary']}")
            elif rep.get("design_problem"):
                ledger.record_replan()      # raises BudgetExceeded at the cap
                prog["replans"] += 1
                tasks.set_state(task_id, "plan", title=title, current_agent="architect")
                plan = agent("architect", {"mode": "replan", "requirements": req, "current_plan": plan,
                                           "failing_report": rep, "attempt": prog["replans"]})
                set_plan(plan, "replan")
                prog.update(item=0, step="dev", fix=0, last_report=None)
            else:
                ledger.record_fix_loop()    # raises BudgetExceeded at the cap
                prog.update(fix=prog["fix"] + 1, step="dev", last_report=rep)
            save()
    tasks.set_state(task_id, "review", title=title, current_agent=None, waiting_approval=True)
    (tdir / "summary.md").write_text(
        f"# {title}\n\n{plan['summary']}\n\nWork items: {len(plan['work_items'])}, all passed.\n"
        f"Workspace: `tasks/{task_id}/workspace` (git history = one commit per work item).\n", encoding="utf-8")
