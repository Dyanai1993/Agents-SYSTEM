import json, re, unittest
from agentsys import config, ledger, loop, prompts, runner, schema, semantics
from tests.base import TmpHome
from tests.helpers import (DEV, NEEDS, REQ, REQ_C, plan, report, t_blocked, t_design, t_fail, t_pass, _check, _item)


def go(tid, backend, req=REQ):
    return loop.run_task(tid, req, backend=backend, env={}, settings_paths=[])


def roles(fb):
    return [c["role"] for c in fb.calls]


class LoopTests(TmpHome):
    def read(self, rel):
        return json.loads((self.home / rel).read_text())

    # ---------------------------------------------------------------- happy path and loops
    def test_happy_path_two_items_plus_final_regression(self):
        fb = runner.FakeBackend({"architect": plan(2), "developer": DEV, "tester": t_pass})
        r = go("t1", fb)
        self.assertEqual(r["status"], "ready_for_review")
        self.assertEqual(roles(fb), ["architect", "developer", "tester", "developer", "tester", "tester"])
        st = self.read("tasks/t1/state.json")
        self.assertEqual((st["state"], st["waiting_approval"]), ("review", True))
        for f in ("requirements.json", "plan.json", "dev_log.md", "test_report.json", "budget.json", "summary.md"):
            self.assertTrue((self.home / "tasks/t1" / f).exists(), f)
        self.assertEqual(json.loads(fb.calls[-1]["prompt"])["work_item"]["id"], "FINAL")

    def test_fix_loop_then_pass_and_report_forwarded(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [t_fail, t_pass]})
        self.assertEqual(go("t1", fb)["status"], "ready_for_review")
        dev_calls = [c for c in fb.calls if c["role"] == "developer"]
        self.assertEqual(len(dev_calls), 2)
        self.assertIn("AssertionError", dev_calls[1]["prompt"])
        self.assertEqual(ledger.Ledger("t1").data["fix_loops"], 1)
        # Review: record_fix_loop() used to run on a Ledger snapshotted before the loop started, so saving it
        # overwrote the calls/tokens runner.run_agent had recorded in between. Every real call must be counted.
        self.assertEqual(ledger.Ledger("t1").data["calls"], len(fb.calls))

    def test_requirement_change_after_final_done_forces_a_fresh_final_regression(self):
        """Review: `orch.py retry --answer` adds a constraint after final_done was already True (e.g. a
        delivery failure unrelated to the requirement). Without this, the new constraint is never checked:
        the next run makes zero calls and goes straight back to ready_for_review."""
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        r = go("t1", fb)
        self.assertEqual(r["status"], "ready_for_review")
        fb2 = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        req2 = dict(REQ, constraints=REQ["constraints"] + ["Clarification from the user: use argparse"])
        r2 = go("t1", fb2, req=req2)
        self.assertEqual(r2["status"], "ready_for_review")
        self.assertEqual(roles(fb2), ["tester"])          # only a fresh FINAL regression; no work item rebuilt
        self.assertEqual(json.loads(fb2.calls[-1]["prompt"])["work_item"]["id"], "FINAL")

    def test_fix_cap_stops_and_asks(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_fail})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("fix-loop cap", r["message"])
        self.assertEqual(len([c for c in fb.calls if c["role"] == "developer"]), 4)
        self.assertTrue((self.home / "tasks/t1/status_report.md").exists())
        self.assertEqual(self.read("tasks/t1/state.json")["state"], "blocked")

    def test_design_problem_replan_then_cap(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_design})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("re-plan cap", r["message"])
        self.assertEqual(roles(fb).count("architect"), 3)
        self.assertTrue((self.home / "tasks/t1/plan-v2.json").exists())
        # Review: record_replan() had the same stale-Ledger bug as record_fix_loop().
        self.assertEqual(ledger.Ledger("t1").data["calls"], len(fb.calls))

    def test_replan_gets_full_context_and_keeps_completed_items(self):
        fb = runner.FakeBackend({"architect": [plan(2), plan(2)], "developer": DEV, "tester": [t_pass, t_design, t_pass, t_pass, t_pass]})
        r = go("t1", fb)
        self.assertEqual(r["status"], "ready_for_review")
        replan = json.loads([c for c in fb.calls if c["role"] == "architect"][1]["prompt"])
        self.assertEqual(replan["mode"], "replan")
        self.assertEqual(replan["completed_item_ids"], ["W1"]); self.assertEqual(replan["failing_item"]["id"], "W2")
        self.assertIn("failing_report", replan)
        # W1 was completed and unchanged: it is NOT rebuilt after the replan
        self.assertEqual([json.loads(c["prompt"])["work_item"]["id"] for c in fb.calls if c["role"] == "developer"], ["W1", "W2", "W2"])

    # ---------------------------------------------------------------- independence and requirement coverage
    def test_tester_never_sees_developer_reasoning(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        go("t1", fb)
        for c in (c for c in fb.calls if c["role"] == "tester"):
            self.assertNotIn("SECRET-DEV-REASONING", c["prompt"])

    def test_constraints_and_original_criteria_reach_developer_and_tester(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        go("t1", fb, REQ_C)
        for role in ("developer", "tester"):
            p = json.loads([c for c in fb.calls if c["role"] == role][0]["prompt"])
            self.assertEqual(p["constraints"], [{"id": "C1", "text": "no third-party packages"}])
        dev = json.loads([c for c in fb.calls if c["role"] == "developer"][0]["prompt"])
        self.assertEqual(dev["work_item"]["original_criteria"], [{"id": "AC1", "text": "counts words"}])

    def test_constraint_neither_checked_nor_na_blocks(self):
        def tester(prompt):
            _, ids, _ = _item(prompt)
            return report("pass", [_check("all", "pass", ids)])         # C1 ignored
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": tester}), REQ_C)
        self.assertEqual(r["status"], "needs_input"); self.assertIn("constraint C1", r["message"])

    def test_tester_that_forgot_a_constraint_gets_one_chance_to_complete(self):
        """Live run T-0010: the summary said 'both constraints verified' but no check carried C1/C2."""
        def forgetful(prompt):
            _, ids, _ = _item(prompt)
            return report("pass", [_check("all", "pass", ids)], summary="All criteria and both constraints verified")
        def complete(prompt):
            p = json.loads(prompt)
            assert "rejection" in p and any("C1" in g for g in p["rejection"]["gaps"])
            _, ids, cons = _item(prompt)
            return report("pass", [_check("all", "pass", ids), _check("stdlib only", "pass", cons)])
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [forgetful, complete, t_pass]})
        r = go("t1", fb, REQ_C)
        self.assertEqual(r["status"], "ready_for_review")
        self.assertEqual(roles(fb).count("tester"), 3)               # item (2 tries) + final
        from agentsys import dashboard_data as dd
        self.assertTrue([e for e in dd.all_events("t1")[0] if "incomplete" in e["summary"]])

    def test_tester_that_stays_incomplete_ends_in_needs_input_after_exactly_two_tries(self):
        def forgetful(prompt):
            _, ids, _ = _item(prompt)
            return report("pass", [_check("all", "pass", ids)])
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": forgetful})
        r = go("t1", fb, REQ_C)
        self.assertEqual(r["status"], "needs_input"); self.assertIn("constraint C1", r["message"])
        self.assertEqual(roles(fb).count("tester"), 2)               # one retry, never a loop

    def test_skipped_checks_are_not_retried(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_blocked})
        go("t1", fb)
        self.assertEqual(roles(fb).count("tester"), 1)               # a tool/environment gap is a real block

    def test_plan_that_drops_a_requirement_criterion_is_rejected(self):
        req = dict(REQ, acceptance_criteria=["a", "b"])
        r = go("t1", runner.FakeBackend({"architect": plan(1)}), req)       # plan covers AC1 only
        self.assertEqual(r["status"], "blocked"); self.assertIn("AC2 is not covered", r["message"])

    def test_plan_too_big_for_budget_is_rejected_before_any_work(self):
        fb = runner.FakeBackend({"architect": plan(15, complexity="complex")})
        r = go("t1", fb)
        self.assertIn("split the task", r["message"]); self.assertEqual(roles(fb), ["architect"])

    # ---------------------------------------------------------------- verdict derivation and evidence
    def test_partial_coverage_cannot_pass(self):
        p = plan(1); p["work_items"][0]["acceptance_criteria"] = ["one", "two"]
        def tester(prompt):
            return report("pass", [_check("only first", "pass", ["W1.1"])])
        r = go("t1", runner.FakeBackend({"architect": p, "developer": DEV, "tester": tester}))
        self.assertEqual(r["status"], "needs_input"); self.assertIn("W1.2 has no passing check", r["message"])

    def test_blocked_report_with_a_failing_check_is_corrected_to_fail(self):
        def tester(prompt):
            _, ids, _ = _item(prompt)
            return report("blocked", [_check("bad", "fail", ids)])
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [tester, t_pass]})
        go("t1", fb)
        self.assertEqual(len([c for c in fb.calls if c["role"] == "developer"]), 2)   # treated as fail -> fix loop
        rep = self.read("tasks/t1/reports/test_report-001-W1.json")
        self.assertEqual((rep["verdict"], rep["verdict_reported"]), ("fail", "blocked"))

    def test_reported_fail_but_all_checks_pass_is_a_hard_error(self):
        def tester(prompt):
            _, ids, _ = _item(prompt)
            return report("fail", [_check("fine", "pass", ids)])
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": tester}))
        self.assertEqual(r["status"], "blocked"); self.assertIn("every check passed", r["message"])

    def test_fabricated_evidence_is_rejected(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass}, tool_logs={"tester": []})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("appears in the tool log", r["message"])

    def test_missing_tool_log_fails_closed_unless_warn_mode(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        orig = runner.FakeBackend.run
        def no_log(self, **kw):
            res = orig(self, **kw); res.tool_log = None; return res
        runner.FakeBackend.run = no_log
        try:
            r = go("t1", fb)
            self.assertIn("cannot be verified", r["message"])
            import os; os.environ["AGENTSYS_EVIDENCE"] = "warn"
            self.assertEqual(go("t2", runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass}))["status"], "ready_for_review")
        finally:
            runner.FakeBackend.run = orig

    def test_tester_blocked_asks_user(self):
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_blocked}))
        self.assertEqual(r["status"], "needs_input")

    def test_final_regression_failure_blocks(self):
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [t_pass, t_fail]})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("final regression failed", r["message"])

    def test_tokens_are_counted_once_per_call(self):
        """Live data: timeline.py showed 2x the real cost because message and agent_end both carried tokens."""
        from agentsys import dashboard_data as dd
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        go("t1", fb)
        logged = sum(e["tokens_estimate"] for e in dd.all_events("t1")[0])
        self.assertEqual(logged, ledger.Ledger("t1").data["tokens_est"])
        self.assertGreater(logged, 0)

    def test_failed_call_tokens_are_counted_once(self):
        from agentsys import dashboard_data as dd
        go("t1", runner.FakeBackend({"architect": {"bad": 1}}))
        self.assertEqual(sum(e["tokens_estimate"] for e in dd.all_events("t1")[0]), ledger.Ledger("t1").data["tokens_est"])

    def test_task_is_refused_when_the_window_is_nearly_used(self):
        import time
        w = ledger.Window()
        w.add("other", "developer", "sonnet", int(config.WINDOW_5H_UNITS * 0.9))
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertIn("window nearly used", r["message"])
        self.assertEqual(roles(fb), ["architect"])          # no build work started

    def test_estimate_follows_call_count(self):
        self.assertEqual(ledger.estimate_task_units(1), 4 * config.TOKENS_PER_CALL * config.FIX_MARGIN)
        self.assertGreater(ledger.estimate_task_units(3), ledger.estimate_task_units(1))

    # ---------------------------------------------------------------- explicit block protocol
    def test_architect_needs_input(self):
        fb = runner.FakeBackend({"architect": NEEDS})
        r = go("t1", fb)
        self.assertEqual(r["status"], "needs_input"); self.assertIn("Which database?", r["message"])
        self.assertEqual(roles(fb), ["architect"])

    def test_blocking_unknown_stops_before_any_agent_call(self):
        fb = runner.FakeBackend({"architect": plan(1)})
        r = go("t1", fb, dict(REQ, unknowns=[{"question": "Which cloud?", "blocking": True}]))
        self.assertEqual(r["status"], "needs_input"); self.assertEqual(fb.calls, [])

    def test_developer_blocked_user_input_vs_environment(self):
        dev = lambda kind: {"status": "blocked", "blocking_kind": kind, "blocking_reason": "why", "summary": "stopped", "files_changed": []}
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev("user_input")}))
        self.assertEqual(r["status"], "needs_input")
        r = go("t2", runner.FakeBackend({"architect": plan(1), "developer": dev("environment")}))
        self.assertEqual(r["status"], "blocked"); self.assertIn("not a question for the user", r["message"])

    def test_schema_failure_blocks_without_retry(self):
        fb = runner.FakeBackend({"architect": {"bad": 1}})
        r = go("t1", fb)
        self.assertEqual(r["status"], "blocked"); self.assertEqual(len(fb.calls), 1)

    def test_invalid_requirements_rejected(self):
        with self.assertRaises(schema.SchemaError):
            go("t1", runner.FakeBackend(), req={"goal": "x"})

    # ---------------------------------------------------------------- limits, resume, guard
    def test_usage_limit_pauses_and_resume_finishes(self):
        fb = runner.FakeBackend({"architect": plan(2), "developer": [DEV, runner.UsageLimitReached("5-hour limit reached"), DEV], "tester": t_pass})
        self.assertEqual(go("t1", fb)["status"], "paused")
        prog = self.read("tasks/t1/progress.json")
        self.assertEqual((prog["item"], prog["step"], sorted(prog["completed"])), (1, "dev", ["W1"]))
        fb2 = runner.FakeBackend({"developer": DEV, "tester": t_pass})
        self.assertEqual(go("t1", fb2)["status"], "ready_for_review")
        self.assertEqual(roles(fb2), ["developer", "tester", "tester"])       # no architect, item 1 not rebuilt

    def test_resume_after_crash_mid_step(self):
        class Crash(BaseException): pass
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": [Crash(), t_pass]})
        with self.assertRaises(Crash):
            go("t1", fb)
        self.assertEqual(self.read("tasks/t1/progress.json")["step"], "test")
        fb2 = runner.FakeBackend({"tester": t_pass})
        self.assertEqual(go("t1", fb2)["status"], "ready_for_review")
        self.assertEqual(roles(fb2), ["tester", "tester"])

    def test_billing_guard_blocks_the_loop(self):
        fb = runner.FakeBackend({"architect": plan(1)})
        r = loop.run_task("t1", REQ, backend=fb, env={"ANTHROPIC_API_KEY": "k"}, settings_paths=[])
        self.assertEqual(r["status"], "blocked"); self.assertEqual(fb.calls, [])

    def test_role_limits_come_from_config(self):
        seen = {}
        class B(runner.FakeBackend):
            def run(self, **kw):
                seen[kw["role"]] = kw["limits"]; return super().run(**kw)
        go("t1", B({"architect": plan(1), "developer": DEV, "tester": t_pass}))
        self.assertEqual(seen["developer"].max_turns, config.ROLE_LIMITS["developer"]["max_turns"])
        self.assertLess(seen["architect"].timeout_s, seen["developer"].timeout_s)

    # ---------------------------------------------------------------- git policy
    def ws(self, tid="t1"):
        return self.home / "tasks" / tid / "workspace"

    def test_commit_per_item_and_qa_commit(self):
        def dev(prompt):
            (self.ws() / f"{json.loads(prompt)['work_item']['id']}.txt").write_text("x"); return DEV
        def tester(prompt):
            (self.ws() / "qa_tests").mkdir(exist_ok=True); (self.ws() / "qa_tests" / "q.py").write_text("x"); return t_pass(prompt)
        go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev, "tester": tester}))
        log = loop._git(self.ws(), "log", "--format=%s").stdout.split("\n")
        self.assertIn("W1: item 1", log); self.assertIn("qa: W1", log)

    def test_developer_edits_to_qa_tests_are_reverted(self):
        qa = lambda: self.ws() / "qa_tests" / "q.py"
        def tester(prompt):
            if not qa().exists():                     # first tester run creates the QA test and fails the item
                qa().parent.mkdir(exist_ok=True); qa().write_text("orig"); return t_fail(prompt)
            return t_pass(prompt)
        calls = {"n": 0}
        def dev(prompt):
            calls["n"] += 1
            if calls["n"] == 2:
                qa().write_text("weakened")           # the fix run tries to hide the failure
            return DEV
        go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev, "tester": tester}))
        self.assertEqual(qa().read_text(), "orig")
        from agentsys import dashboard_data as dd
        self.assertTrue([e for e in dd.all_events("t1")[0] if e["event_type"] == "alert" and "protected" in e["summary"]])

    def test_a_staged_qa_weakening_is_still_reverted(self):
        """Review (third round): protect_qa had the same staging gap protect_config was fixed for last
        round - `checkout -- QA_DIR` alone is a no-op on a change the developer already `git add`ed, since
        it restores from the (still weakened) INDEX, not HEAD."""
        qa = lambda: self.ws() / "qa_tests" / "q.py"
        def tester(prompt):
            if not qa().exists():
                qa().parent.mkdir(exist_ok=True); qa().write_text("orig"); return t_fail(prompt)
            return t_pass(prompt)
        calls = {"n": 0}
        def dev(prompt):
            calls["n"] += 1
            if calls["n"] == 2:
                qa().write_text("weakened")
                loop._git(self.ws(), "add", "-A")      # the developer staged its own change
            return DEV
        go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev, "tester": tester}))
        self.assertEqual(qa().read_text(), "orig")
        self.assertFalse(loop._git(self.ws(), "status", "--porcelain").stdout.strip())

    def test_tester_editing_product_code_invalidates_report(self):
        def tester(prompt):
            (self.ws() / "app.py").write_text("tampered"); return t_pass(prompt)
        def dev(p):
            (self.ws() / "app.py").write_text("real"); return DEV
        r = go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev, "tester": tester}))
        self.assertEqual(r["status"], "blocked"); self.assertIn("modified product files", r["message"])
        self.assertEqual((self.ws() / "app.py").read_text(), "real")

    def test_blocked_developer_leaves_changes_uncommitted_and_reports_them(self):
        def dev(prompt):
            (self.ws() / "half.py").write_text("wip")
            return {"status": "blocked", "blocking_kind": "user_input", "blocking_reason": "?", "summary": "stop", "files_changed": ["half.py"]}
        go("t1", runner.FakeBackend({"architect": plan(1), "developer": dev}))
        self.assertNotIn("W1: item 1", loop._git(self.ws(), "log", "--format=%s").stdout)
        self.assertIn("half.py", (self.home / "tasks/t1/status_report.md").read_text())


class SemanticsTests(unittest.TestCase):
    def test_plan_errors(self):
        p = plan(2); p["work_items"][1]["depends_on"] = ["W2"]
        self.assertTrue(any("depends on itself" in e for e in semantics.plan_errors(p, {"AC1"}, 30)))
        p = plan(2); p["work_items"][0]["depends_on"] = ["W2"]; p["work_items"][1]["depends_on"] = []
        self.assertTrue(any("before its dependency" in e for e in semantics.plan_errors(p, {"AC1"}, 30)))   # also catches cycles
        p = plan(2); p["work_items"][1]["id"] = "W1"
        self.assertTrue(any("duplicate" in e for e in semantics.plan_errors(p, {"AC1"}, 30)))
        p = plan(2); p["work_items"][0]["depends_on"] = ["W9"]
        self.assertTrue(any("unknown item" in e for e in semantics.plan_errors(p, {"AC1"}, 30)))
        self.assertTrue(any("at most 3" in e for e in semantics.plan_errors(plan(4), {"AC1"}, 30)))
        self.assertEqual(semantics.plan_errors(plan(3), {"AC1"}, 30), [])

    def test_derive_verdict_order(self):
        r = report("pass", [_check("a", "fail", ["W1.1"]), _check("b", "skipped", ["W1.2"])])
        self.assertEqual(semantics.derive_verdict(r, ["W1.1", "W1.2"], [])[0], "fail")       # failure beats skipped
        r = report("pass", [_check("b", "skipped", ["W1.1"])])
        self.assertEqual(semantics.derive_verdict(r, ["W1.1"], [])[0], "blocked")
        r = report("pass", [_check("a", "pass", ["W1.1"])])
        self.assertEqual(semantics.derive_verdict(r, ["W1.1"], [])[0], "pass")

    def test_one_script_may_back_several_checks_through_its_output(self):
        """Live run T-0001: 3 tool calls, more checks. Batched scripts must be accepted when the output proves the run."""
        log = [{"tool": "Bash", "command": "bash qa.sh", "output": "CHECK missing_file: stderr=wc.py: nope.txt: No such file exit=1\nCHECK empty_file: 0 0 0 empty.txt"}]
        r = report("pass", [_check("missing", "pass", ["W1.1"], command="python wc.py nope.txt", evidence="stderr=wc.py: nope.txt: No such file exit=1"),
                            _check("empty", "pass", ["W1.2"], command="python wc.py empty.txt", evidence="0 0 0 empty.txt")])
        self.assertEqual(semantics.evidence_errors(r, log, strict=True), [])

    def test_windows_translated_paths_do_not_break_evidence(self):
        """Live run T-0001: the tester quoted /tmp/f.txt, Git Bash printed C:/Users/.../f.txt."""
        log = [{"tool": "Bash", "command": "f=$(mktemp); printf 'a b\\nc d e\\n' > $f; python wc.py $f",
                "output": "2 5 24 C:/Users/dyana/AppData/Local/Temp/tmp.x9/f.txt\nwc.py: nope.txt: No such file or directory"}]
        r = report("pass", [_check("two lines", "pass", ["W1.1"], command="python wc.py /tmp/f.txt", evidence="2 5 24 /tmp/f.txt"),
                            _check("missing", "pass", ["W1.2"], command="python wc.py nope.txt; echo $?",
                                   evidence="stderr: wc.py: nope.txt: No such file or directory")])
        self.assertEqual(semantics.evidence_errors(r, log, strict=True), [])
        wrong_numbers = report("pass", [_check("two lines", "pass", ["W1.1"], command="python wc.py /tmp/other.txt", evidence="9 9 99 /tmp/other.txt")])
        self.assertTrue(semantics.evidence_errors(wrong_numbers, log, strict=True))   # masking the path never hides wrong numbers

    def test_fabricated_or_short_evidence_is_still_rejected(self):
        log = [{"tool": "Bash", "command": "python -m unittest", "output": "Ran 3 tests OK"}]
        invented = report("pass", [_check("x", "pass", ["W1.1"], command="python other.py", evidence="all 12 widgets verified fine")])
        self.assertTrue(semantics.evidence_errors(invented, log, strict=True))
        short = report("pass", [_check("x", "pass", ["W1.1"], command="python other.py", evidence="OK")])
        self.assertTrue(semantics.evidence_errors(short, log, strict=True))         # 'OK' appears in the output but proves nothing (< 6 chars)
        exit_only = report("pass", [_check("x", "pass", ["W1.1"], command="python other.py", evidence="", exit_code=0)])
        self.assertTrue(semantics.evidence_errors(exit_only, log, strict=True))     # exit_code needs a matching command
        labelled_fake = report("pass", [_check("x", "pass", ["W1.1"], command="zzz", evidence="stderr: foo.py: x.txt: No such file or directory")])
        log2 = [{"tool": "Bash", "command": "python wc.py nope.txt", "output": "wc.py: nope.txt: No such file or directory"}]
        self.assertTrue(semantics.evidence_errors(labelled_fake, log2, strict=True))  # only the generic words match: 63% < 75%
        shortened = report("pass", [_check("x", "pass", ["W1.1"], command="zzz", evidence="Ran 3 tests ... OK")])
        self.assertEqual(semantics.evidence_errors(shortened, log, strict=True), [])   # real output, shortened with '...'

    def test_command_really_ran_but_claimed_outcome_is_fabricated(self):
        """Review (third round): a command match alone, with no output check at all, let a report claim
        ANY outcome for a command that genuinely ran - including the opposite of what really happened."""
        log = [{"tool": "Bash", "command": "python -m unittest", "output": "FAILED (failures=3)"}]
        lied = report("pass", [_check("x", "pass", ["W1.1"], command="python -m unittest", evidence="all 50 tests passed, everything is green")])
        self.assertTrue(semantics.evidence_errors(lied, log, strict=True))
        # a command merely echoed as literal text (never run) is rejected too once evidence is substantive
        echoed = [{"tool": "Bash", "command": 'echo "python -m unittest"', "output": "python -m unittest"}]
        fake_run = report("pass", [_check("x", "pass", ["W1.1"], command="python -m unittest", evidence="Ran 7 tests, all green, no failures")])
        self.assertTrue(semantics.evidence_errors(fake_run, echoed, strict=True))
        # the command really ran AND the evidence matches ITS OWN output: still accepted
        honest = report("pass", [_check("x", "pass", ["W1.1"], command="python -m unittest", evidence="FAILED (failures=3)")])
        self.assertEqual(semantics.evidence_errors(honest, log, strict=True), [])

    def test_a_shared_label_cannot_smuggle_a_different_verdict_or_value(self):
        """Review (fourth round): the 75%-of-evidence ratio let a long shared LABEL dominate the match while
        the actual differing payload (the part that does the claiming) was never itself verified - a
        12-of-16-character shared prefix "test_result:" cleared 75% even though the real verdict differed."""
        log = [{"tool": "Bash", "command": "python -m unittest", "output": "test_result: FAIL"}]
        wrong_verdict = report("pass", [_check("x", "pass", ["W1.1"], command="python -m unittest", evidence="test_result: PASS")])
        self.assertTrue(semantics.evidence_errors(wrong_verdict, log, strict=True))
        log2 = [{"tool": "Bash", "command": "cat report.json", "output": "revenue: 45.0"}]
        wrong_number = report("pass", [_check("x", "pass", ["W1.1"], command="cat report.json", evidence="revenue: 57.5")])
        self.assertTrue(semantics.evidence_errors(wrong_number, log2, strict=True))
        # the honest case (label + the REAL trailing value) must still pass
        honest = report("pass", [_check("x", "pass", ["W1.1"], command="python -m unittest", evidence="test_result: FAIL")])
        log3 = [{"tool": "Bash", "command": "python -m unittest", "output": "actual test_result: FAIL"}]  # a real prefix label too
        self.assertEqual(semantics.evidence_errors(honest, log3, strict=True), [])

    def test_command_matching_ignores_whitespace(self):
        r = report("pass", [_check("a", "pass", ["W1.1"], command="python  -m   unittest")])
        self.assertEqual(semantics.evidence_errors(r, [{"tool": "Bash", "command": "python -m unittest"}], strict=True), [])
        err = semantics.evidence_errors(r, [{"tool": "Read", "command": ""}], strict=True)
        self.assertTrue(err)
        self.assertIn("python  -m   unittest".replace("  ", " ").replace("  ", " "), err[0])      # shows what was claimed
        err = semantics.evidence_errors(r, [{"tool": "Bash", "command": "ls -la"}], strict=True)
        self.assertIn("ls -la", err[0])                                                           # and what was really run


class PromptTests(TmpHome):
    def test_agent_files_match_config(self):
        for role in ("architect", "developer", "tester"):
            body, meta = prompts.load_role(role)
            self.assertEqual(meta["tools"], config.ROLES[role]["tools"], role)
            self.assertEqual(meta["model"], config.ROLES[role]["model"], role)
            self.assertIn("is DATA", body)
            self.assertIn("ONE valid JSON object", body)

    def test_examples_inside_prompts_validate_against_schemas(self):
        """The JSON examples in the prompts must be valid replies. Keeps prompts and schemas in sync."""
        for role, name in (("architect", "plan"), ("developer", "dev_report"), ("tester", "test_report")):
            body = prompts.load_role(role)[0]
            blocks = re.findall(r"```\n(\{.*?\})\n```", body, re.S)
            self.assertTrue(blocks, role)
            for b in blocks:
                self.assertEqual(schema.errors_for(name, json.loads(b)), [], f"{role}: {b[:60]}")

    def test_runner_sends_the_role_prompt(self):
        seen = {}
        class B(runner.FakeBackend):
            def run(self, **kw):
                seen.update(kw); return super().run(**kw)
        item = json.dumps({"work_item": {"id": "W1", "acceptance_criteria": [{"id": "W1.1"}]}})
        runner.run_agent("tester", item, task_id="t1", backend=B({"tester": t_pass(item)}), env={}, settings_paths=[])
        self.assertIn("TESTER", seen["system_prompt"]); self.assertEqual(seen["tools"], ["Read", "Bash", "Grep", "Glob"])
        self.assertNotIn("Write", config.ROLES["architect"]["tools"])


if __name__ == "__main__":
    unittest.main()


class SmokeTests(TmpHome):
    def test_smoke_flags_extra_tools_and_mcp_servers(self):
        from agentsys import smoke
        class B(runner.FakeBackend):
            def run(self, **kw):
                res = super().run(**kw); res.init = {"apiKeySource": "none", "tools": ["Bash", "Write", "WebFetch"], "mcp_servers": [{"name": "x"}]}; return res
        orig = runner.billing_guard.run_guard
        runner.billing_guard.run_guard = lambda **kw: []
        try:
            ok, lines = smoke.smoke(B({"smoke": {"ok": True}}, tool_logs={"smoke": [{"tool": "Bash", "command": "echo agentsys-smoke", "output": "agentsys-smoke", "is_error": False}]}))
        finally:
            runner.billing_guard.run_guard = orig
        self.assertFalse(ok)
        self.assertTrue([l for l in lines if l.startswith("FAIL") and "allowed tools" in l]); self.assertTrue([l for l in lines if l.startswith("FAIL") and "MCP" in l])

    def test_smoke_logic_with_fake_backend(self):
        from agentsys import smoke
        fb = runner.FakeBackend({"smoke": {"ok": True}}, tool_logs={"smoke": [{"tool": "Bash", "command": "echo agentsys-smoke", "output": "agentsys-smoke", "is_error": False}]})
        orig = runner.billing_guard.run_guard
        runner.billing_guard.run_guard = lambda **kw: []      # the container has ANTHROPIC_BASE_URL; logic only here
        try:
            ok, lines = smoke.smoke(fb)
        finally:
            runner.billing_guard.run_guard = orig
        self.assertTrue(ok, lines)
        fb2 = runner.FakeBackend({"smoke": {"ok": True}}, api_key_source="user")
        runner.billing_guard.run_guard = lambda **kw: []
        try:
            with self.assertRaises(runner.billing_guard.BillingGuardError):
                smoke.smoke(fb2)                                  # API-key credential source is fatal
        finally:
            runner.billing_guard.run_guard = orig
