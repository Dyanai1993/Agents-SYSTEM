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
    r = subprocess.run([sys.executable, str(task / "check.py"), str(ws)], capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


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


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "selfcheck"
    if mode != "selfcheck":
        sys.exit("real mode arrives in Phase 2 (it costs usage). Use: selfcheck")
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
