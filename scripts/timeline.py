"""Human-readable timeline of one task: python scripts/timeline.py T-0001"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import dashboard_data as d  # noqa: E402

task = sys.argv[1] if len(sys.argv) > 1 else None
ev, bad = d.all_events(task)
tok = 0
for e in ev:
    tok += e["tokens_estimate"]
    flag = "!!" if e["status"] in ("error", "blocked") else "  "
    print(f"{e['ts'][11:19]} {flag} {e['agent']:<12} {e['event_type']:<12} {e['summary']}")
print(f"\n{len(ev)} events, ~{tok} tokens, {bad} unreadable log lines")
