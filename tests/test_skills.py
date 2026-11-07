"""Skills: expert playbooks for the Architect, Developer and Tester, chosen by code for each call - on the real role
payloads, including the FINAL regression; an unrelated work item gets only the guidance it needs."""
import json
from agentsys import loop, runner, skills, worker
from tests.base import TmpHome
from tests.helpers import DEV, REQ, plan, t_pass


def names(books):
    return [b["name"] for b in books]


def item(i, title, covers=("AC1",), files=()):
    return {"id": f"W{i}", "title": title, "files": list(files), "acceptance_criteria": ["works"], "covers": list(covers),
            "depends_on": [f"W{i - 1}"] if i > 1 else []}


class SkillSelectionTests(TmpHome):
    def test_a_react_ui_task_gets_the_front_end_and_design_playbooks(self):
        text = "A Hebrew PWA built with React, TypeScript and Vite; a calm UI with a light/dark theme"
        arch = names(skills.select("architect", text))
        self.assertEqual(arch[0], "architecture"); self.assertLessEqual(len(arch), skills.MAX_PER_CALL)
        dev = names(skills.select("developer", text))
        self.assertEqual(dev[0], "testing"); self.assertTrue({"frontend-react", "ui-design"} & set(dev))

    def test_a_python_cli_task_gets_the_python_playbook_and_nothing_for_the_web(self):
        dev = names(skills.select("developer", "A Python CLI with argparse that converts CSV to JSON, tested with unittest"))
        self.assertIn("python", dev)
        for web in ("frontend-react", "ui-design", "pwa-reliability"):
            self.assertNotIn(web, dev)

    def test_whole_words_only_and_bounded(self):
        self.assertNotIn("ui-design", names(skills.select("developer", "build a quick guide")))
        for role in ("architect", "developer", "tester", "security", "assistant"):
            books = skills.select(role, "react vite ui design python css html page app pwa test offline service worker")
            self.assertLessEqual(len(books), skills.MAX_PER_CALL)
            self.assertLessEqual(sum(len(b["text"]) for b in books), skills.MAX_CHARS)
        self.assertEqual(skills.select("security", "react ui python offline"), [])        # security keeps its own rules

    def test_each_role_gets_only_its_own_section(self):
        def pwa(role):
            return next(b["text"] for b in skills.select(role, "service worker offline cache update") if b["name"] == "pwa-reliability")
        a, d, t = pwa("architect"), pwa("developer"), pwa("tester")
        for text in (a, d, t):
            self.assertIn("ACTIVE VERSION", text)                                          # the shared invariants
        self.assertIn("## For the Architect", a); self.assertNotIn("## For the Tester", a)
        self.assertIn("## For the Developer", d); self.assertNotIn("## For the Architect", d)
        self.assertIn("## For the Tester", t); self.assertNotIn("## For the Developer", t)

    def test_the_bundle_is_in_the_build_id(self):
        self.assertIn("agentsys/playbooks/*.md", worker.CODE_GLOBS)


class SkillPayloadTests(TmpHome):
    """The real role payloads of a run: an offline React app whose second item is only about the theme."""
    REQ = dict(REQ, goal="An offline-capable PWA in React with a service worker download and update",
               acceptance_criteria=["the theme toggle persists", "an offline reload works after the download"])

    def run_task(self):
        p = plan(2, covers=("AC1", "AC2"))
        p["tech_choices"] = [{"choice": "React + Vite", "reason": "the owner's stack", "rejected": []}]
        p["work_items"] = [item(1, "Service worker download with verified offline cache", ("AC2",), ["src/sw.js"]),
                           item(2, "Light and dark theme toggle that persists", ("AC1",), ["src/theme.ts"])]
        fb = runner.FakeBackend({"architect": p, "developer": DEV, "tester": t_pass})
        loop.run_task("t1", self.REQ, backend=fb, env={}, settings_paths=[])
        calls = []
        for c in fb.calls:
            pr = json.loads(c["prompt"])
            calls.append((c["role"], (pr.get("work_item") or {}).get("id"), names(pr.get("playbooks", []))))
        return calls

    def test_selection_on_the_real_payloads_including_final(self):
        calls = self.run_task()
        get = {(r, i): b for r, i, b in calls}
        self.assertIn("architecture", get[("architect", None)]); self.assertIn("pwa-reliability", get[("architect", None)])
        self.assertIn("pwa-reliability", get[("developer", "W1")])                         # the offline item
        self.assertNotIn("pwa-reliability", get[("developer", "W2")])                      # the theme item: not needed
        self.assertIn("pwa-reliability", get[("tester", "W1")]); self.assertNotIn("pwa-reliability", get[("tester", "W2")])
        self.assertIn("pwa-reliability", get[("tester", "FINAL")])                         # integrated checks at the end
        self.assertEqual(get[("security", None)], [])
