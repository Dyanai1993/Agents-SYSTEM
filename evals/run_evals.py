"""Eval harness.

  python evals/run_evals.py selfcheck   Zero quota. Proves every check passes on the reference
                                        solution and fails on an empty workspace.
  python evals/run_evals.py real        (Phase 2) runs the agents on every task. Uses quota.
"""
import json, shutil, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASKS = sorted(p for p in (HERE / "tasks").iterdir() if (p / "task.json").exists())


def check(task: Path, ws: Path):
    try:
        r = subprocess.run([sys.executable, str(task / "check.py"), str(ws)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:           # a hanging solution fails ITS task; it must not abort the whole suite
        return False, "check timed out after 60s"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def _git_state():
    def g(*a):
        r = subprocess.run(["git", *a], cwd=HERE.parent, capture_output=True, text=True)
        return r.stdout.strip() if r.returncode == 0 else ""
    return g("rev-parse", "HEAD"), g("rev-parse", "--abbrev-ref", "HEAD"), bool(g("status", "--porcelain"))


def workspace(task: Path, with_reference: bool, tmp: Path) -> Path:
    ws = tmp / task.name
    ws.mkdir(parents=True)
    for sub in ("fixtures",) + (("reference",) if with_reference else ()):
        src = task / sub
        if src.is_dir():
            shutil.copytree(src, ws, dirs_exist_ok=True)
    return ws


def selfcheck():
    rows = []
    with tempfile.TemporaryDirectory() as t:
        for task in TASKS:
            ok_ref, out_ref = check(task, workspace(task, True, Path(t) / "ref"))
            bad_ok, out_bad = check(task, workspace(task, False, Path(t) / "empty"))
            rows.append({"id": task.name, "reference_passes": ok_ref, "empty_fails": not bad_ok,
                         "ok": ok_ref and not bad_ok, "detail": "" if ok_ref and not bad_ok else (out_ref if not ok_ref else "empty workspace passed")})
    return rows


def run_real(backend, label, only=None, home=None, token_budget=None, resume_dir=None):
    """Run the agent loop on every eval task (REAL quota with a real backend) and apply each task's check.

    Metrics per task: passed, status, fix loops, re-plans, agent calls, tokens (estimate), seconds.
    """
    import os
    sys.path.insert(0, str(HERE.parent))
    from agentsys import loop, config
    from agentsys.ledger import Ledger
    if token_budget is not None and token_budget <= 0:
        raise ValueError("token_budget must be positive")
    run_id = f"{label}-{int(time.time())}"
    sha, branch, dirty = _git_state()
    if resume_dir:                              # resuming is explicit; a repeated label never silently reuses an old run
        home = Path(resume_dir)
        manifest_path = home / "run.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        run_id = manifest.get("run_id", run_id)
        # The manifest is the ONLY source of truth for what commit this run started on. A run resumed on a
        # different commit (e.g. after checking out a lesson-proposal branch) would silently reuse old,
        # already-measured checkpoints and tag them with the new commit's sha - the "after" run would then
        # look genuinely measured on the branch to the eval gate, when it was not re-run at all. A manifest
        # with no git_sha at all (e.g. written before this check existed) must not silently bypass it either.
        if not manifest.get("git_sha"):
            raise ValueError(f"--resume {resume_dir}'s manifest has no recorded git_sha: cannot verify what "
                              f"commit it started on, so it cannot be safely resumed. Start a new run instead.")
        if manifest["git_sha"] != sha:
            raise ValueError(f"--resume {resume_dir} was started on {manifest['git_sha'][:8]}, but the current "
                              f"checkout is on {sha[:8]}: check out the original commit before resuming, or start a new run")
    else:
        home = Path(home or HERE / "runs" / run_id)
    home.mkdir(parents=True, exist_ok=True)
    manifest_path = home / "run.json"
    if not manifest_path.exists():               # immutable once written: a resume never overwrites the original commit
        manifest_path.write_text(json.dumps({"run_id": run_id, "label": label, "git_sha": sha, "git_branch": branch}))
    (HERE / "results").mkdir(exist_ok=True)
    path = HERE / "results" / f"real-{run_id}.json"
    old_home = os.environ.get("AGENTSYS_HOME")
    os.environ["AGENTSYS_HOME"] = str(home)
    rows, spent = [], 0
    try:
        for task in TASKS:
            if only and task.name not in only:
                continue
            # `sha` was captured once, at the top of this function. TASKS, each task's task.json/check.py,
            # and the real repo files underneath this run's own code are all read fresh from disk for EVERY
            # task as the (possibly hours-long, sequential) run proceeds - a commit landing mid-run would
            # otherwise be silently measured by later tasks while the whole run still reports the FIRST sha,
            # as if every task had been measured under one single, consistent version.
            # Review (fourth round): comparing only the sha missed an UNCOMMITTED change appearing mid-run -
            # the run's reported git_dirty is whatever was true at the very START, never re-checked, so a
            # tree that became dirty partway through was still reported clean. Comparing (sha, dirty)
            # catches both directions of drift.
            now_sha, _, now_dirty = _git_state()
            if (now_sha, now_dirty) != (sha, dirty):
                raise RuntimeError(f"the repository changed mid-run: started on {sha[:8]} (dirty={dirty}), now on "
                                    f"{now_sha[:8]} (dirty={now_dirty}) (at task {task.name}). Results so far are "
                                    f"saved; nothing after this is measured under one consistent commit. Start a "
                                    f"new run on a frozen checkout instead.")
            if token_budget and token_budget - spent <= 0:
                rows.append({"id": task.name, "passed": False, "status": "skipped_budget", "fix_loops": 0, "replans": 0, "calls": 0,
                             "tokens": 0, "seconds": 0, "detail": "token budget for the eval run was used up"})
                continue
            spec = json.loads((task / "task.json").read_text(encoding="utf-8"))
            req = {"goal": spec["prompt"], "constraints": [], "acceptance_criteria": [spec["expected"]], "risks": [], "unknowns": []}
            tid = f"eval-{task.name}"
            if token_budget:                    # the budget is a hard cap INSIDE the task, not only between tasks
                Ledger(tid, caps={"token_cap": min(config.TASK_CAPS["token_cap"], token_budget - spent)})
            t0 = time.time()
            res = loop.run_task(tid, req, backend=backend, title=spec["title"], seed=(task / "fixtures") if (task / "fixtures").is_dir() else None)
            ws = config.home() / "tasks" / tid / "workspace"
            if res["status"] == "needs_input":      # an agent that asks instead of guessing: record the questions as the result file
                plan = config.home() / "tasks" / tid / "plan.json"
                qs = json.loads(plan.read_text())["questions"] if plan.exists() and "questions" in json.loads(plan.read_text()) else [res["message"]]
                (ws / "needs_input.json").write_text(json.dumps({"questions": [q if q.strip().endswith("?") else q.strip() + "?" for q in qs]}))
            ok, detail = check(task, ws)
            bud = json.loads((config.home() / "tasks" / tid / "budget.json").read_text()) if (config.home() / "tasks" / tid / "budget.json").exists() else {}
            spent += bud.get("tokens_est", 0)
            rows.append({"id": task.name, "passed": ok, "status": res["status"], "fix_loops": bud.get("fix_loops", 0), "replans": bud.get("replans", 0),
                         "calls": bud.get("calls", 0), "tokens": bud.get("tokens_est", 0), "seconds": round(time.time() - t0, 1), "detail": "" if ok else detail[:200]})
            meta = {"mode": "real", "run_id": run_id, "label": label, "git_sha": sha, "git_branch": branch, "git_dirty": dirty, "complete": False,
                    "ts": datetime.now(timezone.utc).isoformat(), "backend": getattr(backend, "name", "?"), "results": rows}
            path.write_text(json.dumps(meta, indent=2))      # saved after EVERY task: a crash keeps what finished
    finally:
        if old_home is None:
            os.environ.pop("AGENTSYS_HOME", None)
        else:
            os.environ["AGENTSYS_HOME"] = old_home
    # Review (fourth round): the per-task drift check runs at the START of each iteration, so a change
    # landing DURING the LAST task was never caught - the loop simply ends, and the run was still reported
    # complete=True under the original, now-stale sha. Check once more right before finalizing.
    final_sha, _, final_dirty = _git_state()
    if (final_sha, final_dirty) != (sha, dirty):
        meta = {"mode": "real", "run_id": run_id, "label": label, "git_sha": sha, "git_branch": branch, "git_dirty": dirty, "complete": False,
                "ts": datetime.now(timezone.utc).isoformat(), "backend": getattr(backend, "name", "?"), "results": rows}
        path.write_text(json.dumps(meta, indent=2))
        raise RuntimeError(f"the repository changed during the run: started on {sha[:8]} (dirty={dirty}), now on "
                            f"{final_sha[:8]} (dirty={final_dirty}). Results are saved but NOT marked complete - "
                            f"the last task(s) may not be measured under one consistent commit.")
    out = {"mode": "real", "run_id": run_id, "label": label, "git_sha": sha, "git_branch": branch, "git_dirty": dirty, "complete": not only,
           "ts": datetime.now(timezone.utc).isoformat(), "backend": getattr(backend, "name", "?"), "results": rows}
    path.write_text(json.dumps(out, indent=2))
    return out, path


def print_table(rows):
    print(f"{'task':<24}{'passed':<8}{'status':<17}{'fix':<5}{'calls':<7}{'tokens':<9}{'sec':<6}")
    for r in rows:
        print(f"{r['id']:<24}{'yes' if r['passed'] else 'NO':<8}{r['status']:<17}{r['fix_loops']:<5}{r['calls']:<7}{r['tokens']:<9}{r['seconds']:<6}{r['detail'][:60]}")
    print(f"\n{sum(r['passed'] for r in rows)}/{len(rows)} passed, {sum(r['tokens'] for r in rows)} tokens (estimate)")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"
    if mode == "real":
        import argparse
        ap = argparse.ArgumentParser()
        ap.add_argument("mode"); ap.add_argument("--label", required=True)
        ap.add_argument("--backend", default="cli", choices=["cli", "sdk"]); ap.add_argument("--only", action="append")
        ap.add_argument("--token-budget", type=int, default=900_000)
        ap.add_argument("--resume", help="continue THIS run folder (evals/runs/<run>); without it every run is new")
        ap.add_argument("--yes", action="store_true", help="confirm that this spends subscription usage")
        a = ap.parse_args()
        if a.token_budget <= 0:
            sys.exit("--token-budget must be positive")
        n = len(a.only or TASKS)
        print(f"REAL eval: {n} task(s), about {n * 90_000} tokens (estimate) on your subscription. Sequential.")
        if not a.yes:
            sys.exit("add --yes to start")
        sys.path.insert(0, str(HERE.parent))
        from agentsys import billing_guard, runner
        try:
            billing_guard.run_guard()
        except billing_guard.BillingGuardError as e:
            sys.exit(f"{e}")
        out, path = run_real(runner.get_backend(a.backend), a.label, a.only, token_budget=a.token_budget, resume_dir=a.resume)
        print_table(out["results"]); print("saved:", path)
        sys.exit(0)
    if mode == "compare":
        sys.path.insert(0, str(HERE.parent))
        from agentsys import learning
        before, after = (json.loads(Path(p).read_text()) for p in sys.argv[2:4])
        c = learning.compare_results(before, after)
        print(f"{'task':<24}{'before':<8}{'after':<8}{'fix b/a'}")
        for r in c["rows"]:
            print(f"{r['id']:<24}{str(r['before']):<8}{str(r['after']):<8}{r['fix_before']}/{r['fix_after']}")
        print(f"\npassed {c['pass_before']} -> {c['pass_after']}, tokens {c['tokens_before']} -> {c['tokens_after']} ({c['token_change_pct']:+}%)")
        print("REGRESSIONS:", c["regressions"] or "none")
        sys.exit(1 if c["regressions"] else 0)
    if mode != "selfcheck":
        sys.exit("modes: selfcheck | real --label L --yes | compare BEFORE.json AFTER.json")
    rows = selfcheck()
    out = {"mode": "selfcheck", "ts": datetime.now(timezone.utc).isoformat(), "results": rows}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"selfcheck-{int(time.time())}.json").write_text(json.dumps(out, indent=2))
    print(f"{'task':<24}{'reference':<11}{'empty':<8}")
    for r in rows:
        print(f"{r['id']:<24}{'pass' if r['reference_passes'] else 'FAIL':<11}{'fails' if r['empty_fails'] else 'PASSES!':<8}{r['detail'][:80]}")
    bad = [r for r in rows if not r["ok"]]
    print(f"\n{len(rows) - len(bad)}/{len(rows)} checks are sound")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
