#!/usr/bin/env python3
"""Tiny HTTP server that serves canned JSON fixtures for bin/kb bats tests.

Usage:
    mock-server.py PORT FIXTURE_DIR

Read routes:
    GET /api/v1/health                  -> health-ok.json
    GET /api/v1/health?mode=degraded    -> health-degraded.json (status 200)
    GET /api/v1/health?mode=503         -> health-degraded.json (status 503)
    GET /api/v1/outline                 -> {"tiers":[],"source_commit":"abc"}
    GET /api/v1/get/STD-U-007           -> get-stdu007.json
    GET /api/v1/get/STD-MISSING         -> 404 {"error":"not_found"}
    GET /api/v1/list?tier=T1&category=foundation -> list-t1-foundation.json
    GET /api/v1/list                    -> 400 {"error":"missing_tier"}
    GET /api/v1/search?q=worktree       -> search-worktree.json
    GET /api/v1/pending                 -> canned pending list (2C-second)
    GET /api/v1/audit                   -> canned audit events (2C-second)
    GET /api/v1/onboarding/status       -> synthetic status (2D-onboarding):
                                            state=onboarded for workspace=example-project,
                                            state=absent for any other workspace.

Write routes (2C-second):
    POST /api/v1/propose/memory         -> committed if confidence>=0.85,
                                            pending_confirmation otherwise
    POST /api/v1/propose/edit           -> same shape as memory
    POST /api/v1/resolve/<pending_id>   -> committed (approve) / rejected (reject)
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

FIXTURE_DIR = Path(sys.argv[2])


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: str | bytes) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fixture(self, name: str) -> str:
        return (FIXTURE_DIR / name).read_text()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        path = parsed.path
        if path == "/api/v1/health":
            mode = qs.get("mode", [""])[0]
            if mode == "degraded":
                self._send(200, self._fixture("health-degraded.json"))
            elif mode == "503":
                self._send(503, self._fixture("health-degraded.json"))
            else:
                self._send(200, self._fixture("health-ok.json"))
        elif path == "/api/v1/outline":
            self._send(200, '{"tiers":[],"source_commit":"abc","degraded":false}')
        elif path == "/api/v1/get/STD-U-007":
            self._send(200, self._fixture("get-stdu007.json"))
        elif path == "/api/v1/get/STD-MISSING":
            self._send(404, '{"error":"not_found","message":"no document with id=\'STD-MISSING\'"}')
        elif path == "/api/v1/list":
            if qs.get("tier") == ["T1"] and qs.get("category") == ["foundation"]:
                self._send(200, self._fixture("list-t1-foundation.json"))
            elif "tier" not in qs:
                self._send(400, '{"error":"missing_tier"}')
            else:
                tier_val = qs.get("tier", [""])[0]
                empty_body = (
                    '{"tier":"' + tier_val + '","category":null,'
                    '"entries":[],"source_commit":"abc","total":0}'
                )
                self._send(200, empty_body)
        elif path == "/api/v1/search":
            self._send(200, self._fixture("search-worktree.json"))
        elif path == "/api/v1/pending":
            self._send(
                200,
                json.dumps(
                    {
                        "pending": [
                            {
                                "pending_id": "p1",
                                "proposal_type": "memory",
                                "target_path": "operator/memory/inbox/x.md",
                                "confidence": 0.4,
                                "agent_identity": "claude",
                                "created_at": 1700000000.0,
                            }
                        ],
                        "source_commit": "abc",
                    }
                ),
            )
        elif path == "/api/v1/onboarding/status":
            workspace = qs.get("workspace", [""])[0]
            component = qs.get("component", [""])[0] or None
            if workspace == "example-project":
                body = {
                    "state": "onboarded",
                    "workspace": "example-project",
                    "component": component,
                    "missing_files": [],
                    "rename_candidates": [],
                }
            else:
                body = {
                    "state": "absent",
                    "workspace": workspace,
                    "component": component,
                    "missing_files": ["README.md", "AGENTS.md"],
                    "rename_candidates": [],
                }
            self._send(200, json.dumps(body))
        elif path == "/api/v1/audit":
            self._send(
                200,
                json.dumps(
                    {
                        "events": [
                            {
                                "ts": 1700000000.0,
                                "event_type": "propose_memory",
                                "status": "committed",
                                "agent_identity": "claude",
                                "target_path": "operator/memory/inbox/x.md",
                                "commit_sha": "abc1234",
                            }
                        ],
                        "returned": 1,
                        "limit_hit": False,
                    }
                ),
            )
        else:
            self._send(404, '{"error":"unknown_route"}')

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            body = {}
        if path in ("/api/v1/propose/memory", "/api/v1/propose/edit"):
            confidence = float(body.get("confidence", 0.0))
            if confidence >= 0.85:
                self._send(
                    200,
                    json.dumps(
                        {
                            "status": "committed",
                            "commit_sha": "abc1234",
                            "push_state": "queued",
                        }
                    ),
                )
            else:
                # Per spec, pending_confirmation returns 202 from the real
                # server, but bin/kb only inspects the body so 200 is fine.
                self._send(
                    202,
                    json.dumps(
                        {
                            "status": "pending_confirmation",
                            "pending_id": "pending-xyz",
                            "proposal_text": body.get("text") or body.get("postimage", ""),
                            "operator_prompt": "Confirm proposal",
                        }
                    ),
                )
        elif path.startswith("/api/v1/resolve/"):
            decision = body.get("decision", "")
            if decision == "approve":
                self._send(
                    200,
                    json.dumps(
                        {"status": "committed", "commit_sha": "resolved-sha"}
                    ),
                )
            elif decision == "reject":
                self._send(200, json.dumps({"status": "rejected"}))
            else:
                self._send(400, '{"error":"bad_decision"}')
        else:
            self._send(404, '{"error":"unknown_route"}')

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: ARG002
        pass  # silence


if __name__ == "__main__":
    port = int(sys.argv[1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
