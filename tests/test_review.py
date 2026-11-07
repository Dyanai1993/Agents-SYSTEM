"""Phase B of the master prompt: one review screen, delivery of exactly the approved commit, a delivery receipt."""
import json, os
from agentsys import actions, approvals, dashboard_data as dd, loop, orchestrator, worker
from tests.base import TmpHome
from tests.helpers import DEV, REQ
from tests.test_phase3 import fb, git

H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "X-Agentsys": "1"}
HEBREW = "print('שלום, כפתור עצירה')\n"


class ReviewTests(TmpHome):
    def ws(self, tid="T-0001"):
        return self.home / "tasks" / tid / "workspace"

    def to_review(self, tid="T-0001"):
        orchestrator.submit(tid, requirements=REQ)

        def dev(prompt):
            (self.ws(tid) / "app.py").write_text(HEBREW, encoding="utf-8")
            return DEV
        e = orchestrator.process(tid, backend=fb(developer=dev), env={}, settings_paths=[])
        self.assertEqual(e["status"], "waiting_approval")
        return approvals.latest(tid, "merge")[0]

    def test_the_review_shows_criteria_with_evidence_files_and_the_diff_of_the_approval(self):
        ap = self.to_review()
        v = dd.review_view("T-0001")
        self.assertEqual(v["approval"]["id"], ap["id"]); self.assertEqual(v["approval"]["status"], "pending")
        self.assertEqual(v["commit"], orchestrator.approved_sha(ap["diff_ref"]))
        self.assertIs(v["stale"], False)
        self.assertEqual([c["id"] for c in v["criteria"]], ["AC1"])
        self.assertTrue(v["criteria"][0]["passed"]); self.assertEqual(v["criteria"][0]["checks"][0]["evidence"], "all ... ok")
        self.assertEqual(v["final"]["verdict"], "pass")
        self.assertIn("app.py", [f["path"] for f in v["files"]])
        self.assertIn("שלום", v["diff"])                                   # UTF-8 from git, not the Windows code page
        self.assertIsNotNone(v["security"])

    def test_a_workspace_change_after_the_request_makes_it_stale_and_the_shown_diff_stays_the_approved_one(self):
        self.to_review()
        (self.ws() / "later.py").write_text("x = 2\n")
        git(self.ws(), "add", "-A"); git(self.ws(), "commit", "-qm", "later change")
        v = dd.review_view("T-0001")
        self.assertIs(v["stale"], True)
        self.assertNotIn("later.py", v["diff"])                            # what is shown = what the approval delivers

    def test_approve_and_deliver_ships_exactly_the_approved_commit_with_a_receipt(self):
        ap = self.to_review()
        out = self.home / "deliveries" / "T-0001"
        out.mkdir(parents=True)
        (out / "0009-stale-from-an-earlier-delivery.patch").write_text("old")
        self.assertEqual(actions.handle_post("/api/approval/decide", {"id": ap["id"], "decision": "approved"}, H)[0], 200)
        self.assertEqual(actions.handle_post("/api/task/run", {"task_id": "T-0001", "action": "continue"}, H)[0], 200)
        r = worker.step(backend=fb(), env={}, settings_paths=[])
        self.assertEqual(r["status"], "done")
        d = orchestrator.load_entry("T-0001")["delivery"]
        self.assertEqual(d["commit"], orchestrator.approved_sha(ap["diff_ref"])); self.assertEqual(d["approval_id"], ap["id"])
        self.assertEqual(sorted(p.name for p in out.glob("*.patch")), d["patches"])   # exactly this delivery's patches
        self.assertNotIn("0009-stale-from-an-earlier-delivery.patch", d["patches"])
        self.assertEqual(dd.review_view("T-0001")["delivery"]["commit"], d["commit"])  # the receipt is on the review

    def test_deliver_cuts_the_patches_at_the_given_commit_not_at_head(self):
        self.to_review()
        approved = loop._head(self.ws())
        (self.ws() / "later.py").write_text("x = 2\n")
        git(self.ws(), "add", "-A"); git(self.ws(), "commit", "-qm", "later change")
        d = orchestrator.deliver("T-0001", commit=approved)
        text = "".join((self.home / "deliveries" / "T-0001" / p).read_text(encoding="utf-8") for p in d["patches"])
        self.assertIn("app.py", text); self.assertNotIn("later.py", text)
        self.assertEqual(d["commit"], approved)
        with self.assertRaises(RuntimeError):
            orchestrator.deliver("T-0001", commit="0" * 40)                   # an unknown commit is refused

    def test_approved_sha_and_the_endpoint(self):
        self.assertEqual(orchestrator.approved_sha("git:" + "a" * 40 + ":req:x:sec:y"), "a" * 40)
        for bad in (None, "", "git:short:req:x", "req:x", "git:" + "G" * 40):
            self.assertIsNone(orchestrator.approved_sha(bad))
        orchestrator.submit("T-0002", requirements=REQ)
        self.assertIsNone(dd.review_view("T-0002"))                          # no merge approval yet
        self.assertIsNone(dd.review_view("../x"))


class GitUtf8Tests(TmpHome):
    def test_git_output_with_hebrew_is_decoded_as_utf8(self):
        ws = self.home / "ws"
        loop.ensure_workspace(self.home)
        ws = self.home / "workspace"
        (ws / "שלום.py").write_text(HEBREW, encoding="utf-8")
        st = loop._git(ws, "status", "--porcelain").stdout
        self.assertIn("שלום.py", st)                                       # core.quotepath=false + utf-8
        loop._git(ws, "add", "-A"); loop._git(ws, "commit", "-qm", "heb")
        self.assertIn("כפתור", loop._git(ws, "show", "HEAD").stdout)
