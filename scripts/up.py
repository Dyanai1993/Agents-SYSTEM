"""Start everything in one terminal: the dashboard and the worker.

  python scripts/up.py [--port 8765]

The dashboard (http://127.0.0.1:<port>) never calls a model. The worker answers your chat messages and runs the tasks
you Start or Continue in the dashboard - nothing else, and no model call while nothing is waiting. Ctrl+C stops both.
The billing guard runs first: with an API-key or provider variable set, nothing starts.
"""
import argparse, subprocess, sys, threading, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agentsys import billing_guard  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=8765)
a = ap.parse_args()

try:
    billing_guard.run_guard()
except billing_guard.BillingGuardError as e:
    sys.exit(f"{e}\nNothing was started.")

import socket  # noqa: E402
with socket.socket() as s:          # seen live: an old dashboard server still held the port, so the new one died at once
    try:
        s.bind(("127.0.0.1", a.port))
    except OSError:
        sys.exit(f"Port {a.port} is already in use - probably an old dashboard server (python dashboard/server.py) is still "
                 f"running. Stop it with Ctrl+C in its terminal, also stop an old `orch.py assistant`, then run this again.\n"
                 f"Nothing was started.")


def pump(name, proc):
    for line in proc.stdout:
        print(f"[{name}] {line}", end="", flush=True)


procs = {
    "dashboard": subprocess.Popen([sys.executable, "-u", str(ROOT / "dashboard" / "server.py"), str(a.port)], cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"),
    "worker": subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts" / "orch.py"), "worker"], cwd=ROOT,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"),
}
for name, p in procs.items():
    threading.Thread(target=pump, args=(name, p), daemon=True).start()
print(f"Open http://127.0.0.1:{a.port}  -  Ctrl+C stops the dashboard and the worker.", flush=True)

try:
    while True:
        for name, p in procs.items():
            if p.poll() is not None:            # one of them ended (e.g. a second worker refused to start): say so, stop
                raise SystemExit(f"[{name}] exited with code {p.returncode}: stopping the other one too.")
        time.sleep(1)
except KeyboardInterrupt:
    print("stopping...", flush=True)
finally:
    # Ctrl+C reached both children too: give them time to stop cleanly (the worker removes its heartbeat and leaves
    # no half-written state) before forcing them. Seen live: an immediate terminate left a fresh heartbeat behind.
    deadline = time.time() + 10
    while time.time() < deadline and any(p.poll() is None for p in procs.values()):
        time.sleep(0.2)
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
