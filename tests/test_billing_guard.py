import json, os, unittest
from agentsys import billing_guard as g, runner
from tests.base import TmpHome


class GuardTests(TmpHome):
    def test_clean_env_passes(self):
        self.assertEqual(g.run_guard(env={"PATH": "/bin"}, settings_paths=self.no_settings), [])

    def test_fails_when_api_key_is_set(self):
        """Required proof: an API key makes the guard fail."""
        with self.assertRaises(g.BillingGuardError) as cm:
            g.run_guard(env={"ANTHROPIC_API_KEY": "sk-ant-fake"}, settings_paths=self.no_settings)
        self.assertIn("ANTHROPIC_API_KEY", str(cm.exception))
        self.assertNotIn("sk-ant-fake", str(cm.exception))   # never print the value

    def test_every_forbidden_variable_fails(self):
        for name in g.FORBIDDEN_ENV:
            with self.subTest(name), self.assertRaises(g.BillingGuardError):
                g.run_guard(env={name: "x"}, settings_paths=self.no_settings)

    def test_provider_flags(self):
        for name in g.FORBIDDEN_FLAGS:
            with self.subTest(name):
                self.assertTrue(g.check_env({name: "1"}))
                self.assertFalse(g.check_env({name: "0"}))

    def test_empty_value_is_not_set(self):
        self.assertEqual(g.check_env({"ANTHROPIC_API_KEY": ""}), [])

    def test_oauth_token_allowed(self):
        self.assertEqual(g.check_env({"CLAUDE_CODE_OAUTH_TOKEN": "t"}), [])

    def test_api_key_helper_in_settings(self):
        p = self.home / "s.json"; p.write_text(json.dumps({"apiKeyHelper": "echo k"}))
        self.assertTrue(g.check_settings([p]))

    def test_key_in_settings_env_block(self):
        p = self.home / "s.json"; p.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "k"}}))
        self.assertTrue(g.check_settings([p]))

    def test_unparseable_settings_fail_closed(self):
        p = self.home / "s.json"; p.write_text("{not json")
        self.assertTrue(g.check_settings([p]))

    def test_init_message_sources(self):
        self.assertEqual(g.check_init_message({"apiKeySource": "none"}), [])
        self.assertEqual(g.check_init_message({"apiKeySource": "oauth"}), [])
        self.assertTrue(g.check_init_message({"apiKeySource": "user"}))
        self.assertTrue(g.check_init_message({"apiKeySource": "ANTHROPIC_API_KEY"}))
        self.assertTrue(g.check_init_message({}))      # missing -> fail closed
        self.assertTrue(g.check_init_message(None))

    def test_run_agent_blocked_by_env_before_backend_runs(self):
        fb = runner.FakeBackend({"architect": {}})
        with self.assertRaises(g.BillingGuardError):
            runner.run_agent("architect", {}, task_id="t1", backend=fb, env={"ANTHROPIC_API_KEY": "k"},
                             settings_paths=self.no_settings)
        self.assertEqual(fb.calls, [])   # the backend never ran

    def test_run_agent_blocked_when_runtime_credential_is_api_key(self):
        fb = runner.FakeBackend({"developer": {"a": 1}}, api_key_source="user")
        with self.assertRaises(g.BillingGuardError):
            runner.run_agent("developer", {}, task_id="t2", backend=fb, env={}, settings_paths=self.no_settings)

    def test_cli_main_exit_code_2(self):
        os.environ["ANTHROPIC_API_KEY"] = "k"
        self.assertEqual(g.main(), 2)


if __name__ == "__main__":
    unittest.main()
