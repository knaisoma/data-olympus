"""Minimal mock of the enforce REST endpoints for bats hook tests.

/api/v1/consult       -> {"is_governed_decision": true, "rules": [...], ...}
/api/v1/gate/check    -> verdict depends on action_path / session_id
/api/v1/compliance    -> {"counts": {}, "by_agent": {}}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/v1/compliance"):
            self._send({"counts": {}, "by_agent": {}})
        else:
            self._send({"error": "not_found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or "{}")
        if self.path == "/api/v1/consult":
            self._send({
                "is_governed_decision": True,
                "rules": [{"id": "STD-U-002", "path": "p", "title": "Style",
                           "snippet": "...", "score": 1.0, "status": "", "type": ""}],
                "consulted_at": 1.0, "ttl_seconds": 300,
            })
        elif self.path == "/api/v1/gate/check":
            path = (body.get("action_path") or "")
            if path.endswith("pyproject.toml") and body.get("session_id") != "allowme":
                self._send({"verdict": "consult_required",
                            "reason": "governed action; call kb_consult first",
                            "rules": []})
            else:
                self._send({"verdict": "allow", "reason": "ok", "rules": []})
        else:
            self._send({"error": "not_found"}, 404)


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
