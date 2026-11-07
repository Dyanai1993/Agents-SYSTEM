"""The benchmark brief (T-0026): a check must prove the criterion it names. False passes are rejected; legitimate
expected failures, batched scripts, suite-level and source-level criteria still pass; device / deployment / visual
checks that were not performed are reported as unverified (never a pass, shown to the human)."""
from agentsys import schema, semantics
from tests.base import TmpHome
from tests.helpers import report, _check

BEHAVIOUR = {"W1.1": "Activating Download with the keyboard shows the progress region and moves focus to it"}


def errs(checks, log, criteria=BEHAVIOUR):
    r = report("pass", checks)
    return semantics.evidence_errors(r, log, strict=True) + semantics.evidence_quality_errors(r, criteria)


class FalsePassTests(TmpHome):
    def test_an_aggregate_count_does_not_prove_a_behaviour(self):
        log = [{"tool": "Bash", "command": "npx vitest run", "output": "Test Files  4 passed (4)\n      Tests  23 passed (23)"}]
        bad = [_check("keyboard download", "pass", ["W1.1"], command="npx vitest run", evidence="Tests  23 passed (23)")]
        self.assertTrue(any("aggregate count" in e for e in errs(bad, log)))

    def test_an_aggregate_count_does_prove_a_suite_criterion(self):
        log = [{"tool": "Bash", "command": "npx vitest run", "output": "Tests  23 passed (23)"}]
        ok = [_check("suite", "pass", ["W1.1"], command="npx vitest run", evidence="Tests  23 passed (23)")]
        self.assertEqual(errs(ok, log, {"W1.1": "The test suite completes with no errors"}), [])

    def test_a_grep_of_source_does_not_prove_a_behaviour_but_proves_a_source_criterion(self):
        log = [{"tool": "Bash", "command": "grep -n 'aria-live' src/DownloadPanel.tsx", "output": "41: <div aria-live=\"polite\" role=\"status\">"}]
        check = [_check("live region", "pass", ["W1.1"], command="grep -n 'aria-live' src/DownloadPanel.tsx",
                        evidence="<div aria-live=\"polite\" role=\"status\">")]
        self.assertTrue(any("text search" in e for e in errs(check, log)))
        self.assertEqual(errs(check, log, {"W1.1": "UI strings are defined in locale files, not hard-coded"}), [])

    def test_a_test_name_from_grep_never_backs_a_run_claim(self):
        log = [{"tool": "Bash", "command": "grep -rn 'keyboard download' e2e/", "output": "e2e/a11y.spec.ts:12: test('keyboard download works'"}]
        bad = [_check("keyboard download", "pass", ["W1.1"], command="npx playwright test", evidence="test('keyboard download works'")]
        self.assertTrue(errs(bad, log))

    def test_an_expected_failure_and_a_batched_script_still_pass(self):
        log = [{"tool": "Bash", "command": "bash qa.sh",
                "output": "CASE corrupt-asset: download failed: hash img/1.png; active=A offline=ok\nCASE keyboard: focus=progressbar"}]
        ok = [_check("corrupt asset keeps A", "pass", ["W1.1"], command="node qa/corrupt.mjs",
                     evidence="download failed: hash img/1.png; active=A offline=ok"),
              _check("focus after download", "pass", ["W1.1"], command="node qa/keyboard.mjs", evidence="CASE keyboard: focus=progressbar")]
        self.assertEqual(errs(ok, log), [])


class UnverifiedTests(TmpHome):
    def phone(self, because="device"):
        return _check("installs on a phone", "unverified", ["AC6"], command="", evidence="needs a real Android/iPhone and HTTPS hosting",
                      not_verified_because=because)

    def test_unverified_is_neither_a_pass_nor_a_blocker_and_is_listed(self):
        r = report("pass", [_check("build", "pass", ["AC1"], command="npm run build", evidence="built in 2.1s"), self.phone()])
        schema.validate("test_report", r)
        log = [{"tool": "Bash", "command": "npm run build", "output": "built in 2.1s"}]
        self.assertEqual(semantics.process_test_report(r, ["AC1", "AC6"], [], log), [])
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual(r["unverified"][0]["criterion_ids"], ["AC6"]); self.assertEqual(r["unverified"][0]["because"], "device")

    def test_unverified_items_reach_the_review_and_the_merge_approval(self):
        import json
        from agentsys import approvals, dashboard_data as dd, orchestrator
        from tests.helpers import REQ
        from tests.test_phase3 import fb
        req = dict(REQ, acceptance_criteria=["counts words", "installs on Android and iPhone from a shareable link"])

        def tester(prompt):
            p = json.loads(prompt)
            ids = [c["id"] for c in p["work_item"]["acceptance_criteria"]]
            checks = [_check("counts", "pass", [i]) for i in ids if i != "AC2"]
            if "AC2" in ids:
                checks.append(self.phone() | {"criterion_ids": ["AC2"]})
            return report("pass", checks)

        def dev(prompt):
            (self.home / "tasks/t1/workspace/app.py").write_text("x = 1\n")
            from tests.helpers import DEV
            return DEV
        from tests.helpers import plan
        orchestrator.submit("t1", requirements=req)
        e = orchestrator.process("t1", backend=fb(architect=plan(1, covers=("AC1", "AC2")), developer=dev, tester=tester), env={}, settings_paths=[])
        self.assertEqual(e["status"], "waiting_approval", e["note"])
        ap = approvals.latest("t1", "merge")[0]
        self.assertIn("NOT VERIFIED", ap["context"]); self.assertIn("AC2", ap["context"])
        crit = {c["id"]: c for c in dd.review_view("t1")["criteria"]}
        self.assertTrue(crit["AC2"]["unverified"]); self.assertFalse(crit["AC2"]["passed"]); self.assertTrue(crit["AC1"]["passed"])

    def test_unverified_needs_a_reason_from_the_list(self):
        bad = report("pass", [self.phone()]); del bad["checks"][0]["not_verified_because"]
        with self.assertRaises(schema.SchemaError):
            schema.validate("test_report", bad)
        with self.assertRaises(schema.SchemaError):
            schema.validate("test_report", report("pass", [self.phone(because="too lazy")]))

    def test_a_criterion_with_nothing_but_skipped_still_blocks(self):
        r = report("blocked", [_check("installs", "skipped", ["AC6"])])
        verdict, reasons = semantics.derive_verdict(r, ["AC6"], [])
        self.assertEqual(verdict, "blocked")


# ------------------------------------------------------------------ benchmark phase 4: first-use criteria
FIRST_USE = {"W1.2": "The first screen shows one prominent first action (Download) with a short explanation of it"}
BUILD = {"tool": "Bash", "command": "npm run build",
         "output": "vite v5.4.2 building for production...\n\u2713 34 modules transformed.\ndist/index.html  0.62 kB\n\u2713 built in 1.24s"}
EVAL = {"tool": "Bash", "command": "node qa/first_use.mjs",
        "output": "PASS  FU1 one clear first action - 1 primary button(s) (\"\u05d4\u05d5\u05e8\u05d3\u05d4\")\n"
                  "PASS  FU2 guidance - \"\u05de\u05d5\u05e8\u05d9\u05d3\u05d9\u05dd \u05e4\u05e2\u05dd \u05d0\u05d7\u05ea\""}


class FirstUseFalsePassTests(TmpHome):
    """A first-use criterion (what a first-time user sees and does) is proven only by measuring the rendered screen.
    Real output of an unrelated run - a build, an install, a server start, an echo of the claim, a grep of source -
    must be rejected; the measured output must be accepted."""

    def bad(self, log, command, evidence, criteria=FIRST_USE):
        return errs([_check("first action", "pass", ["W1.2"], command=command, evidence=evidence)], log, criteria)

    def test_a_build_line_does_not_prove_a_first_use_criterion(self):
        for ev in ("\u2713 built in 1.24s", "\u2713 34 modules transformed. || \u2713 built in 1.24s", "dist/index.html  0.62 kB"):
            e = self.bad([BUILD], "npm run build", ev)
            self.assertTrue(any("build/status line" in x for x in e), (ev, e))

    def test_a_build_line_does_prove_a_build_criterion(self):
        self.assertEqual(self.bad([BUILD], "npm run build", "\u2713 built in 1.24s",
                                  {"W1.2": "The production build completes with no errors"}), [])

    def test_a_dev_server_or_install_line_does_not_prove_it(self):
        dev = {"tool": "Bash", "command": "npm run dev", "output": "  VITE v5.4.2  ready in 312 ms\n\n  \u279c  Local:   http://localhost:5173/"}
        inst = {"tool": "Bash", "command": "npm install", "output": "added 214 packages, and audited 215 packages in 9s\n\nfound 0 vulnerabilities"}
        for log, cmd, ev in (([dev], "npm run dev", "VITE v5.4.2  ready in 312 ms"),
                             ([dev], "npm run dev", "\u279c  Local:   http://localhost:5173/"),
                             ([inst], "npm install", "added 214 packages, and audited 215 packages in 9s || found 0 vulnerabilities")):
            self.assertTrue(any("build/status line" in x for x in self.bad(log, cmd, ev)), ev)

    def test_an_echo_of_the_claim_does_not_prove_it(self):
        chained = {"tool": "Bash", "command": "npm run build && echo \"PASS: Download is the one primary action\"",
                   "output": "\u2713 built in 1.2s\nPASS: Download is the one primary action"}
        plain = {"tool": "Bash", "command": "echo 'first screen OK: one primary action, guidance shown'",
                 "output": "first screen OK: one primary action, guidance shown"}
        e1 = self.bad([chained], "npm run build", "PASS: Download is the one primary action")
        e2 = self.bad([plain], "echo 'first screen OK: one primary action, guidance shown'",
                      "first screen OK: one primary action, guidance shown")
        for e in (e1, e2):
            self.assertTrue(any("echoed itself" in x for x in e), e)

    def test_a_literal_printed_by_a_program_after_its_own_assertion_still_counts(self):
        run = {"tool": "Bash", "command": "node -e \"const n = count(); assert.equal(n, 1); console.log('one primary action: ok')\"",
               "output": "one primary action: ok"}
        self.assertEqual(self.bad([run], run["command"], "one primary action: ok"), [])

    def test_a_grep_never_proves_a_screen_criterion_even_when_it_says_contains(self):
        grep = {"tool": "Bash", "command": "grep -n 'className=\"primary\"' src/App.tsx",
                "output": "12:  <button className=\"primary\" onClick={download}>"}
        e = self.bad([grep], grep["command"], "<button className=\"primary\" onClick={download}>",
                     {"W1.2": "The first screen contains exactly one primary button"})
        self.assertTrue(any("text search" in x for x in e), e)

    def test_an_aggregate_count_does_not_prove_it(self):
        log = [{"tool": "Bash", "command": "npx vitest run", "output": "Tests  23 passed (23)"}]
        self.assertTrue(self.bad(log, "npx vitest run", "Tests  23 passed (23)"))

    def test_the_measured_first_use_output_is_accepted(self):
        ev = "PASS  FU1 one clear first action - 1 primary button(s) (\"\u05d4\u05d5\u05e8\u05d3\u05d4\")"
        self.assertEqual(self.bad([BUILD, EVAL], "node qa/first_use.mjs", ev), [])


class FirstUseLoopTests(TmpHome):
    def test_a_build_line_claim_gets_one_redo_and_the_measured_claim_passes(self):
        """End to end with FakeBackend: the Tester first backs the first-use criterion with real but unrelated output
        (the build). The loop rejects it, asks once with the reason, and accepts the measured result."""
        import json
        from agentsys import loop, runner
        from tests.helpers import DEV, REQ, plan, report as rep, _item
        req = dict(REQ, acceptance_criteria=[FIRST_USE["W1.2"]])
        unit = {"tool": "Bash", "command": "python -m unittest -v", "output": "all ... ok", "is_error": False}
        log = {"tester": [dict(BUILD, is_error=False), dict(EVAL, is_error=False), unit]}

        def build_claim(prompt):
            _, ids, _ = _item(prompt)
            return rep("pass", [_check("first action", "pass", ids, command="npm run build", evidence="\u2713 built in 1.24s")])

        def measured(prompt):
            p = json.loads(prompt)
            assert "build/status line" in p["rejection"]["errors"], p.get("rejection")
            _, ids, _ = _item(prompt)
            return rep("pass", [_check("first action", "pass", ids, command="node qa/first_use.mjs",
                                       evidence=EVAL["output"].splitlines()[0])])

        def final(prompt):
            _, ids, _ = _item(prompt)
            return rep("pass", [_check("all", "pass", ids)])
        pl = plan(1); pl["work_items"][0]["acceptance_criteria"] = [FIRST_USE["W1.2"]]
        fb = runner.FakeBackend({"architect": pl, "developer": DEV, "tester": [build_claim, measured, final]}, tool_logs=log)
        r = loop.run_task("t1", req, backend=fb, env={}, settings_paths=[])
        self.assertEqual(r["status"], "ready_for_review", r["message"])
        self.assertEqual([c["role"] for c in fb.calls].count("tester"), 3)     # item (rejected + redo) + final
