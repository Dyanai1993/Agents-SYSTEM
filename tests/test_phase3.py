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

    def test_ordering_survives_past_nine_requests(self):
        """Review (third round, missing-tests note): a plain string sort of filenames puts '...-10.json'
        before '...-9.json' ('1' < '9'), so after a 10th request for the same task+gate, latest() picked
        the stale 9th (rejected) request instead of the actual latest (pending) one."""
        for i in range(1, 10):
            approvals.decide(approvals.request("t1", "merge", f"ctx{i}", diff_ref=f"v{i}", new=(i > 1))["id"], "rejected")
        tenth = approvals.request("t1", "merge", "ctx10", diff_ref="v10", new=True)
        self.assertEqual(tenth["id"], "t1-merge-10")
        doc, st = approvals.latest("t1", "merge")
        self.assertEqual(doc["id"], "t1-merge-10"); self.assertEqual(st, "pending")

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

    def test_a_foreign_branch_of_the_same_name_is_never_deleted(self):
        """Review (third round): deliver() used to delete ANY existing branch of that name, with no proof
        the system created it - including the user's own unrelated branch that happens to collide."""
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        git(repo, "checkout", "-q", "-b", "agentsys/t1")
        (repo / "mine.py").write_text("print('not agentsys')\n"); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "my own work")
        git(repo, "checkout", "-q", "main")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        e = proc("t1", f)
        self.assertEqual(e["status"], "blocked"); self.assertIn("not created by this task's delivery", e["note"])
        self.assertIn("agentsys/t1", self.branches_of(repo))
        self.assertEqual(git(repo, "show", "agentsys/t1:mine.py").stdout, "print('not agentsys')\n")

    def branches_of(self, repo):
        return git(repo, "branch").stdout

    def test_redelivering_after_new_commits_actually_redelivers(self):
        """Review (third round): the idempotency shortcut only checked target_repo+branch+'delivered', not
        the content version - a retry that added new commits after a successful delivery would report
        success again without ever running `git am` on the new patches, leaving the target branch stale."""
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        d1 = orchestrator.deliver("t1", repo)
        self.assertEqual(git(repo, "show", "agentsys/t1:app.py").stdout, "print('hi')\n")
        # a new commit lands on the task's workspace after the first delivery (e.g. a later retry's fix)
        ws = self.ws("t1")
        (ws / "app.py").write_text("print('updated')\n")
        loop._git(ws, "add", "-A"); loop._git(ws, "commit", "-q", "-m", "W2: update")
        d2 = orchestrator.deliver("t1", repo)
        self.assertEqual(d2["branch"], "agentsys/t1")
        self.assertEqual(git(repo, "show", "agentsys/t1:app.py").stdout, "print('updated')\n")   # actually redelivered

    def test_a_branch_modified_in_the_target_repo_is_never_silently_treated_as_ours(self):
        """Review (fourth round): the journal proves we CREATED this branch, not that its CURRENT content
        is still what we last left it at. Without checking the branch's own sha, an unchanged workspace
        (same ws_sha) would make a user-modified branch look like a still-valid delivery, and a changed
        workspace would make deliver() delete that same branch - discarding the user's own added commit -
        since 'ours' alone was treated as 'safe to replace'."""
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        orchestrator.deliver("t1", repo)
        # the user adds their own commit directly onto the delivered branch in the target repo
        git(repo, "checkout", "-q", "agentsys/t1")
        (repo / "mine.py").write_text("print('added after delivery')\n"); git(repo, "add", "-A"); git(repo, "commit", "-q", "-m", "my own addition")
        git(repo, "checkout", "-q", "main")
        # same workspace content (ws_sha unchanged): must NOT silently report "already delivered" success
        with self.assertRaises(RuntimeError) as cm:
            orchestrator.deliver("t1", repo)
        self.assertIn("has changed since this task last delivered to it", str(cm.exception))
        self.assertEqual(git(repo, "show", "agentsys/t1:mine.py").stdout, "print('added after delivery')\n")  # untouched
        # a NEW workspace commit (so a real redelivery would otherwise be attempted): still must not delete it
        ws = self.ws("t1")
        (ws / "app.py").write_text("print('updated')\n"); loop._git(ws, "add", "-A"); loop._git(ws, "commit", "-q", "-m", "W2: update")
        with self.assertRaises(RuntimeError) as cm2:
            orchestrator.deliver("t1", repo)
        self.assertIn("has changed since this task last delivered to it", str(cm2.exception))
        self.assertIn("agentsys/t1", git(repo, "branch").stdout)
        self.assertEqual(git(repo, "show", "agentsys/t1:mine.py").stdout, "print('added after delivery')\n")  # still there

    def test_recovery_after_a_crash_mid_delivery_uses_the_journaled_original_branch(self):
        """Review (third round): if the process crashed right after `checkout -b branch` succeeded (so
        current HEAD IS `branch` itself), a fresh `rev-parse --abbrev-ref HEAD` on retry would adopt the
        broken branch as its own "original" instead of the journal's saved main/start branch."""
        repo = self.home / "target"; repo.mkdir()
        git(repo, "init", "-q", "-b", "main"); (repo / "README").write_text("x"); git(repo, "add", "."); git(repo, "commit", "-q", "-m", "init")
        orchestrator.submit("t1", requirements=REQ, target_repo=repo)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        # Simulate a crash right after deliver() created the branch but before `git am` ran: write the
        # journal as deliver() would at that point, and leave the repo checked out on the half-finished branch.
        tdir = self.home / "tasks" / "t1"
        git(repo, "checkout", "-q", "-b", "agentsys/t1")
        (tdir / "delivery.json").write_text(json.dumps({"target_repo": str(repo), "branch": "agentsys/t1",
                                                         "orig_branch": "main", "ws_sha": "stale", "state": "creating"}))
        e = proc("t1", f)
        self.assertEqual(e["status"], "done")
        self.assertEqual(git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "agentsys/t1")  # delivered, left on the branch
        self.assertEqual(git(repo, "show", "agentsys/t1:app.py").stdout, "print('hi')\n")

    def test_stop_prevents_delivery_even_with_no_agent_call_left_to_make(self):
        """Review (fourth round): control.read() was checked only inside loop.py's agent() - a task sitting
        at an already-approved merge gate has nothing left for the build loop to do, so "stop" was never
        seen at all, and delivery ran to completion as if nothing had been requested."""
        orchestrator.submit("t1", requirements=REQ)
        f = fb(developer=self.dev_writes("t1"))
        proc("t1", f)
        approvals.decide(approvals.pending()[0]["id"], "approved")
        control.set_control("t1", "stop")
        e = proc("t1", f)
        self.assertEqual(e["status"], "stopped")
        self.assertFalse((self.home / "deliveries/t1").exists())
        self.assertEqual(orchestrator.load_entry("t1")["status"], "stopped")
        control.set_control("t1", "resume")
        e2 = proc("t1", f)
        self.assertEqual(e2["status"], "done")
        self.assertTrue((self.home / "deliveries/t1").exists())

    def test_stop_before_intake_prevents_a_real_call(self):
        orchestrator.submit("t1", request_text="make a word counter")
        control.set_control("t1", "stop")
        f = fb()
        e = proc("t1", f)
        self.assertEqual(e["status"], "stopped"); self.assertEqual(f.calls, [])

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

    def test_intake_usage_limit_pauses_instead_of_crashing(self):
        """Review (third round): intake had a BudgetExceeded handler but none for UsageLimitReached, which
        runner.run_agent also raises - this crashed _process() with the queue entry stuck on 'queued'
        forever (never set to 'blocked'), and nothing stopped the scheduler from immediately retrying the
        same call in a tight loop."""
        orchestrator.submit("t1", request_text="x")
        e = proc("t1", fb(orchestrator=runner.UsageLimitReached("5-hour limit reached")))
        self.assertEqual(e["status"], "paused")
        self.assertIn("Usage limit reached", e["note"])
        self.assertIsNotNone(e["resume_after"]); self.assertGreater(e["resume_after"], time.time())

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


class DashboardServerHeaderTests(TmpHome):
    """Review (fourth round): server.py sent no frame-ancestors/X-Frame-Options, so the dashboard (which
    performs approval actions) could be embedded in an attacker's iframe for a clickjacking attack."""

    def test_every_response_blocks_being_framed(self):
        import http.client
        from http.server import ThreadingHTTPServer
        from threading import Thread
        from dashboard import server as dashboard_server

        srv = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.H)
        t = Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            try:
                conn.request("GET", "/api/overview")
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
                self.assertIn("frame-ancestors 'none'", resp.getheader("Content-Security-Policy") or "")
            finally:
                conn.close()
        finally:
            srv.shutdown()
            t.join(timeout=5)
            srv.server_close()


class LockTests(TmpHome):
    def test_second_run_on_the_same_task_is_refused(self):
        orchestrator.submit("t1", requirements=REQ)
        with orchestrator._Lock("t1"):
            f = fb()
            with self.assertRaises(orchestrator.TaskBusy):
                proc("t1", f)
            out = scheduler.run_next(backend=f, env={}, settings_paths=[])
            self.assertIn("another run", out["skipped"]); self.assertEqual(f.calls, [])
        with orchestrator._Lock("t1"):          # released: a fresh acquire succeeds right away
            pass

    def test_lock_is_released_after_an_error(self):
        orchestrator.submit("t1", requirements=REQ)
        with self.assertRaises(RuntimeError):
            with orchestrator._Lock("t1"):
                raise RuntimeError("boom")
        with orchestrator._Lock("t1"):          # released even though the block raised
            pass

    def test_a_stale_looking_lock_file_with_no_real_holder_is_not_a_blocker(self):
        """Review (fourth round): the previous scheme decided staleness from the lock file's mtime, with a
        real race between that check and the unlink. An OS-level lock needs no staleness heuristic at all -
        a lock file nobody is actually holding (e.g. left over from a process that died, or even a bare file
        with old content and an old mtime that nothing ever flocked) can always be acquired immediately."""
        import os, time
        lock = self.home / "queue/t1.lock"; lock.parent.mkdir(parents=True); lock.write_text("999999")
        os.utime(lock, (time.time() - 999_999, time.time() - 999_999))
        with orchestrator._Lock("t1"):
            pass

    def test_a_real_lock_holder_is_not_bypassed_by_an_old_mtime(self):
        """The flip side of the above: an OS lock held by a LIVE process must still block, no matter how old
        the lock file's mtime looks - mtime must never be able to bypass a genuinely-held lock."""
        import os, time
        with orchestrator._Lock("t1"):
            lock = self.home / "queue/t1.lock"
            os.utime(lock, (time.time() - 999_999, time.time() - 999_999))
            with self.assertRaises(orchestrator.TaskBusy):
                with orchestrator._Lock("t1"):
                    pass

    def test_concurrent_threads_racing_for_the_lock_never_both_win(self):
        """Review (fourth round) reproduced the old lock's double-acquisition race with two threads. Run the
        same shape of race against the fixed lock and confirm it genuinely holds under real concurrency,
        not just in sequential before/after checks."""
        import threading
        results = []
        barrier = threading.Barrier(2)
        def worker(name):
            barrier.wait()          # both threads hit _Lock.__enter__ as close together as possible
            try:
                with orchestrator._Lock("t1"):
                    results.append((name, "acquired"))
                    import time as _t; _t.sleep(0.05)
            except orchestrator.TaskBusy:
                results.append((name, "busy"))
        threads = [threading.Thread(target=worker, args=(n,)) for n in ("A", "B")]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sorted(r[1] for r in results), ["acquired", "busy"])   # exactly one of each, never both "acquired"

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
