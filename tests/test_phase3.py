import json, subprocess, time, unittest
from agentsys import actions, approvals, control, dashboard_data as dd, ledger, loop, orchestrator, runner, scheduler
from tests.base import TmpHome
from tests.helpers import DEV, REQ, plan, t_pass, t_fail


def fb(**kw):
    base = {"architect": plan(1), "developer": DEV, "tester": t_pass, "orchestrator": REQ}
    base.update(kw)
    return runner.FakeBackend(base)


def proc(tid, backend):
    return orchestrator.process(tid, backend=backend, env={}, settings_paths=[])


def git(path, *a):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=path, capture_output=True, text=True)


class ApprovalTests(TmpHome):
    def test_request_is_idempotent_and_decision_is_a_file(self):
        a = approvals.request("t1", "merge", "ctx")
        self.assertEqual(approvals.request("t1", "merge", "other"), a)
        self.assertEqual(approvals.status(a["id"]), "pending")
        approvals.decide(a["id"], "approved", "ok")
        self.assertEqual(approvals.status(a["id"]), "approved")
        self.assertTrue((self.home / "approvals" / "decided" / f"{a['id']}.json").exists())
        with self.assertRaises(approvals.ApprovalError):
            approvals.decide(a["id"], "rejected")               # decisions are final

    def test_rejected_request_needs_explicit_new_request(self):
        a = approvals.request("t1", "merge", "c")
        approvals.decide(a["id"], "rejected")
        self.assertEqual(approvals.request("t1", "merge", "c")["id"], a["id"])
        self.assertNotEqual(approvals.request("t1", "merge", "c", new=True)["id"], a["id"])

    def test_bad_ids_and_unknown_requests(self):
        for bad in ("../x", "", "a/b"):
            with self.assertRaises(approvals.ApprovalError):
                approvals.decide(bad, "approved")
        with self.assertRaises(approvals.ApprovalError):
            approvals.decide("nope-1", "approved")

    def test_opus_needs_a_human_decision(self):
        f = runner.FakeBackend({"architect": plan(1)})
        with self.assertRaises(approvals.OpusNotApproved):
            runner.run_agent("architect", {}, model="opus", task_id="t1", backend=f, env={}, settings_paths=[])
        self.assertEqual(f.calls, [])
        pend = approvals.pending()
        self.assertEqual([p["gate"] for p in pend], ["opus"])
        approvals.decide(pend[0]["id"], "approved")
        self.assertTrue(runner.run_agent("architect", {}, model="opus", task_id="t1", backend=f, env={}, settings_paths=[]).ok)


class ControlTests(TmpHome):
    def test_pause_stop_resume(self):
        control.set_control("t1", "pause")
        f = fb()
        r = loop.run_task("t1", REQ, backend=f, env={}, settings_paths=[])
        self.assertEqual(r["status"], "paused"); self.assertEqual(f.calls, [])     # no agent call while paused
        control.set_control("t1", "resume")
        self.assertEqual(loop.run_task("t1", REQ, backend=f, env={}, settings_paths=[])["status"], "ready_for_review")
        control.set_control("t2", "stop")
        f2 = fb()
        self.assertEqual(loop.run_task("t2", REQ, backend=f2, env={}, settings_paths=[])["status"], "stopped")
        self.assertEqual(f2.calls, [])

    def test_pause_between_steps(self):
        calls = {"n": 0}
        def dev(prompt):
            calls["n"] += 1
            control.set_control("t1", "pause")          # the user presses pause while the developer works
            return DEV
        r = loop.run_task("t1", REQ, backend=fb(developer=dev), env={}, settings_paths=[])
        self.assertEqual(r["status"], "paused")
        prog = json.loads((self.home / "tasks/t1/progress.json").read_text())
        self.assertEqual(prog["step"], "test")            # developer step is saved; the tester has not run


class OrchestratorTests(TmpHome):
    def ws(self, tid):
        return self.home / "tasks" / tid / "workspace"

    def dev_writes(self, tid):
        def dev(prompt):
            (self.ws(tid) / "app.py").write_text("print('hi')\n"); return DEV
        return dev

    def test_full_flow_with_merge_gate_and_patches(self):
        orchestrator.submit("t1", requirements=REQ)
        f = fb(developer=self.dev_writes("t1"))
        e = proc("t1", f)
        self.assertEqual(e["status"], "waiting_approval")
        pend = approvals.pending()
        self.assertEqual(pend[0]["gate"], "merge"); self.assertIn("app.py", pend[0]["context"])
        n = len(f.calls)
        self.assertEqual(proc("t1", f)["status"], "waiting_approval")
        self.assertEqual(len(f.calls), n)                 # waiting costs nothing
        approvals.decide(pend[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "done"); self.assertEqual(len(f.calls), n)
        self.assertTrue(list((self.home / "deliveries/t1").glob("*.patch")))
        self.assertEqual(json.loads((self.home / "tasks/t1/state.json").read_text())["state"], "done")

    def test_approved_merge_creates_branch_in_target_and_never_touches_main(self):
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        main_before = git(repo, "rev-parse", "main").stdout
        # the target repo must share the workspace root commit for `git am`: patches apply on any tree, so this works
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "done"); self.assertEqual(e["delivery"]["branch"], "agentsys/t1")
        self.assertEqual(git(repo, "rev-parse", "main").stdout, main_before)
        self.assertIn("agentsys/t1", git(repo, "branch").stdout)
        self.assertEqual(git(repo, "show", "agentsys/t1:app.py").stdout, "print('hi')\n")

    def test_dirty_target_repo_is_not_touched(self):
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "i")
        (repo / "README").write_text("dirty")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f); approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("uncommitted", e["note"])
        self.assertEqual((repo / "README").read_text(), "dirty")

    def test_missing_target_repo_blocks_cleanly_instead_of_crashing(self):
        missing = self.home / "does-not-exist"
        orchestrator.submit("t1", requirements=REQ, target_repo=missing)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f); approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("does not exist", e["note"])

    def test_target_path_that_is_not_a_git_repo_blocks_cleanly(self):
        not_git = self.home / "plain-folder"; not_git.mkdir()
        orchestrator.submit("t1", requirements=REQ, target_repo=not_git)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f); approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("not a git repository", e["note"])

    def test_deliver_is_idempotent_after_a_crash_between_delivery_and_done(self):
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        d1 = orchestrator.deliver("t1", repo)
        self.assertEqual(d1["branch"], "agentsys/t1")
        # as if _process crashed right after deliver() returned, before recording "done": deliver() runs again
        d2 = orchestrator.deliver("t1", repo)
        self.assertEqual(d2["branch"], "agentsys/t1")
        self.assertEqual(git(repo, "branch").stdout.count("agentsys/t1"), 1)

    def test_failed_patch_apply_restores_the_original_branch_and_deletes_the_broken_one(self):
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "app.py").write_text("conflict\n"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("do not apply cleanly", e["note"])
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")
        self.assertNotIn("agentsys/t1", git(repo, "branch").stdout)
        # fix the conflict in the target repo and retry: the stale broken branch must not block this
        (repo / "app.py").unlink(); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "remove conflict")
        orchestrator.retry("t1")
        e2 = proc("t1", f)
        self.assertEqual(e2["status"], "done")
        self.assertIn("agentsys/t1", git(repo, "branch").stdout)

    def test_rejected_merge(self):
        orchestrator.submit("t1", requirements=REQ)
        f = fb(); proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "rejected", "not now")
        self.assertEqual(proc("t1", f)["status"], "rejected")
        self.assertFalse((self.home / "deliveries/t1").exists())

    def test_intake_from_plain_request(self):
        orchestrator.submit("t1", request_text="make a word counter")
        f = fb()
        proc("t1", f)
        self.assertEqual(f.calls[0]["role"], "orchestrator")
        self.assertEqual(f.calls[0]["model"], "haiku")
        self.assertEqual(json.loads((self.home / "tasks/t1/requirements.json").read_text())["goal"], "word counter")

    def test_intake_blocking_unknown_stops_before_architect(self):
        req = dict(REQ, unknowns=[{"question": "Which cloud?", "blocking": True}])
        orchestrator.submit("t1", request_text="deploy it")
        f = fb(orchestrator=req)
        e = proc("t1", f)
        self.assertEqual(e["status"], "needs_input"); self.assertIn("Which cloud?", e["note"])
        self.assertEqual([c["role"] for c in f.calls], ["orchestrator"])

    def test_intake_bad_output_blocks(self):
        orchestrator.submit("t1", request_text="x")
        e = proc("t1", fb(orchestrator={"nope": 1}))
        self.assertEqual(e["status"], "blocked"); self.assertIn("intake failed", e["note"])

    def test_submit_validation(self):
        with self.assertRaises(ValueError): orchestrator.submit("t1")
        with self.assertRaises(ValueError): orchestrator.submit("t1", request_text="a", requirements=REQ)
        orchestrator.submit("t1", requirements=REQ)
        with self.assertRaises(ValueError): orchestrator.submit("t1", requirements=REQ)


class SchedulerTests(TmpHome):
    def test_priority_order_and_one_task_per_call(self):
        orchestrator.submit("low", requirements=REQ, priority=9)
        orchestrator.submit("high", requirements=REQ, priority=1)
        f = fb()
        e = scheduler.run_next(backend=f, env={}, settings_paths=[])
        self.assertEqual(e["task_id"], "high")
        self.assertEqual(orchestrator.load_entry("low")["status"], "queued")      # sequential: only one advanced

    def test_waiting_approval_is_not_picked_until_decided(self):
        orchestrator.submit("t1", requirements=REQ)
        def dev(prompt):
            (self.home / "tasks/t1/workspace/a.py").write_text("x"); return DEV
        f = fb(developer=dev)
        scheduler.run_next(backend=f, env={}, settings_paths=[])
        self.assertIsNone(scheduler.run_next(backend=f, env={}, settings_paths=[]))
        approvals.decide(approvals.pending()[0]["id"], "approved")
        self.assertEqual(scheduler.run_next(backend=f, env={}, settings_paths=[])["status"], "done")

    def test_new_task_refused_when_window_nearly_used(self):
        ledger.Window().add("other", "developer", "sonnet", int(0.9 * 3_000_000))
        orchestrator.submit("t1", requirements=REQ)
        f = fb()
        out = scheduler.run_next(backend=f, env={}, settings_paths=[])
        self.assertIn("skipped", out); self.assertEqual(f.calls, [])
        self.assertIn("not started", orchestrator.load_entry("t1")["note"])

    def test_usage_limit_pauses_and_waits_for_reset(self):
        orchestrator.submit("t1", requirements=REQ)
        f = fb(developer=runner.UsageLimitReached("5-hour limit reached"))
        e = scheduler.run_next(backend=f, env={}, settings_paths=[])
        self.assertEqual(e["status"], "paused"); self.assertGreater(e["resume_after"], time.time() + 4 * 3600)
        self.assertIsNone(scheduler.pick())                                         # too early
        self.assertEqual(scheduler.pick(now=time.time() + 6 * 3600)["task_id"], "t1")
        control.set_control("t1", "pause")
        self.assertIsNone(scheduler.pick(now=time.time() + 6 * 3600))               # the user's pause still wins
        control.set_control("t1", "resume")
        e = scheduler.run_next(backend=fb(), env={}, settings_paths=[], now=time.time() + 6 * 3600)
        self.assertEqual(e["status"], "waiting_approval")

    def test_blocked_tasks_wait_for_a_human(self):
        orchestrator.submit("t1", requirements=REQ)
        scheduler.run_next(backend=fb(architect={"bad": 1}), env={}, settings_paths=[])
        self.assertEqual(orchestrator.load_entry("t1")["status"], "blocked")
        self.assertIsNone(scheduler.pick())


class DashboardActionTests(TmpHome):
    H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "X-Agentsys": "1"}

    def test_post_requires_custom_header_json_and_local_origin(self):
        a = approvals.request("t1", "merge", "c")
        body = {"id": a["id"], "decision": "approved"}
        bad = [dict(self.H, **{"X-Agentsys": "0"}), {k: v for k, v in self.H.items() if k != "X-Agentsys"},
               dict(self.H, **{"Content-Type": "text/plain"}), dict(self.H, Origin="https://evil.example"),
               dict(self.H, Host="evil.example")]
        for h in bad:
            self.assertEqual(actions.handle_post("/api/approval/decide", body, h)[0], 403, h)
        self.assertEqual(approvals.status(a["id"]), "pending")                      # nothing was written
        code, _ = actions.handle_post("/api/approval/decide", body, dict(self.H, Origin="http://localhost:8765"))
        self.assertEqual(code, 200); self.assertEqual(approvals.status(a["id"]), "approved")

    def test_post_validation(self):
        self.assertEqual(actions.handle_post("/api/approval/decide", {"id": "x", "decision": "maybe"}, self.H)[0], 400)
        self.assertEqual(actions.handle_post("/api/approval/decide", {"id": "missing-1", "decision": "approved"}, self.H)[0], 400)
        self.assertEqual(actions.handle_post("/api/control", {"task_id": "../x", "action": "stop"}, self.H)[0], 400)
        self.assertEqual(actions.handle_post("/api/control", {"task_id": "t1", "action": "format"}, self.H)[0], 400)
        self.assertEqual(actions.handle_post("/api/control", {"task_id": "t1", "action": "pause"}, self.H)[0], 200)
        self.assertEqual(control.read("t1"), "pause")
        self.assertEqual(actions.handle_post("/api/nothing", {}, self.H)[0], 404)

    def test_views(self):
        orchestrator.submit("t1", requirements=REQ)
        proc("t1", fb())
        av = dd.approvals_view()
        self.assertEqual(av["pending"][0]["task_id"], "t1")
        uv = dd.usage_view()
        self.assertGreater(uv["by_model_7d"]["sonnet"], 0); self.assertEqual(uv["tasks"][0]["task_id"], "t1")
        texts = [i["text"] for i in dd.alerts_view()["items"]]
        self.assertTrue(any("pending approval: merge" in t for t in texts))
        orchestrator.submit("t2", requirements=REQ)
        proc("t2", fb(architect={"bad": 1}))
        self.assertTrue(any(i["task_id"] == "t2" and i["level"] == "bad" for i in dd.alerts_view()["items"]))


class LockTests(TmpHome):
    def test_second_run_on_the_same_task_is_refused(self):
        orchestrator.submit("t1", requirements=REQ)
        with orchestrator._Lock("t1"):
            f = fb()
            with self.assertRaises(orchestrator.TaskBusy):
                proc("t1", f)
            out = scheduler.run_next(backend=f, env={}, settings_paths=[])
            self.assertIn("another run", out["skipped"]); self.assertEqual(f.calls, [])
        self.assertFalse((self.home / "queue/t1.lock").exists())             # released

    def test_lock_is_released_after_an_error_and_stale_locks_are_taken_over(self):
        import os
        orchestrator.submit("t1", requirements=REQ)
        with self.assertRaises(RuntimeError):
            with orchestrator._Lock("t1"):
                raise RuntimeError("boom")
        self.assertFalse((self.home / "queue/t1.lock").exists())
        lock = self.home / "queue/t1.lock"; lock.write_text("999999")
        old = time.time() - orchestrator.LOCK_STALE_S - 10
        os.utime(lock, (old, old))
        with orchestrator._Lock("t1"):
            pass

    def test_simultaneous_decisions_only_one_wins(self):
        a = approvals.request("t1", "merge", "c")
        approvals.decide(a["id"], "approved")
        with self.assertRaises(approvals.ApprovalError):
            approvals.decide(a["id"], "rejected")
        self.assertEqual(approvals.status(a["id"]), "approved")


class EmptyDeliveryTest(TmpHome):
    def test_task_that_changed_nothing_cannot_be_delivered(self):
        orchestrator.submit("t1", requirements=REQ)
        f = fb(); proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("nothing to deliver", e["note"])


if __name__ == "__main__":
    unittest.main()
