"""Phase A of the master prompt: one worker, a heartbeat, Start/Continue/Answer requests, immediate controls."""
import json, time
from agentsys import actions, approvals, assistant, control, dashboard_data as dd, orchestrator, runner, scheduler, worker
from agentsys.filelock import FileLock
from tests.base import TmpHome
from tests.helpers import DEV, NEEDS, REQ
from tests.test_phase3 import fb

H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "X-Agentsys": "1"}


class WorkerBase(TmpHome):
    def dev(self, tid):
        def write(prompt):
            (self.home / "tasks" / tid / "workspace" / "a.py").write_text("x = 1\n")
            return DEV
        return write

    def step(self, backend, hb=None):
        return worker.step(backend=backend, heartbeat=hb, env={}, settings_paths=[])


class HeartbeatTests(WorkerBase):
    def test_a_heartbeat_names_the_job_and_disappears_on_a_clean_exit(self):
        with worker.Heartbeat("worker") as hb:
            hb.set_job("task", "T-0001")
            d = json.loads(hb.path.read_text())
            self.assertEqual((d["kind"], d["job"]["id"], d["build"]), ("worker", "T-0001", worker.build_id()))
            s = worker.status()
            self.assertEqual(s["level"], "ok"); self.assertIn("running T-0001", s["text"])
        self.assertFalse(hb.path.exists())
        self.assertIn("offline", worker.status()["text"])

    def test_the_beat_continues_during_a_long_call(self):
        old = worker.BEAT_S
        worker.BEAT_S = 0.05
        try:
            with worker.Heartbeat("worker") as hb:
                first = json.loads(hb.path.read_text())["beat_at"]
                time.sleep(0.3)                          # the "agent call": nothing else writes the file
                self.assertGreater(json.loads(hb.path.read_text())["beat_at"], first)
        finally:
            worker.BEAT_S = old

    def test_a_crashed_worker_is_named_with_what_it_was_doing(self):
        hb = worker.Heartbeat("worker")
        hb.doc["job"] = {"kind": "task", "id": "T-0003", "since": time.time() - 600}
        hb.beat()                                        # written once, then the process "dies" (no __exit__)
        s = worker.status(now=time.time() + 300)
        self.assertEqual(s["level"], "bad"); self.assertIn("stopped while running T-0003", s["text"])

    def test_old_code_and_two_workers_are_reported(self):
        a = worker.Heartbeat("worker", build="0ld")
        with a:
            self.assertIn("runs code from before the last update", worker.status()["text"])
            with worker.Heartbeat("worker"):
                self.assertIn("2 workers are running", worker.status()["text"])
        self.assertIn("dashboard server runs old code", worker.status(server_build="0ld")["text"])

    def test_the_task_view_shows_the_agent_of_a_live_run(self):
        orchestrator.submit("T-0005", requirements=REQ)
        (self.home / "tasks/T-0005/state.json").write_text(json.dumps({"state": "build", "current_agent": "developer"}))
        (self.home / "tasks/T-0005/progress.json").write_text("{}")                 # a run already started it
        with worker.Heartbeat("worker") as hb:
            hb.set_job("task", "T-0005")
            self.assertIn("T-0005: developer", worker.status()["text"])
            self.assertIsNone(dd.task_detail("T-0005")["attention"])          # running: not "stuck", not "not running"
            self.assertTrue(dd.task_detail("T-0005")["run"]["running"])
        self.assertIn("not running", dd.task_detail("T-0005")["attention"]["text"])  # the moment it is gone


class SingleWorkerTests(WorkerBase):
    def test_only_one_worker_runs(self):
        with FileLock(self.home / "state" / "worker.lock"):
            with self.assertRaises(worker.AlreadyRunning):
                worker.run_forever(backend=runner.FakeBackend({}), should_stop=lambda: True)

    def test_a_killed_workers_heartbeat_is_forgotten_by_the_next_worker(self):
        """Seen live: the old worker was killed before removing its file, and for 20 s the page said '2 workers'."""
        dead = worker.Heartbeat("worker")
        dead.beat()                                          # fresh file, but its process is gone (no lock held)
        run = worker.Heartbeat("run-next")
        run.beat()
        seen = []
        worker.run_forever(backend=runner.FakeBackend({}), should_stop=lambda: seen.append(worker.status()) or True)
        self.assertFalse(dead.path.exists()); self.assertTrue(run.path.exists())      # run-next is not a worker
        self.assertNotIn("2 workers", seen[0]["text"])

    def test_run_forever_stops_and_cleans_up(self):
        worker.run_forever(backend=runner.FakeBackend({}), should_stop=lambda: True)
        self.assertEqual(list((self.home / "state" / "workers").glob("*.json")), [])


class RunRequestTests(WorkerBase):
    def test_the_worker_never_starts_a_task_nobody_asked_for(self):
        orchestrator.submit("T-0001", requirements=REQ)
        self.assertIsNone(self.step(fb(developer=self.dev("T-0001"))))
        self.assertEqual(orchestrator.load_entry("T-0001")["status"], "queued")

    def test_start_runs_it_until_it_needs_you_then_continue_delivers(self):
        orchestrator.submit("T-0001", requirements=REQ)
        f = fb(developer=self.dev("T-0001"))
        code, body = actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "start"}, H)
        self.assertEqual(code, 200, body)
        self.assertEqual(self.step(f)["status"], "waiting_approval")
        self.assertIsNone(worker.get_request("T-0001"))                       # done with: its status says the rest
        code, body = actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "continue"}, H)
        self.assertEqual(code, 400); self.assertIn("decision in the Approvals tab", body["error"])
        approvals.decide(approvals.pending()[0]["id"], "approved")
        self.assertIn("press Continue to deliver", dd.task_detail("T-0001")["attention"]["text"])
        actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "continue"}, H)
        self.assertEqual(self.step(f)["status"], "done")

    def test_a_chat_message_goes_first_and_the_heartbeat_names_the_draft(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        sid = assistant.post_message(None, "a word counter")["id"]
        seen = []

        class HB:
            def set_job(self, kind, i): seen.append((kind, i))
            def clear_job(self): pass
        turn = {"reply": "ok", "draft": dict(REQ, unknowns=[]), "suggestions": [], "conflicts": []}
        out = self.step(fb(assistant=turn), HB())
        self.assertIn("assistant_turn", out); self.assertEqual(seen, [("draft", sid)])
        self.assertEqual(worker.get_request("T-0001")["status"], "waiting")    # next step runs the task

    def test_requests_that_need_you_first_are_refused_with_the_reason(self):
        orchestrator.submit("T-0001", requirements=REQ)
        orchestrator.apply_control("T-0001", "stop")
        with self.assertRaises(ValueError) as c:
            worker.request_run("T-0001")
        self.assertIn("press Resume first", str(c.exception))
        e = orchestrator.load_entry("T-0001"); e["status"] = "needs_input"; orchestrator.save_entry(e)
        self.assertEqual(actions.handle_post("/api/task/run", {"task_id": "T-0001"}, H)[0], 400)
        self.assertEqual(actions.handle_post("/api/task/run", {"task_id": "T-0404"}, H)[0], 404)

    def test_a_request_that_must_wait_stays_and_says_why(self):
        orchestrator.submit("T-0001", requirements=REQ)
        old = scheduler.ledger.Window.can_start_large_task
        scheduler.ledger.Window.can_start_large_task = lambda self, units: (False, "5-hour window nearly used")
        try:
            worker.request_run("T-0001")
            self.assertIsNone(self.step(fb()))
            r = worker.get_request("T-0001")
            self.assertEqual(r["status"], "waiting"); self.assertIn("5-hour window", r["note"])
            self.assertIn("waiting for the worker", dd.task_detail("T-0001")["attention"]["text"])
        finally:
            scheduler.ledger.Window.can_start_large_task = old

    def test_a_second_click_is_the_same_request(self):
        orchestrator.submit("T-0001", requirements=REQ)
        self.assertEqual(worker.request_run("T-0001"), worker.request_run("T-0001"))

    def test_a_task_that_became_unrunnable_is_refused_at_run_time(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        e = orchestrator.load_entry("T-0001"); e["status"] = "needs_input"; orchestrator.save_entry(e)
        self.assertIsNone(self.step(fb()))
        self.assertEqual(worker.get_request("T-0001")["status"], "refused")
        self.assertIn("request was refused", dd.task_detail("T-0001")["attention"]["text"])

    def test_a_task_locked_by_a_terminal_run_waits(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        with orchestrator._Lock("T-0001"):
            self.assertIsNone(self.step(fb()))
        self.assertEqual(worker.get_request("T-0001")["status"], "waiting")


class AnswerTests(WorkerBase):
    def test_answering_a_question_requeues_and_asks_the_worker_to_continue(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        self.assertEqual(self.step(fb(architect=NEEDS))["status"], "needs_input")
        self.assertIn("a question for you", dd.task_detail("T-0001")["attention"]["text"])
        code, body = actions.handle_post("/api/task/answer", {"task_id": "T-0001", "answer": "SQLite"}, H)
        self.assertEqual(code, 200, body)
        self.assertEqual(body["status"], "queued"); self.assertEqual(body["request"]["action"], "continue")
        self.assertIn("SQLite", json.dumps(orchestrator.load_entry("T-0001")["requirements"]))

    def test_an_answer_is_never_cut_silently(self):
        orchestrator.submit("T-0001", requirements=REQ)
        e = orchestrator.load_entry("T-0001"); e["status"] = "needs_input"; orchestrator.save_entry(e)
        code, body = actions.handle_post("/api/task/answer", {"task_id": "T-0001", "answer": "x" * 301}, H)
        self.assertEqual(code, 400); self.assertIn("301", body["error"])
        self.assertEqual(actions.handle_post("/api/task/answer", {"task_id": "T-0001", "answer": "  "}, H)[0], 400)


class ControlTests(WorkerBase):
    def test_stop_and_pause_apply_now_when_nothing_runs_and_stop_is_reversible(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        r = orchestrator.apply_control("T-0001", "stop")
        self.assertTrue(r["applied"]); self.assertEqual(orchestrator.load_entry("T-0001")["status"], "stopped")
        self.assertIsNone(worker.get_request("T-0001"))                       # a stop cancels the waiting Start
        self.assertIn("press Resume", dd.task_detail("T-0001")["attention"]["text"])
        self.assertEqual(orchestrator.apply_control("T-0001", "resume")["status"], "queued")
        self.assertEqual(orchestrator.apply_control("T-0001", "pause")["status"], "paused")
        self.assertEqual(orchestrator.apply_control("T-0001", "resume")["status"], "queued")
        self.assertIsNone(control.read("T-0001"))

    def test_pause_cancels_a_waiting_start_and_resume_clears_an_old_refusal(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        orchestrator.apply_control("T-0001", "pause")
        self.assertIsNone(worker.get_request("T-0001"))
        orchestrator.apply_control("T-0001", "resume")
        worker.request_run("T-0001")
        e = orchestrator.load_entry("T-0001"); e["status"] = "needs_input"; orchestrator.save_entry(e)
        self.step(fb())                                                       # refused: it has a question now
        self.assertEqual(worker.get_request("T-0001")["status"], "refused")
        e["status"] = "paused"; orchestrator.save_entry(e)
        orchestrator.apply_control("T-0001", "resume")
        self.assertIsNone(worker.get_request("T-0001"))                       # no stale "refused" after Resume

    def test_a_control_on_a_running_task_is_pending(self):
        orchestrator.submit("T-0001", requirements=REQ)
        with orchestrator._Lock("T-0001"):
            r = orchestrator.apply_control("T-0001", "stop")
        self.assertFalse(r["applied"]); self.assertIn("before its next agent call", r["note"])
        self.assertEqual(orchestrator.load_entry("T-0001")["status"], "queued")
        self.assertEqual(control.read("T-0001"), "stop")                       # the loop still sees it

    def test_a_stopped_task_resumes_from_its_checkpoint_and_finishes(self):
        orchestrator.submit("T-0001", requirements=REQ)
        f = fb(developer=self.dev("T-0001"))
        calls = []
        real = f.run

        def stop_after_plan(**kw):                        # the user presses Stop while the Architect works
            calls.append(kw["role"])
            if kw["role"] == "architect":
                control.set_control("T-0001", "stop")
            return real(**kw)
        f.run = stop_after_plan
        worker.request_run("T-0001")
        self.assertEqual(self.step(f)["status"], "stopped")
        orchestrator.apply_control("T-0001", "resume")
        worker.request_run("T-0001", "continue")
        self.assertEqual(self.step(f)["status"], "waiting_approval")
        self.assertEqual(calls.count("architect"), 1)                          # the plan was not made again


class RequirementEditTests(WorkerBase):
    """Seen live (T-0025): too big for one task; answers only ADD constraints, so the Architect asked round after round.
    The human narrows the requirement itself: a new revision, planned from scratch."""
    BIG = dict(REQ, acceptance_criteria=[f"criterion {i}" for i in range(1, 27)])

    def test_narrowing_a_stuck_task_makes_a_new_revision_the_architect_plans(self):
        orchestrator.submit("T-0001", requirements=self.BIG)
        worker.request_run("T-0001")
        self.assertEqual(self.step(fb(architect=NEEDS))["status"], "needs_input")
        self.assertTrue(dd.task_detail("T-0001")["run"]["can_edit_requirement"])
        small = dict(REQ, acceptance_criteria=["criterion 1", "criterion 3"])
        code, body = actions.handle_post("/api/task/requirement", {"task_id": "T-0001", "requirement": small}, H)
        self.assertEqual(code, 200, body); self.assertEqual(body["revision"], 2)
        e = orchestrator.load_entry("T-0001")
        self.assertEqual(e["status"], "queued"); self.assertEqual(e["requirements"]["acceptance_criteria"], ["criterion 1", "criterion 3"])
        hist = json.loads((self.home / "tasks/T-0001/requirement_history.json").read_text(encoding="utf-8"))
        self.assertEqual(len(hist[0]["requirement"]["acceptance_criteria"]), 26)             # the old revision is kept
        seen = []

        def arch(prompt):
            seen.append(json.loads(prompt)["requirements"]["acceptance_criteria"])
            from tests.helpers import plan
            return plan(1, covers=("AC1", "AC2"))
        worker.request_run("T-0001", "continue")
        self.assertEqual(self.step(fb(architect=arch, developer=self.dev("T-0001")))["status"], "waiting_approval")
        self.assertEqual([c["text"] for c in seen[0]], ["criterion 1", "criterion 3"])         # planned from revision 2

    def test_a_blocked_task_takes_a_note_with_retry_and_a_coverage_failure_says_narrow_it(self):
        orchestrator.submit("T-0001", requirements=self.BIG)
        e = orchestrator.load_entry("T-0001")
        e.update(status="blocked", note="Step failed: plan rejected: requirement criterion AC2 is not covered by any work item")
        orchestrator.save_entry(e)
        (self.home / "tasks/T-0001").mkdir(parents=True, exist_ok=True)
        (self.home / "tasks/T-0001/state.json").write_text(json.dumps({"state": "blocked"}))
        self.assertIn("a Retry fails the same way", dd.task_detail("T-0001")["attention"]["text"])
        code, body = actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "retry", "answer": "use SQLite"}, H)
        self.assertEqual(code, 200, body)
        self.assertIn("use SQLite", json.dumps(orchestrator.load_entry("T-0001")["requirements"]))
        e = orchestrator.load_entry("T-0001"); e["status"] = "blocked"; orchestrator.save_entry(e)
        worker.cancel_run("T-0001")
        self.assertEqual(actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "retry", "answer": "x" * 301}, H)[0], 400)

    def test_no_edit_once_something_was_built(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        self.step(fb(developer=self.dev("T-0001")))                                            # built and waiting for review
        e = orchestrator.load_entry("T-0001"); e["status"] = "blocked"; orchestrator.save_entry(e)
        self.assertFalse(dd.task_detail("T-0001")["run"]["can_edit_requirement"])
        code, body = actions.handle_post("/api/task/requirement", {"task_id": "T-0001", "requirement": REQ}, H)
        self.assertEqual(code, 400); self.assertIn("start a new task", body["error"])

    def test_bad_input_is_refused(self):
        orchestrator.submit("T-0001", requirements=REQ)
        bad = dict(REQ, acceptance_criteria=[])
        self.assertEqual(actions.handle_post("/api/task/requirement", {"task_id": "T-0001", "requirement": bad}, H)[0], 400)
        self.assertEqual(actions.handle_post("/api/task/requirement", {"task_id": "T-0404", "requirement": REQ}, H)[0], 404)
        with orchestrator._Lock("T-0001"):
            self.assertEqual(actions.handle_post("/api/task/requirement", {"task_id": "T-0001", "requirement": REQ}, H)[0], 409)


class LiveViewTests(WorkerBase):
    def test_a_run_leaves_live_lines_the_task_monitor_shows(self):
        orchestrator.submit("T-0001", requirements=REQ)
        worker.request_run("T-0001")
        self.step(fb(developer=self.dev("T-0001")))
        live = dd.task_detail("T-0001")["live"]
        kinds = [(x["agent"], x["kind"]) for x in live]
        self.assertIn(("architect", "start"), kinds); self.assertIn(("developer", "end"), kinds)
        self.assertIn(("tester", "tool"), kinds)                       # the tester's command, as it ran
        self.assertTrue(all("ts" in x for x in live))

    def test_live_lines_never_carry_a_secret_or_raw_output(self):
        from agentsys import runner
        got = []
        runner._report_progress(got.append, {"type": "tool_use", "name": "Bash", "input": {"command": "curl -H 'api_key: abc123secret' x"}})
        runner._report_progress(got.append, {"type": "tool_result", "content": "line one sk-ABCDEFGH12345678\nline two", "is_error": False})
        runner._report_progress(lambda e: 1 / 0, {"type": "text", "text": "a broken callback never breaks the run"})
        self.assertNotIn("abc123secret", json.dumps(got)); self.assertNotIn("sk-ABCDEFGH12345678", json.dumps(got))
        self.assertEqual(got[1]["text"], "line one [REDACTED]")                 # first line only

    def test_the_live_file_stays_small(self):
        from agentsys import logger
        old = logger.LIVE_MAX_BYTES
        logger.LIVE_MAX_BYTES = 2000
        try:
            for i in range(200):
                logger.live("T-0009", "developer", {"kind": "say", "text": f"line {i} " + "x" * 40})
            self.assertLess((self.home / "tasks/T-0009/live.jsonl").stat().st_size, 4000)
            self.assertIn("line 199", logger.live_tail("T-0009")[-1]["text"])
        finally:
            logger.LIVE_MAX_BYTES = old


class BlockerConsistencyTests(WorkerBase):
    def test_run_blocker_agrees_with_the_scheduler(self):
        orchestrator.submit("T-0001", requirements=REQ)
        for st in ("queued", "done", "rejected", "stopped", "paused", "needs_input", "blocked", "needs_approval", "waiting_approval"):
            e = orchestrator.load_entry("T-0001"); e["status"] = st; orchestrator.save_entry(e)
            self.assertEqual(scheduler.run_blocker(e)[0] is None, scheduler._runnable(e, time.time()), st)
