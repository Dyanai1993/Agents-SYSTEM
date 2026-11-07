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
