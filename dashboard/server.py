"""Local dashboard server. Read-only in Phase 1. Binds to 127.0.0.1 only. No LLM, no network."""
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agentsys import actions, dashboard_data as d  # noqa: E402

INDEX = Path(__file__).with_name("index.html")


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                return self._send(200, INDEX.read_bytes(), "text/html")
            if u.path == "/api/overview":
                return self._send(200, d.overview())
            if u.path == "/api/events":
                return self._send(200, d.query_events(q.get("task"), q.get("agent"), q.get("type"), q.get("q")))
            if u.path == "/api/messages":
                return self._send(200, d.messages(q.get("task")))
            if u.path == "/api/task":
                t = d.task_detail(q.get("id"))
                return self._send(200 if t else 404, t or {"error": "not found"})
            if u.path == "/api/quality":
                return self._send(200, d.quality())
            if u.path == "/api/approvals":
                return self._send(200, d.approvals_view())
            if u.path == "/api/usage":
                return self._send(200, d.usage_view())
            if u.path == "/api/learning":
                return self._send(200, d.learning_view())
            if u.path == "/api/alerts":
                return self._send(200, d.alerts_view())
            if u.path == "/api/payload":
                p = d.payload(q.get("ref", ""))
                return self._send(200 if p is not None else 404, p if p is not None else {"error": "not found"})
        except Exception as e:  # the dashboard must never crash on bad data
            return self._send(500, {"error": str(e)[:200]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"error": "bad json"})
        code, obj = actions.handle_post(urlparse(self.path).path, body, dict(self.headers))
        self._send(code, obj)

    def log_message(self, *a):
        pass


def main(port=8765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(f"Dashboard: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
