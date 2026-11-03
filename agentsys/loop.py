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
import subprocess

from . import billing_guard, config, logger, runner, schema, semantics, tasks
from .ledger import BudgetExceeded, Ledger

QA_DIR = "qa_tests"


class StepFailed(RuntimeError):
    pass


class NeedsInput(RuntimeError):
    """A human must answer. Used only for missing information, never for budget or environment problems."""


class EnvBlocked(RuntimeError):
    """The environment cannot do the work (missing tool, no network...). A human must fix the setup."""


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


def _changed(ws):
    out = _git(ws, "status", "--porcelain", "--untracked-files=all").stdout
    return [line[3:].strip().strip('"') for line in out.splitlines() if line.strip()]


def _is_qa(path):
    return path.replace("\\", "/").startswith(QA_DIR + "/")


def protect_qa(ws, task_id):
    """Developer must not touch qa_tests/. Revert any change and raise an alert."""
    bad = [f for f in _changed(ws) if _is_qa(f)]
    if bad:
        _git(ws, "checkout", "--", QA_DIR)
        _git(ws, "clean", "-fdq", "--", QA_DIR)
        logger.log_event(task_id, "orchestrator", "alert", f"developer changed protected {QA_DIR}/: reverted", status="blocked",
                         payload={"files": bad})
    return bad


def commit_changes(ws, message, only=None):
    files = [f for f in _changed(ws) if only is None or only(f)]
    if files:
        _git(ws, "add", "--", *files)
        _git(ws, "commit", "-q", "-m", message[:100])
    return files


def _load_progress(tdir):
    p = tdir / "progress.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"plan_done": False, "item": 0, "step": "dev", "fix": 0, "last_report": None, "runs": 0, "replans": 0,
            "reports": 0, "completed": {}, "final_done": False, "last_files": []}


def _ids(req):
    ac = {f"AC{i + 1}": t for i, t in enumerate(req["acceptance_criteria"])}
    cons = {f"C{i + 1}": t for i, t in enumerate(req["constraints"])}
    return ac, cons


def _item_hash(wi):
    return hashlib.sha256(json.dumps(wi, sort_keys=True).encode()).hexdigest()[:12]


def run_task(task_id, requirements, *, backend, title=None, env=None, settings_paths=None, limits=None):
    """Run or resume one task. Returns {"status", "state", "message", "report"}."""
    logger.safe_task_id(task_id)
    tdir = config.home() / "tasks" / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    schema.validate("requirements", requirements)   # bad input is the caller's bug: raise, do not log a task failure
    title = title or requirements["goal"][:60]
    try:
        _run(task_id, tdir, requirements, backend, title, env, settings_paths, limits)
        status, state, msg = "ready_for_review", "review", "All work items and the final regression passed. Waiting for your review."
    except runner.UsageLimitReached as e:
        status, state, msg = "paused", "paused", f"Usage limit reached. State saved. Run the same command again later. ({e})"
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


def _run(task_id, tdir, req, backend, title, env, settings_paths, limits):
    ac, cons = _ids(req)
    blocking = [u["question"] for u in req["unknowns"] if u["blocking"]]
    if blocking:                                    # no agent call needed to know a human must answer
        raise NeedsInput("Blocking unknowns in the requirement: " + " | ".join(blocking))
    ws = ensure_workspace(tdir)
    prog = _load_progress(tdir)
    save = lambda: _save(tdir / "progress.json", prog)
    _save(tdir / "requirements.json", req)
    req_view = {"goal": req["goal"], "risks": req["risks"],
                "constraints": [{"id": k, "text": v} for k, v in cons.items()],
                "acceptance_criteria": [{"id": k, "text": v} for k, v in ac.items()]}
    call_cap = Ledger(task_id).data["call_cap"]

    def agent(role, payload, post_process=None):
        prog["runs"] += 1
        save()
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

    # ---- build / test / fix, one work item at a time
    ledger = Ledger(task_id)
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
                ledger.record_replan()      # raises BudgetExceeded at the cap
                prog["replans"] += 1
                tasks.set_state(task_id, "plan", title=title, current_agent="architect")
                accept_plan(agent("architect", {"mode": "replan", "requirements": req_view, "current_plan": plan,
                                                "failing_item": view, "failing_report": rep,
                                                "completed_item_ids": sorted(prog["completed"]), "attempt": prog["replans"]}))
                plan = json.loads((tdir / "plan.json").read_text(encoding="utf-8"))
                prog.update(step="dev", fix=0, last_report=None)
            else:
                ledger.record_fix_loop()    # raises BudgetExceeded at the cap
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
    tasks.set_state(task_id, "review", title=title, current_agent=None, waiting_approval=True)
    (tdir / "summary.md").write_text(
        f"# {title}\n\n{plan['summary']}\n\nWork items: {len(plan['work_items'])}, all passed; final regression passed.\n"
        f"Workspace: `tasks/{task_id}/workspace` (git history = one commit per work item).\n", encoding="utf-8")
