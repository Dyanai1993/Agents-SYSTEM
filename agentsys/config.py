"""Central settings. Every number marked (assumption) must be calibrated from real runs."""
import os
from pathlib import Path

_DEFAULT_HOME = Path(__file__).resolve().parent.parent


def home() -> Path:
    """Project root. Tests set AGENTSYS_HOME to a temp directory."""
    return Path(os.environ.get("AGENTSYS_HOME") or _DEFAULT_HOME)


PLAN_TIER = "max5x"

# Aliases work for both the SDK and `claude -p`.
MODELS = {"haiku": "haiku", "sonnet": "sonnet", "opus": "opus"}

# Role -> default model, tools, output schema. Agent prompts arrive in Phase 2.
ROLES = {
    "orchestrator": {"model": "haiku", "tools": [], "schema": "requirements"},
    "architect": {"model": "sonnet", "tools": ["Read", "Grep", "Glob"], "schema": "plan"},
    "developer": {"model": "sonnet", "tools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob"], "schema": "dev_report"},
    "tester": {"model": "sonnet", "tools": ["Read", "Bash", "Grep", "Glob"], "schema": "test_report"},
    "reviewer": {"model": "haiku", "tools": ["Read"], "schema": None},
}

# Per-task caps for Max 5x (assumption).
TASK_CAPS = {"call_cap": 30, "fix_cap": 3, "replan_cap": 2, "timeout_s": 45 * 60}

# Usage weights per token by model (assumption).
MODEL_WEIGHT = {"haiku": 0.3, "sonnet": 1.0, "opus": 5.0}
# Rolling window sizes in weighted tokens for Max 5x (assumption, NOT official).
WINDOW_5H_UNITS = 3_000_000
WINDOW_7D_UNITS = 25_000_000
# Refuse to start a large task above this share of the window.
WINDOW_REFUSE_SHARE = 0.85

# Per-role runtime limits (assumption). max_turns = tool rounds, timeout in seconds.
ROLE_LIMITS = {
    "orchestrator": {"max_turns": 3, "timeout_s": 120},
    "architect": {"max_turns": 12, "timeout_s": 600},
    "developer": {"max_turns": 30, "timeout_s": 1200},
    "tester": {"max_turns": 25, "timeout_s": 900},
    "reviewer": {"max_turns": 8, "timeout_s": 300},
}
