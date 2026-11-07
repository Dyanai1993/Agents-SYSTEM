"""Skills: expert playbooks for the Architect, Developer and Tester, chosen by code for each call."""
import json
from agentsys import loop, runner, skills
from tests.base import TmpHome
from tests.helpers import DEV, REQ, plan, t_pass


class SkillSelectionTests(TmpHome):
    def names(self, role, text):
        return [b["name"] for b in skills.select(role, text)]

    def test_a_react_ui_task_gets_the_front_end_and_design_playbooks(self):
        text = "A Hebrew PWA built with React, TypeScript and Vite; a calm UI with a light/dark theme"
        self.assertEqual(self.names("architect", text)[0], "architecture")            # always for the architect
        self.assertIn("frontend-react", self.names("architect", text)); self.assertIn("ui-design", self.names("architect", text))
        dev = self.names("developer", text)
        self.assertEqual(dev[0], "testing"); self.assertEqual(set(dev[1:]), {"frontend-react", "ui-design"})
        self.assertEqual(self.names("tester", text), ["testing"])

    def test_a_python_cli_task_gets_the_python_playbook_and_nothing_for_the_web(self):
        dev = self.names("developer", "A Python CLI with argparse that converts CSV to JSON, tested with unittest")
        self.assertIn("python", dev); self.assertNotIn("frontend-react", dev); self.assertNotIn("ui-design", dev)

    def test_whole_words_only_and_bounded(self):
        self.assertNotIn("ui-design", self.names("developer", "build a quick guide"))     # "ui" inside "build"/"guide"
        for role in ("architect", "developer", "tester", "security", "assistant"):
            books = skills.select(role, "react vite ui design python css html page app pwa test")
            self.assertLessEqual(len(books), skills.MAX_PER_CALL)
            self.assertLessEqual(sum(len(b["text"]) for b in books), skills.MAX_CHARS)
        self.assertEqual(skills.select("security", "react ui python"), [])                # security keeps its own rules

    def test_the_agents_receive_them_in_their_input(self):
        req = dict(REQ, goal="A React + Vite page with a polished UI")
        fb = runner.FakeBackend({"architect": plan(1), "developer": DEV, "tester": t_pass})
        loop.run_task("t1", req, backend=fb, env={}, settings_paths=[])
        got = {c["role"]: [b["name"] for b in json.loads(c["prompt"]).get("playbooks", [])] for c in fb.calls}
        self.assertIn("architecture", got["architect"]); self.assertIn("ui-design", got["developer"])
        self.assertEqual(got["tester"], ["testing"])
        self.assertNotIn("playbooks", json.loads([c for c in fb.calls if c["role"] == "security"][0]["prompt"]))
