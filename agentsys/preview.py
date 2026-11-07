"""Preview what a task built, from the dashboard: the owner could approve a result but not try it ("the system asked
for a review but I can't test the product").

The site is served as static files from its OWN local address (127.0.0.1, one port per task): a different origin
from the dashboard, so the previewed app's own code cannot call the dashboard (approve itself, start runs). Serving
static files runs none of the project's code on this computer. Building does - the project's own build script - so
it happens only when the owner presses "Build again". No model call anywhere.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config, logger

BASE_PORT = 8800
SKIP = {"node_modules", ".git", "qa_tests", "__pycache__", ".venv", "test-results", "playwright-report"}
BUILD_TIMEOUT_S = 300
_servers = {}          # task -> (server, port, root)
_builds = {}           # task -> {"state": "running"|"ok"|"failed", "tail": str, "at": time}
_lock = threading.Lock()


def _ws(task_id):
    return config.home() / "tasks" / logger.safe_task_id(task_id) / "workspace"


def find_site(task_id):
    """Where the task's viewable result is: a built folder (dist/build/out with an index.html) of an app that has a
    build script, or a plain static page. None when there is nothing to show."""
    ws = _ws(task_id)
    if not ws.is_dir():
        return None
    dirs = [ws] + sorted(d for d in ws.iterdir() if d.is_dir() and d.name not in SKIP and not d.name.startswith("."))
    for d in dirs:
        pkg = d / "package.json"
        if pkg.is_file():
            try:
                scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts") or {}
            except (OSError, ValueError):
                scripts = {}
            built = next((d / b for b in ("dist", "build", "out") if (d / b / "index.html").is_file()), None)
            if built or "build" in scripts:
                return {"root": str(built) if built else None, "entry": "index.html", "app_dir": str(d),
                        "can_build": "build" in scripts, "has_deps": (d / "node_modules").is_dir(),
                        "built_at": (built / "index.html").stat().st_mtime if built else None}
    for d in dirs:                                          # a plain page: index.html, or another top-level .html
        pages = sorted(p.name for p in d.glob("*.html"))
        if pages:
            entry = "index.html" if "index.html" in pages else pages[0]
            return {"root": str(d), "entry": entry, "app_dir": None, "can_build": False, "has_deps": False,
                    "built_at": (d / entry).stat().st_mtime}
    return None


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")       # a rebuilt app shows at once
        super().end_headers()


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass                                                 # a browser closing a connection is normal


def status(task_id):
    site = find_site(task_id)
    with _lock:
        srv = _servers.get(task_id)
        b = dict(_builds.get(task_id) or {})
    url = None
    if srv and site and srv[2] == site["root"]:
        url = f"http://127.0.0.1:{srv[1]}/" + ("" if site["entry"] == "index.html" else site["entry"])
    return {"task_id": task_id, "site": site, "url": url, "build": b or None}


def start(task_id):
    """Serve the task's site (again) from its own port. Returns the status with the url."""
    site = find_site(task_id)
    if not site or not site["root"]:
        raise ValueError("nothing to preview yet" + (": press Build again first" if site and site["can_build"] else ""))
    stop(task_id)
    base = BASE_PORT + int("".join(c for c in task_id if c.isdigit())[-2:] or 0)
    last = None
    for port in range(base, base + 60):                     # one port per task: a service worker stays with its app
        try:
            srv = _Server(("127.0.0.1", port), partial(_Quiet, directory=site["root"]))
            break
        except OSError as e:
            last = e
    else:
        raise ValueError(f"no free port near {base}: {last}")
    threading.Thread(target=srv.serve_forever, daemon=True, name=f"preview-{task_id}").start()
    with _lock:
        _servers[task_id] = (srv, port, site["root"])
    logger.log_event(task_id, "dashboard", "decision", f"preview started on port {port}", status="ok")
    return status(task_id)


def stop(task_id):
    with _lock:
        srv = _servers.pop(task_id, None)
    if srv:
        srv[0].shutdown()
        srv[0].server_close()
    return status(task_id)


def build(task_id):
    """Run the project's own build script (npm run build) in the background. Only on the owner's click."""
    site = find_site(task_id)
    if not site or not site["can_build"]:
        raise ValueError("this task has no build script")
    if not site["has_deps"]:
        raise ValueError(f"the dependencies are not installed: run `npm install` in {site['app_dir']} first")
    npm = shutil.which("npm")
    if not npm:
        raise ValueError("npm is not installed on this computer")
    with _lock:
        if (_builds.get(task_id) or {}).get("state") == "running":
            return status(task_id)
        _builds[task_id] = {"state": "running", "tail": "", "at": time.time()}
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}   # a build needs no keys

    def run():
        try:
            r = subprocess.run([npm, "run", "build"], cwd=site["app_dir"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=BUILD_TIMEOUT_S, env=env)
            out = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
            res = {"state": "ok" if r.returncode == 0 else "failed", "tail": "\n".join(out[-25:]), "at": time.time()}
        except subprocess.TimeoutExpired:
            res = {"state": "failed", "tail": f"the build took longer than {BUILD_TIMEOUT_S} s and was stopped", "at": time.time()}
        except OSError as e:
            res = {"state": "failed", "tail": str(e), "at": time.time()}
        with _lock:
            _builds[task_id] = res
        if res["state"] == "ok":
            with _lock:
                running = task_id in _servers
            if running:
                try:
                    start(task_id)                           # serve the fresh build
                except ValueError:
                    pass
    threading.Thread(target=run, daemon=True, name=f"build-{task_id}").start()
    return status(task_id)
