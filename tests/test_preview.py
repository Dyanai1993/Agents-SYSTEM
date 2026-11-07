"""Preview what a task built (the owner could approve a result but not try it)."""
import json, urllib.request
from agentsys import actions, preview
from tests.base import TmpHome

H = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "X-Agentsys": "1"}


class PreviewTests(TmpHome):
    def ws(self, tid="T-0001"):
        w = self.home / "tasks" / tid / "workspace"
        w.mkdir(parents=True, exist_ok=True)
        return w

    def tearDown(self):
        for tid in list(preview._servers):
            preview.stop(tid)
        super().tearDown()

    def test_a_built_app_is_found_served_from_its_own_port_and_stopped(self):
        app = self.ws() / "firstwords"
        (app / "dist").mkdir(parents=True)
        (app / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
        (app / "dist" / "index.html").write_text("<html lang='he'><body>מילים ראשונות</body></html>", encoding="utf-8")
        s = preview.find_site("T-0001")
        self.assertTrue(s["root"].endswith("dist")); self.assertTrue(s["can_build"]); self.assertFalse(s["has_deps"])
        st = preview.start("T-0001")
        self.assertTrue(st["url"].startswith("http://127.0.0.1:88"))                  # not the dashboard's port
        self.assertNotIn(":8765", st["url"])
        body = urllib.request.urlopen(st["url"], timeout=5).read().decode("utf-8")
        self.assertIn("מילים ראשונות", body)
        self.assertIsNone(preview.stop("T-0001")["url"])

    def test_a_plain_page_is_previewed_and_nothing_means_nothing(self):
        self.assertIsNone(preview.find_site("T-0001"))
        (self.ws() / "plan.html").write_text("<html dir='rtl'>plan</html>", encoding="utf-8")
        s = preview.find_site("T-0001")
        self.assertEqual(s["entry"], "plan.html"); self.assertFalse(s["can_build"])
        self.assertTrue(preview.start("T-0001")["url"].endswith("/plan.html"))

    def test_build_is_refused_without_a_script_or_dependencies(self):
        (self.ws() / "index.html").write_text("x")
        with self.assertRaises(ValueError):
            preview.build("T-0001")                                                   # no build script
        app = self.ws("T-0002") / "app"
        app.mkdir()
        (app / "package.json").write_text(json.dumps({"scripts": {"build": "vite build"}}))
        with self.assertRaises(ValueError) as c:
            preview.build("T-0002")
        self.assertIn("npm install", str(c.exception))

    def test_the_actions_need_the_dashboard_headers_and_a_valid_task(self):
        (self.ws() / "index.html").write_text("x")
        self.assertEqual(actions.handle_post("/api/preview", {"task_id": "T-0001", "action": "start"}, {})[0], 403)
        self.assertEqual(actions.handle_post("/api/preview", {"task_id": "../x", "action": "start"}, H)[0], 400)
        self.assertEqual(actions.handle_post("/api/preview", {"task_id": "T-0001", "action": "rm"}, H)[0], 400)
        code, body = actions.handle_post("/api/preview", {"task_id": "T-0001", "action": "start"}, H)
        self.assertEqual(code, 200, body); self.assertTrue(body["url"])
