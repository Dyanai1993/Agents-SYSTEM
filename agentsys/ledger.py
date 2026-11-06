"""Budget ledger. Per task: tasks/<id>/budget.json. Global rolling window: state/window.json."""
import json
import time
from datetime import datetime, timezone

from . import config, logger


class BudgetExceeded(RuntimeError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(f"budget exceeded: {reason}")


def _iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, task_id, caps=None):
        self.task_id = logger.safe_task_id(task_id)
        self.path = config.home() / "tasks" / task_id / "budget.json"
        self.caps = dict(config.TASK_CAPS, **(caps or {}))
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {
                "task_id": task_id, "started_at": _iso(), "started_epoch": time.time(),
                "calls": 0, "call_cap": self.caps["call_cap"],
                "fix_loops": 0, "fix_cap": self.caps["fix_cap"],
                "replans": 0, "replan_cap": self.caps["replan_cap"],
                "timeout_s": self.caps["timeout_s"], "token_cap": self.caps.get("token_cap"),
                "tokens_est": 0, "units": 0.0, "active_s": 0.0, "run_started_epoch": time.time(),
                "by_model": {}, "by_agent": {}, "limit_hits": [],
            }
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic: a killed run never leaves half a file

    def begin_run(self):
        """A new run (run_task call) starts its own clock. Time spent waiting for a human, or paused, never counts."""
        self.data["run_started_epoch"] = time.time()
        self.save()

    def check_can_call(self):
        d = self.data
        if d["calls"] >= d["call_cap"]:
            raise BudgetExceeded(f"call cap {d['call_cap']} reached")
        elapsed = time.time() - d.get("run_started_epoch", d["started_epoch"])      # old budget files fall back to the start time
        if elapsed > d["timeout_s"]:
            raise BudgetExceeded(f"run timeout {d['timeout_s']}s reached in this run (time spent waiting for you does not count)")
        cap = d.get("token_cap")
        if cap is not None and d["tokens_est"] >= cap:
            raise BudgetExceeded(f"token cap {cap} reached ({d['tokens_est']} used)")

    def record_call(self, agent, model, tokens, seconds=0.0):
        d = self.data
        d["calls"] += 1
        d["active_s"] = round(d.get("active_s", 0.0) + seconds, 1)
        d["tokens_est"] += int(tokens)
        d["units"] += tokens * config.MODEL_WEIGHT.get(model, 1.0)
        d["by_model"][model] = d["by_model"].get(model, 0) + 1
        d["by_agent"][agent] = d["by_agent"].get(agent, 0) + 1
        self.save()
        Window().add(self.task_id, agent, model, tokens)

    def record_fix_loop(self):
        if self.data["fix_loops"] >= self.data["fix_cap"]:
            raise BudgetExceeded(f"fix-loop cap {self.data['fix_cap']} reached")
        self.data["fix_loops"] += 1
        self.save()

    def record_replan(self):
        if self.data["replans"] >= self.data["replan_cap"]:
            raise BudgetExceeded(f"re-plan cap {self.data['replan_cap']} reached")
        self.data["replans"] += 1
        self.save()

    def record_limit_hit(self, info):
        self.data["limit_hits"].append({"ts": _iso(), "info": str(info)[:300]})
        self.save()


def estimate_task_units(n_items: int) -> float:
    """Pre-task estimate in weighted tokens: architect + (developer + tester) per item + final regression + security."""
    calls = 2 * n_items + 3
    return calls * config.TOKENS_PER_CALL * config.FIX_MARGIN * config.MODEL_WEIGHT["sonnet"]


class Window:
    """Rolling usage across all tasks. An estimate: no official remaining-window API exists."""

    def __init__(self):
        self.path = config.home() / "state" / "window.json"

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                return []
        return []

    def add(self, task_id, agent, model, tokens):
        rows = [r for r in self._load() if r["t"] > time.time() - 7 * 86400]
        rows.append({"t": time.time(), "task": task_id, "agent": agent, "model": model,
                     "tokens": int(tokens), "units": tokens * config.MODEL_WEIGHT.get(model, 1.0)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        tmp.replace(self.path)

    def used(self):
        rows, now = self._load(), time.time()
        u5 = sum(r["units"] for r in rows if r["t"] > now - 5 * 3600)
        u7 = sum(r["units"] for r in rows if r["t"] > now - 7 * 86400)
        return {"units_5h": u5, "cap_5h": config.WINDOW_5H_UNITS, "share_5h": u5 / config.WINDOW_5H_UNITS,
                "units_7d": u7, "cap_7d": config.WINDOW_7D_UNITS, "share_7d": u7 / config.WINDOW_7D_UNITS}

    def can_start_large_task(self, estimate_units):
        u = self.used()
        if (u["units_5h"] + estimate_units) / u["cap_5h"] > config.WINDOW_REFUSE_SHARE:
            return False, "5-hour window nearly used"
        if (u["units_7d"] + estimate_units) / u["cap_7d"] > config.WINDOW_REFUSE_SHARE:
            return False, "weekly window nearly used"
        return True, "ok"
