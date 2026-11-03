"""Billing guard. Fails loudly when anything could switch billing away from the subscription.

Runs at startup, before every task, and before every agent call.
Exit code 2 from the CLI means "do not run".
"""
import json
import os
import sys
from pathlib import Path

# Any of these (non-empty) can route usage to pay-per-token billing.
FORBIDDEN_ENV = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "AWS_BEARER_TOKEN_BEDROCK",
]
# Provider switches: only "1"/"true" turn them on.
FORBIDDEN_FLAGS = ["CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"]
# `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) is a subscription token. It is allowed.

# Values of apiKeySource in the SDK/CLI init message that mean "subscription login".
OK_KEY_SOURCES = {"none", "oauth"}


class BillingGuardError(RuntimeError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("BILLING GUARD FAILED: " + " | ".join(self.problems))


def check_env(env=None) -> list:
    env = os.environ if env is None else env
    problems = []
    for name in FORBIDDEN_ENV:
        if (env.get(name) or "").strip():
            problems.append(f"env var {name} is set (value hidden)")
    for name in FORBIDDEN_FLAGS:
        if (env.get(name) or "").strip().lower() in ("1", "true", "yes"):
            problems.append(f"env flag {name} is enabled")
    return problems


def default_settings_paths(cwd=None) -> list:
    cwd = Path(cwd or os.getcwd())
    return [
        Path.home() / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]


def check_settings(paths=None) -> list:
    problems = []
    for p in paths if paths is not None else default_settings_paths():
        p = Path(p)
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            problems.append(f"cannot parse {p}: guard refuses to guess")
            continue
        if not isinstance(data, dict):
            continue
        if data.get("apiKeyHelper"):
            problems.append(f"apiKeyHelper is set in {p}")
        block = data.get("env")
        if isinstance(block, dict):
            for k in FORBIDDEN_ENV:
                if str(block.get(k) or "").strip():
                    problems.append(f"{k} is set in the env block of {p}")
            for k in FORBIDDEN_FLAGS:
                if str(block.get(k) or "").strip().lower() in ("1", "true", "yes"):
                    problems.append(f"{k} is enabled in the env block of {p}")
    return problems


def check_init_message(init) -> list:
    """init = the `system/init` message of the SDK or `claude -p --output-format stream-json`.

    Must carry apiKeySource. Missing or unknown value = fail (fail closed).
    """
    if not isinstance(init, dict):
        return ["no init message: cannot prove the credential source"]
    src = init.get("apiKeySource", init.get("api_key_source"))
    if src is None:
        return ["init message has no apiKeySource: cannot prove subscription login"]
    if str(src).lower() not in OK_KEY_SOURCES:
        return [f"credential source is '{src}', not the subscription login"]
    return []


def run_guard(*, env=None, settings_paths=None, init=None, require_init=False, raise_on_fail=True) -> list:
    problems = check_env(env) + check_settings(settings_paths)
    if init is not None or require_init:
        problems += check_init_message(init)
    if problems and raise_on_fail:
        raise BillingGuardError(problems)
    return problems


def main(argv=None) -> int:
    problems = run_guard(raise_on_fail=False)
    try:  # best-effort alert for the dashboard
        from . import logger
        if problems:
            logger.log_event("_system", "guard", "alert", "billing guard failed", payload={"problems": problems}, status="blocked")
    except Exception:
        pass
    if problems:
        print("BILLING GUARD FAILED", file=sys.stderr)
        for p in problems:
            print(" -", p, file=sys.stderr)
        print("Unset these and run `claude login`. Also check by hand that 'extra usage' is OFF in claude.ai settings.", file=sys.stderr)
        return 2
    print("billing guard: OK (env and settings clean). Init-message check runs on every agent call.")
    print("reminder: 'extra usage' cannot be read by code. Verify it is OFF in claude.ai settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
