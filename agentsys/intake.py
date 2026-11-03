"""Intake: plain-language request -> requirements.json (Haiku, 1 call)."""
import json

from . import config, runner


def run_intake(task_id, request_text, *, backend, env=None, settings_paths=None):
    """Return (requirements, error). error is None on success."""
    res = runner.run_agent("orchestrator", {"request": request_text}, task_id=task_id, run_id="intake", backend=backend,
                           env=env, settings_paths=settings_paths)
    if not res.ok:
        return None, res.error
    tdir = config.home() / "tasks" / task_id
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "requirements.json").write_text(json.dumps(res.output, indent=2, ensure_ascii=False), encoding="utf-8")
    return res.output, None
