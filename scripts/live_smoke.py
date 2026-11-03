"""LIVE smoke test: 1 Haiku call on your Claude login. Run after `claude login`.

  python scripts/live_smoke.py            (uses the `claude -p` backend)
  python scripts/live_smoke.py --backend sdk
"""
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import billing_guard, runner, smoke  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--backend", default="cli", choices=["cli", "sdk"])
a = ap.parse_args()
try:
    billing_guard.run_guard()
except billing_guard.BillingGuardError as e:
    print(e, file=sys.stderr); sys.exit(2)
try:
    ok, lines = smoke.smoke(runner.get_backend(a.backend))
except runner.UsageLimitReached as e:
    print("usage limit reached:", e); sys.exit(3)
except Exception as e:
    print("smoke failed:", type(e).__name__, e); sys.exit(1)
print("\n".join(lines))
print("\nLIVE SMOKE:", "OK" if ok else "FAILED. Copy this output and the newest file in logs/ back to Claude.")
sys.exit(0 if ok else 1)
