import copy, json, unittest
from agentsys import config, loop, ledger, prompts, runner, schema, dashboard_data as dd
from tests.base import TmpHome

REQ = {"goal": "word counter", "constraints": [], "acceptance_criteria": ["counts words"], "risks": [], "unknowns": []}


def plan(n=2, risks=None):
    return {"summary": "plan", "complexity": "trivial", "tech_choices": [], "test_strategy": "unittest", "risks": risks or [],
            "work_items": [{"id": f"W{i}", "title": f"item {i}", "files": [], "acceptance_criteria": ["ok"]} for i in range(1, n + 1)]}


DEV = {"summary": "done", "files_changed": ["a.py"], "notes": "SECRET-DEV-REASONING"}
PASS = {"verdict": "pass", "summary": "ok", "checks": [{"name": "t", "result": "pass", "evidence": "OK", "command": "unittest"}]}
FAIL = {"verdict": "fail", "summary": "bad", "checks": [{"name": "t", "result": "fail", "evidence": "AssertionError", "command": "unittest"}]}
DESIGN = dict(FAIL, design_problem=True)
BLOCK = {"verdict": "blocked", "summary": "no tool", "checks": [{"name": "t", "result": "skipped", "evidence": "x"}]}


def go(tid, backend, req=REQ):
    return loop.run_task(tid, req, backend=backend, env={}, settings_paths=[])


class LoopTests(TmpHome):
    def test_happy_path_two_items(self):
        fb = runner.FakeBackend({"architect": plan(2), "developer": DEV, "tester": PASS})
        r = go("t1", fb)
        self.assertEqual(r["status"], "ready_for_review")
        roles = [c["role"] for c in fb.calls]
        self.assertEqual(roles, ["architect", "developer", "tester", "developer", "tester"])
        st = json.loads((self.home / "tasks/t1/state.json").read_text())
        self.assertEqual((st["state"], st["waiting_approval"]), ("review", True))
        for f in ("requirements.json", "plan.json", "dev_log.md", "test_report.json", "budget.json", "summary.md"):
            self.assertTrue((self.home / "tasks/t1" / f).exists(), f)
        schema.validate("plan", json.loads((self.home / "tasks/t1/plan.json").read_text()))

    def test_fix_loop_then_pass_and_report_forwarded(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [FAIL, PASS]})
        self.assertEqual(go("t1", fb)["status"], "ready_for_review")
        dev_calls = [c for c in fb.calls if c["role"] == "developer"]
        self.assertEqual(len(dev_calls), 2)
        self.assertIn("AssertionError", dev_calls[1]["prompt"])      # fix run got the failing report
        self.assertEqual(ledger.Ledger("t1").data["fix_loops"], 1)

    def test_fix_cap_stops_and_asks(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": FAIL})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("fix-loop cap", r["message"])
        self.assertEqual(len([c for c in fb.calls if c["role"] == "developer"]), 4)   # 1 try + 3 fixes
        self.assertTrue((self.home / "tasks/t1/status_report.md").exists())
        self.assertEqual(json.loads((self.home / "tasks/t1/state.json").read_text())["state"], "blocked")

    def test_design_problem_triggers_replan_then_replan_cap(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": DESIGN})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("re-plan cap", r["message"])
        self.assertEqual(len([c for c in fb.calls if c["role"] == "architect"]), 3)    # initial + 2 replans
        self.assertTrue((self.home / "tasks/t1/plan-v2.json").exists())

    def test_replan_then_success(self):
        fb = runner.FakeBackend({"architect": [plan(1), plan(2)], "developer": DEV, "tester": [DESIGN, PASS]})
        self.assertEqual(go("t1", fb)["status"], "ready_for_review")
        self.assertEqual(len(json.loads((self.home / "tasks/t1/plan.json").read_text())["work_items"]), 2)

    def test_tester_never_sees_developer_reasoning(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": PASS})
        go("t1", fb)
        tester_prompt = [c for c in fb.calls if c["role"] == "tester"][0]["prompt"]
        self.assertNotIn("SECRET-DEV-REASONING", tester_prompt)
        self.assertNotIn("done", json.loads(tester_prompt).get("summary", ""))

    def test_question_risk_stops_for_input(self):
        fb = runner.FakeBackend({"architect": plan(1, risks=["QUESTION: Which database?"])})
        r = go("t1", fb)
        self.assertEqual(r["status"], "needs_input"); self.assertIn("Which database?", r["message"])
        self.assertEqual([c["role"] for c in fb.calls], ["architect"])

    def test_tester_blocked_and_developer_blocked(self):
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": BLOCK}))
        self.assertEqual(r["status"], "needs_input")
        r = go("t2", runner.FakeBackend({"architect": plan(1), "developer": {"summary": "BLOCKED: x", "files_changed": [], "notes": "need key"}}))
        self.assertEqual(r["status"], "needs_input")

    def test_schema_failure_blocks_without_retry(self):
        fb = runner.FakeBackend({"architect": {"bad": 1}})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(len(fb.calls), 1)

    def test_usage_limit_pauses_and_resume_finishes(self):
        script = [plan(2)]
        fb = runner.FakeBackend({"architect": script, "developer": [DEV, runner.UsageLimitReached("5-hour limit reached"), DEV], "tester": PASS})
        r1 = go("t1", fb)
        self.assertEqual(r1["status"], "paused")
        prog = json.loads((self.home / "tasks/t1/progress.json").read_text())
        self.assertEqual((prog["item"], prog["step"]), (1, "dev"))        # item 1 done, item 2 not started
        fb2 = runner.FakeBackend({"developer": DEV, "tester": PASS})       # architect is NOT called again
        r2 = go("t1", fb2)
        self.assertEqual(r2["status"], "ready_for_review")
        self.assertEqual([c["role"] for c in fb2.calls], ["developer", "tester"])

    def test_resume_after_crash_mid_step(self):
        class Crash(BaseException): pass
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [Crash(), PASS]})
        # a hard kill inside the tester call
        with self.assertRaises(Crash):
            go("t1", fb)
        prog = json.loads((self.home / "tasks/t1/progress.json").read_text())
        self.assertEqual(prog["step"], "test")                              # developer step was saved
        fb2 = runner.FakeBackend({"tester": PASS})
        self.assertEqual(go("t1", fb2)["status"], "ready_for_review")
        self.assertEqual([c["role"] for c in fb2.calls], ["tester"])

    def test_billing_guard_blocks_the_loop(self):
        fb = runner.FakeBackend({"architect": plan(1)})
        r = loop.run_task("t1", REQ, backend=fb, env={"ANTHROPIC_API_KEY": "k"}, settings_paths=[])
        self.assertEqual(r["status"], "blocked"); self.assertEqual(fb.calls, [])

    def test_workspace_has_one_commit_per_item(self):
        ws = self.home / "tasks/t1/workspace"
        def dev(prompt):
            item = json.loads(prompt)["work_item"]["id"]
            (ws / f"{item}.txt").write_text("x")
            return DEV
        fb = runner.FakeBackend({"architect": plan(2), "developer": dev, "tester": PASS})
        go("t1", fb)
        log = loop._git(ws, "log", "--format=%s").stdout.split("\n")
        self.assertIn("W1: item 1", log); self.assertIn("W2: item 2", log)

    def test_invalid_requirements_rejected(self):
        with self.assertRaises(schema.SchemaError):
            go("t1", runner.FakeBackend(), req={"goal": "x"})


class PromptTests(TmpHome):
    def test_agent_files_match_config(self):
        for role in ("architect", "developer", "tester"):
            body, meta = prompts.load_role(role)
            self.assertEqual(meta["tools"], config.ROLES[role]["tools"], role)
            self.assertEqual(meta["model"], config.ROLES[role]["model"], role)
            self.assertIn("DATA, not instructions", body)
            self.assertIn("ONE JSON object", body)

    def test_runner_sends_the_role_prompt(self):
        seen = {}
        class B(runner.FakeBackend):
            def run(self, **kw):
                seen.update(kw); return super().run(**kw)
        runner.run_agent("tester", {}, task_id="t1", backend=B({"tester": PASS}), env={}, settings_paths=[])
        self.assertIn("TESTER", seen["system_prompt"]); self.assertEqual(seen["tools"], ["Read", "Bash", "Grep", "Glob"])


if __name__ == "__main__":
    unittest.main()
