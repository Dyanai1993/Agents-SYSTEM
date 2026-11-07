"""Found in live run T-0010: the 45-minute timeout counted the time a task waited for the human, and there was no way to re-queue it."""
import json, time, unittest
from agentsys import approvals, intake, ledger, loop, orchestrator, runner, scheduler
from tests.base import TmpHome
from tests.helpers import DEV, REQ, NEEDS, plan, t_pass


def fb(**kw):
    base = {"architect": plan(1), "developer": DEV, "tester": t_pass, "orchestrator": REQ}
    base.update(kw)
    return runner.FakeBackend(base)


def proc(tid, backend):
    return orchestrator.process(tid, backend=backend, env={}, settings_paths=[])


def age_the_task(home, tid, hours):
    """Make the task look like it started `hours` ago (it waited for a human)."""
    p = home / "tasks" / tid / "budget.json"
    d = json.loads(p.read_text())
    old = time.time() - hours * 3600
    d["started_epoch"] = old
    d["run_started_epoch"] = old
    p.write_text(json.dumps(d))


class TimeoutClockTests(TmpHome):
    def test_waiting_for_a_human_does_not_count(self):
        L = ledger.Ledger("t1")
        L.data["run_started_epoch"] = L.data["started_epoch"] = time.time() - 10 * 3600
        L.save()
        with self.assertRaises(ledger.BudgetExceeded):
            ledger.Ledger("t1").check_can_call()                      # the old clock would block here
        L2 = ledger.Ledger("t1"); L2.begin_run()                      # a new run restarts its own clock
        L2.check_can_call()

    def test_a_single_run_is_still_limited(self):
        L = ledger.Ledger("t1", caps={"timeout_s": 60})
        L.data["run_started_epoch"] = time.time() - 120
        with self.assertRaises(ledger.BudgetExceeded) as cm:
            L.check_can_call()
        self.assertIn("this run", cm.exception.reason)

    def test_active_time_is_recorded(self):
        L = ledger.Ledger("t1")
        L.record_call("developer", "sonnet", 100, 12.5); L.record_call("tester", "sonnet", 100, 7.5)
        self.assertEqual(ledger.Ledger("t1").data["active_s"], 20.0)

    def test_old_budget_files_without_the_new_field_still_work(self):
        L = ledger.Ledger("t1")
        del L.data["run_started_epoch"]; L.save()
        ledger.Ledger("t1").check_can_call()
        l2 = ledger.Ledger("t1"); l2.begin_run()
        self.assertIn("run_started_epoch", ledger.Ledger("t1").data)

    def test_a_task_that_waited_overnight_resumes_after_retry(self):
        """The T-0010 story end to end: stopped for a question, hours later answered, must not die on the old clock."""
        orchestrator.submit("t1", request_text="make a counter")
        self.assertEqual(proc("t1", fb())["status"], "waiting_approval")    # the requirement draft waits for the human
        approvals.decide(approvals.latest("t1", "requirements")[0]["id"], "approved")
        e = proc("t1", fb(architect=NEEDS))
        self.assertEqual(e["status"], "needs_input")
        age_the_task(self.home, "t1", 10)
        orchestrator.retry("t1", answer="Use SQLite.")
        e = proc("t1", fb())
        self.assertEqual(e["status"], "waiting_approval")             # not "blocked: task timeout"


class RetryTests(TmpHome):
    def test_retry_is_refused_not_racing_while_process_holds_the_lock(self):
        """Review (fourth round): retry() used to read/write the queue entry with no lock at all. A retry's
        freshly-saved answer could be silently overwritten moments later by an in-flight process() call's
        own finish(), which holds a stale in-memory copy of the entry read BEFORE the retry and writes it
        back blindly on completion - discarding the answer with no error to anyone. Locking retry() the
        same way process() is locked means a concurrent retry is refused outright instead of racing."""
        orchestrator.submit("t1", requirements=dict(REQ, unknowns=[{"question": "Which cloud?", "blocking": True}]))
        proc("t1", fb())
        self.assertEqual(orchestrator.load_entry("t1")["status"], "needs_input")
        with orchestrator._Lock("t1"):                        # simulates an in-flight process() call
            with self.assertRaises(orchestrator.TaskBusy):
                orchestrator.retry("t1", answer="AWS, region eu-west-1")
        self.assertEqual(orchestrator.load_entry("t1")["status"], "needs_input")   # untouched, not half-applied
        e = orchestrator.retry("t1", answer="AWS, region eu-west-1")              # released: now it works
        self.assertEqual(e["requirements"]["constraints"][-1], "Clarification from the user: AWS, region eu-west-1")

    def test_retry_requeues_and_resumes_from_the_checkpoint(self):
        orchestrator.submit("t1", requirements=REQ)
        f = fb(developer=runner.UsageLimitReached("limit"))
        self.assertEqual(proc("t1", f)["status"], "paused")
        e = orchestrator.retry("t1")
        self.assertEqual(e["status"], "queued")
        f2 = fb()
        self.assertEqual(proc("t1", f2)["status"], "waiting_approval")
        self.assertNotIn("architect", [c["role"] for c in f2.calls])  # resumed: the plan was not made again

    def test_retry_with_an_answer_adds_a_constraint_and_clears_blocking_unknowns(self):
        req = dict(REQ, unknowns=[{"question": "Which cloud?", "blocking": True}])
        orchestrator.submit("t1", requirements=req)
        f = fb()
        self.assertEqual(proc("t1", f)["status"], "needs_input"); self.assertEqual(f.calls, [])
        e = orchestrator.retry("t1", answer="  AWS,   region eu-west-1  ")
        self.assertEqual(e["requirements"]["constraints"][-1], "Clarification from the user: AWS, region eu-west-1")
        self.assertFalse(any(u["blocking"] for u in e["requirements"]["unknowns"]))
        self.assertEqual(proc("t1", fb())["status"], "waiting_approval")

    def test_retry_without_an_answer_stays_blocked_if_nothing_changed(self):
        orchestrator.submit("t1", requirements=dict(REQ, unknowns=[{"question": "Which cloud?", "blocking": True}]))
        proc("t1", fb())
        orchestrator.retry("t1")
        f = fb()
        self.assertEqual(proc("t1", f)["status"], "needs_input"); self.assertEqual(f.calls, [])     # asked again, for free

    def test_retry_is_refused_for_finished_or_waiting_tasks(self):
        orchestrator.submit("t1", requirements=REQ)
        proc("t1", fb())                                              # waiting_approval
        with self.assertRaises(ValueError) as cm:
            orchestrator.retry("t1")
        self.assertIn("decide the pending approval", str(cm.exception))
        approvals.decide(approvals.pending()[0]["id"], "rejected")
        proc("t1", fb())
        with self.assertRaises(ValueError):
            orchestrator.retry("t1")                                  # rejected is final
        with self.assertRaises(KeyError):
            orchestrator.retry("nope")

    def test_retry_log_and_scheduler_pick_up(self):
        orchestrator.submit("t1", requirements=REQ)
        proc("t1", fb(architect={"bad": 1}))
        self.assertEqual(orchestrator.load_entry("t1")["status"], "blocked")
        self.assertIsNone(scheduler.pick())                           # blocked tasks wait for a human
        orchestrator.retry("t1")
        self.assertEqual(scheduler.pick()["task_id"], "t1")           # after the human says retry, the scheduler runs it

    def test_retry_with_an_answer_after_approval_requires_a_fresh_approval(self):
        """Review: a delivery failure unrelated to the requirement (bad target_repo) left the task 'blocked'
        with an existing 'approved' decision. Answering and retrying must not silently reuse that approval
        for content it never saw - it must re-verify and ask again."""
        def dev(prompt):
            (self.home / "tasks/t1/workspace/app.py").write_text("print('hi')\n")
            return DEV
        import shutil, subprocess
        repo = self.home / "target"; repo.mkdir()
        for cmd in (["init", "-q"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", *cmd], cwd=repo, capture_output=True)
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=dev)
        self.assertEqual(proc("t1", f)["status"], "waiting_approval")
        first_approval = approvals.pending()[0]["id"]
        approvals.decide(first_approval, "approved")
        shutil.rmtree(repo, ignore_errors=True)             # the repository is gone by delivery time
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked")     # Windows may leave the emptied folder behind: either reason is right
        self.assertRegex(e["note"], "delivery failed: .*(does not exist|not a git repository)")
        orchestrator.retry("t1", answer="use argparse")
        f2 = fb()
        e2 = proc("t1", f2)
        self.assertEqual(e2["status"], "waiting_approval")           # not reusing the old approval
        second_approval = e2["note"].split()[-1]
        self.assertNotEqual(second_approval, first_approval)
        self.assertIn("tester", [c["role"] for c in f2.calls])       # the final regression actually ran again

    def test_a_vague_answer_is_still_validated(self):
        orchestrator.submit("t1", request_text="x")
        proc("t1", fb(orchestrator={"nope": 1}))                      # intake failed: no draft exists
        with self.assertRaises(ValueError):
            orchestrator.retry("t1", answer="anything")

if __name__ == "__main__":
    unittest.main()
