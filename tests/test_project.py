"""A project repository per task: a new local repo for every new task (the owner's choice), or an existing one picked
for a follow-up task. The workspace starts as a copy of one commit; delivery adds agentsys/<task> there."""
import json, os, subprocess
from pathlib import Path
from agentsys import actions, approvals, assistant, config, dashboard_data as dd, loop, orchestrator, worker
from tests.base import TmpHome
from tests.helpers import DEV, REQ
from tests.test_phase3 import fb, git

H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "X-Agentsys": "1"}


def head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class ProjectRepoTests(TmpHome):
    def make_repo(self, name="existing"):
        r = self.home / name
        r.mkdir()
        git(r, "init", "-q"); git(r, "checkout", "-q", "-b", "main")
        (r / "README.md").write_text("# existing project\n")
        (r / "shell.py").write_text("print('shell from task 1')\n")
        git(r, "add", "-A"); git(r, "commit", "-qm", "task 1 work")
        return r

    def dev(self, tid):
        def write(prompt):
            (self.home / "tasks" / tid / "workspace" / "feature.py").write_text("print('task 2')\n")
            return DEV
        return write

    def test_projects_go_inside_a_separate_data_folder_never_the_real_home(self):
        self.assertEqual(config.projects_dir(), self.home / "projects")

    def test_project_check_explains_what_is_wrong(self):
        empty = self.home / "empty"; empty.mkdir(); git(empty, "init", "-q")
        cases = {"relative/path": "FULL path", str(self.home / "nope"): "does not exist", str(self.home): "not a git repository",
                 str(empty): "no commits yet"}
        for path, why in cases.items():
            with self.assertRaises(ValueError) as c:
                orchestrator.project_check(path)
            self.assertIn(why, str(c.exception), path)
        r = self.make_repo()
        with self.assertRaises(ValueError):
            orchestrator.project_check(str(r), "--upload-pack=x")            # never an option for git
        with self.assertRaises(ValueError):
            orchestrator.project_check(str(r), "no-such-branch")
        top, ref, sha = orchestrator.project_check(str(r))
        self.assertEqual((ref, sha), ("main", head(r)))

    def test_a_new_repository_is_created_for_every_submitted_draft(self):
        sid = assistant.post_message(None, "a word counter")["id"]
        assistant._run_turn(sid, fb(assistant={"reply": "ok", "draft": REQ, "suggestions": [], "conflicts": []}), {}, [])
        s = assistant.load(sid)
        tid, _ = assistant.submit(sid, s["revision"])
        e = orchestrator.load_entry(tid)
        repo = Path(e["target_repo"])
        self.assertEqual(repo.parent, config.projects_dir()); self.assertTrue(repo.name.startswith(tid))
        self.assertEqual(e["base_ref"], "main")
        self.assertIn("Start:", git(repo, "log", "--oneline").stdout)
        self.assertEqual(assistant.submit(sid, s["revision"])[0], tid)                          # idempotent
        self.assertEqual(len(list(config.projects_dir().iterdir())), 1)                         # never a second repo

    def test_a_follow_up_task_builds_on_an_existing_repo_and_delivers_a_branch_there(self):
        r = self.make_repo()
        base = head(r)
        orchestrator.submit("T-0002", requirements=REQ)
        code, body = actions.handle_post("/api/task/project", {"task_id": "T-0002", "repo": str(r), "ref": "main"}, H)
        self.assertEqual(code, 200, body)
        worker.request_run("T-0002")
        self.assertEqual(worker.step(backend=fb(developer=self.dev("T-0002")), env={}, settings_paths=[])["status"], "waiting_approval")
        ws = self.home / "tasks/T-0002/workspace"
        self.assertEqual((ws / "shell.py").read_text(), "print('shell from task 1')\n")            # started from task 1's code
        self.assertEqual(orchestrator.load_entry("T-0002")["project_base"]["sha"], base)
        (r / "unrelated.txt").write_text("x"); git(r, "add", "-A"); git(r, "commit", "-qm", "someone moved main on")
        approvals.decide(approvals.pending()[0]["id"], "approved")
        worker.request_run("T-0002", "continue")
        self.assertEqual(worker.step(backend=fb(), env={}, settings_paths=[])["status"], "done")
        d = orchestrator.load_entry("T-0002")["delivery"]
        self.assertEqual(d["branch"], "agentsys/T-0002")
        patches = "".join((Path(d["patches_dir"]) / p).read_text(encoding="utf-8") for p in d["patches"])
        self.assertIn("feature.py", patches); self.assertNotIn("shell.py", patches)             # only this task's work
        self.assertEqual(git(r, "rev-parse", "agentsys/T-0002~1").stdout.strip(), base)          # branched from the start commit
        self.assertNotIn("unrelated.txt", git(r, "ls-tree", "-r", "--name-only", "agentsys/T-0002").stdout)

    def test_the_repository_cannot_change_once_the_task_started(self):
        r = self.make_repo()
        orchestrator.submit("T-0003", requirements=REQ)
        loop.ensure_workspace(self.home / "tasks/T-0003")
        code, body = actions.handle_post("/api/task/project", {"task_id": "T-0003", "repo": str(r)}, H)
        self.assertEqual(code, 400); self.assertIn("already started", body["error"])
        self.assertFalse(dd.task_detail("T-0003")["run"]["project"]["editable"])

    def test_a_vanished_repository_blocks_with_a_reason_instead_of_crashing(self):
        r = self.make_repo()
        orchestrator.submit("T-0004", requirements=REQ, target_repo=str(r), base_ref="main")
        import shutil; shutil.rmtree(r, ignore_errors=True)
        e = orchestrator.process("T-0004", backend=fb(), env={}, settings_paths=[])
        self.assertEqual(e["status"], "blocked"); self.assertIn("project repository", e["note"])

    def test_a_repository_whose_only_commit_is_empty_works(self):
        r = self.home / "empty-start"; r.mkdir()
        git(r, "init", "-q"); git(r, "commit", "-q", "--allow-empty", "-m", "init")
        orchestrator.submit("T-0006", requirements=REQ, target_repo=str(r))
        e = orchestrator.process("T-0006", backend=fb(developer=self.dev("T-0006")), env={}, settings_paths=[])
        self.assertEqual(e["status"], "waiting_approval", e["note"])

    def test_delete_also_drops_a_waiting_start(self):
        orchestrator.submit("T-0005", requirements=REQ)
        worker.request_run("T-0005")
        orchestrator.delete_task("T-0005")
        self.assertIsNone(worker.get_request("T-0005"))
