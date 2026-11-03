import json, os, stat, time, unittest
from agentsys import config, ledger, logger, runner, schema, dashboard_data as dd
from tests.base import TmpHome, fake_claude

PLAN = {"summary": "s", "complexity": "trivial", "tech_choices": [], "test_strategy": "t", "risks": [],
        "work_items": [{"id": "W1", "title": "t", "files": [], "acceptance_criteria": ["x"]}]}
ENV = {}


def run(role, backend, **kw):
    return runner.run_agent(role, {"x": 1}, task_id=kw.pop("task_id", "t1"), backend=backend, env=ENV, settings_paths=[], **kw)


class SchemaTests(TmpHome):
    def test_all_schemas_are_valid(self):
        for p in schema.schemas_dir().glob("*.schema.json"):
            schema._validator(p.name.split(".")[0])

    def test_pass_verdict_needs_evidence(self):
        bad = {"verdict": "pass", "summary": "s", "checks": [{"name": "a", "result": "pass", "evidence": ""}]}
        self.assertTrue(schema.errors_for("test_report", bad))
        bad2 = {"verdict": "pass", "summary": "s", "checks": [{"name": "a", "result": "fail", "evidence": "boom"}]}
        self.assertTrue(schema.errors_for("test_report", bad2))   # pass verdict with a failing check
        ok = {"verdict": "pass", "summary": "s", "checks": [{"name": "a", "result": "pass", "evidence": "OK"}]}
        self.assertEqual(schema.errors_for("test_report", ok), [])


class LoggerTests(TmpHome):
    def test_event_with_payload_ref(self):
        e = logger.log_event("t1", "tester", "message", "hi", payload={"a": 1}, from_="tester", to="orchestrator")
        self.assertTrue(e["payload_ref"].startswith("tasks/t1/payloads/"))
        self.assertEqual(json.loads((self.home / e["payload_ref"]).read_text()), {"a": 1})
        line = (self.home / "logs" / "t1.jsonl").read_text().strip()
        schema.validate("event", json.loads(line))

    def test_bad_status_is_rejected_and_not_written(self):
        with self.assertRaises(schema.SchemaError):
            logger.log_event("t1", "a", "message", "x", status="weird")
        self.assertFalse((self.home / "logs" / "t1.jsonl").exists())

    def test_path_traversal_task_id(self):
        for bad in ("../x", "a/b", "", ".."):
            with self.assertRaises(ValueError):
                logger.log_event(bad, "a", "message", "x")


class LedgerTests(TmpHome):
    def test_call_cap(self):
        L = ledger.Ledger("t1", caps={"call_cap": 2})
        L.record_call("developer", "sonnet", 100); L.record_call("developer", "sonnet", 100)
        with self.assertRaises(ledger.BudgetExceeded):
            L.check_can_call()

    def test_fix_and_replan_caps(self):
        L = ledger.Ledger("t1")
        for _ in range(3): L.record_fix_loop()
        with self.assertRaises(ledger.BudgetExceeded): L.record_fix_loop()
        for _ in range(2): L.record_replan()
        with self.assertRaises(ledger.BudgetExceeded): L.record_replan()

    def test_timeout(self):
        L = ledger.Ledger("t1", caps={"timeout_s": -1})
        with self.assertRaises(ledger.BudgetExceeded): L.check_can_call()

    def test_persisted_and_resumed(self):
        ledger.Ledger("t1").record_call("a", "haiku", 50)
        self.assertEqual(ledger.Ledger("t1").data["calls"], 1)

    def test_window_refuses_large_task(self):
        w = ledger.Window()
        self.assertTrue(w.can_start_large_task(1000)[0])
        self.assertFalse(w.can_start_large_task(config.WINDOW_5H_UNITS)[0])


class RunnerTests(TmpHome):
    def test_happy_path_logs_and_validates(self):
        r = run("architect", runner.FakeBackend({"architect": PLAN}))
        self.assertTrue(r.ok); self.assertEqual(r.output["complexity"], "trivial")
        types = [e["event_type"] for e in dd.all_events("t1")[0]]
        self.assertEqual(types, ["agent_start", "message", "agent_end"])
        self.assertEqual(ledger.Ledger("t1").data["calls"], 1)

    def test_schema_failure_is_reported_not_raised(self):
        r = run("architect", runner.FakeBackend({"architect": {"nope": 1}}))
        self.assertFalse(r.ok); self.assertIn("schema_validation", r.error)
        self.assertEqual(dd.all_events("t1")[0][-1]["status"], "error")

    def test_json_in_code_fence(self):
        r = run("architect", runner.FakeBackend({"architect": "Here:\n```json\n" + json.dumps(PLAN) + "\n```"}))
        self.assertTrue(r.ok)

    def test_usage_limit_checkpoints_and_stops(self):
        fb = runner.FakeBackend({"developer": runner.UsageLimitReached("5-hour limit reached")})
        with self.assertRaises(runner.UsageLimitReached):
            run("developer", fb)
        ck = json.loads((self.home / "tasks/t1/checkpoint.json").read_text())
        self.assertEqual(ck["reason"], "usage_limit")
        self.assertEqual(len(ledger.Ledger("t1").data["limit_hits"]), 1)
        self.assertEqual(len(fb.calls), 1)      # one try only: no tight retry loop

    def test_budget_cap_stops_before_backend(self):
        ledger.Ledger("t1", caps={"call_cap": 1}).record_call("x", "sonnet", 1)
        fb = runner.FakeBackend({"developer": {}})
        with self.assertRaises(ledger.BudgetExceeded):
            run("developer", fb)
        self.assertEqual(fb.calls, [])

    def test_cli_backend_with_fake_claude_binary(self):
        env = fake_claude(self.home, "import json,sys\nsys.stdin.read()\n"
                       "print(json.dumps({'type':'system','subtype':'init','apiKeySource':'none'}))\n"
                       "print(json.dumps({'type':'result','is_error':False,'result':json.dumps({'ok':1}),"
                       "'num_turns':2,'usage':{'input_tokens':10,'output_tokens':5}}))\n")
        r = runner.run_agent("developer", {"a": 1}, task_id="t3", backend=runner.CliBackend(), env=env, settings_paths=[])
        self.assertTrue(r.ok); self.assertEqual(r.output, {"ok": 1}); self.assertEqual(r.api_key_source, "none")

    def test_cli_backend_detects_usage_limit(self):
        env = fake_claude(self.home, "import sys\nsys.stdin.read()\nprint('Claude usage limit reached', file=sys.stderr)\nsys.exit(1)\n")
        with self.assertRaises(runner.UsageLimitReached):
            runner.run_agent("developer", {}, task_id="t4", backend=runner.CliBackend(), env=env, settings_paths=[])

    def test_cli_backend_without_init_fails_closed(self):
        env = fake_claude(self.home, "import json,sys\nsys.stdin.read()\nprint(json.dumps({'type':'result','is_error':False,'result':'{}','usage':{}}))\n")
        from agentsys.billing_guard import BillingGuardError
        with self.assertRaises(BillingGuardError):
            runner.run_agent("developer", {}, task_id="t5", backend=runner.CliBackend(), env=env, settings_paths=[])


class DashboardDataTests(TmpHome):
    def test_bad_lines_are_skipped_and_counted(self):
        logger.log_event("t1", "a", "message", "good")
        with open(self.home / "logs" / "t1.jsonl", "a") as f:
            f.write("garbage\n{\"x\":1}\n")
        events, bad = dd.all_events()
        self.assertEqual(len(events), 1); self.assertEqual(bad, 2)

    def test_payload_path_is_restricted(self):
        self.assertIsNone(dd.payload("../../etc/passwd"))
        self.assertIsNone(dd.payload("tasks/t1/budget.json"))
        e = logger.log_event("t1", "a", "message", "x", payload={"k": 1})
        self.assertEqual(dd.payload(e["payload_ref"]), {"k": 1})

    def test_overview(self):
        from agentsys import tasks
        tasks.set_state("t1", "build", title="T", current_agent="developer")
        o = dd.overview()
        self.assertEqual(o["tasks"][0]["state"], "build")


class EvalSelfCheck(unittest.TestCase):
    def test_every_eval_check_is_sound(self):
        import subprocess, sys
        from pathlib import Path
        r = subprocess.run([sys.executable, str(Path(__file__).parent.parent / "evals" / "run_evals.py"), "selfcheck"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
