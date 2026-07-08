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
    GET /api/v1/pending                 -> canned pending list
    GET /api/v1/audit                   -> canned audit events
    GET /api/v1/session-recap           -> canned recap (echoes source_session)
    GET /api/v1/onboarding/status       -> synthetic status:
                                            state=onboarded for workspace=example-project,
                                            state=absent for any other workspace.
    GET /api/v1/onboarding/playbook     -> {"kind":..., "text":...} via the real
                                            render_playbook() (single-sourced).

Write routes:
    POST /api/v1/propose/memory         -> committed if confidence>=0.85,
                                            pending_confirmation otherwise;
                                            a payload containing "status: active"
                                            returns a governed-lane demotion
                                            (demotion_reason, no proposal_text)
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

# Make the real render_playbook() importable so the mock server stays
# single-sourced with the REST endpoint instead of hand-maintaining a canned
# copy of the onboarding script text.
_SRC_DIR = Path(__file__).resolve().parents[2] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))
from data_olympus.onboarding_playbook import render_playbook  # noqa: E402


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
                                "target_path": "memory/inbox/x.md",
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
        elif path == "/api/v1/onboarding/playbook":
            kind = qs.get("kind", ["dispatch"])[0]
            workspace = qs.get("workspace", [None])[0]
            component = qs.get("component", [None])[0]
            workspace_remote_url = qs.get("workspace_remote_url", [None])[0]
            component_remote_url = qs.get("component_remote_url", [None])[0]
            try:
                text = render_playbook(
                    kind,
                    workspace=workspace,
                    component=component,
                    workspace_remote_url=workspace_remote_url,
                    component_remote_url=component_remote_url,
                )
            except ValueError as e:
                self._send(400, json.dumps({"error": str(e)}))
                return
            self._send(200, json.dumps({"kind": kind, "text": text}))
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
                                "target_path": "memory/inbox/x.md",
                                "commit_sha": "abc1234",
                            }
                        ],
                        "returned": 1,
                        "limit_hit": False,
                    }
                ),
            )
        elif path == "/api/v1/session-recap":
            source_session = (qs.get("source_session") or [""])[0]
            self._send(
                200,
                json.dumps({
                    "source_session": source_session,
                    "committed": 2,
                    "demoted_to_pending": 1,
                    "rejected": 0,
                }),
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
        if path == "/api/v1/propose/edit" and body.get("target_path") == "trigger/server-error.md":
            # Plain-text 5xx, mimicking the real server's unhandled-KeyError 500.
            # Exercises the CLI's graceful non-JSON error handling (must not feed
            # this body to jq and abort with a parse error).
            self._send(500, "Internal Server Error")
        elif path == "/api/v1/propose/edit" and not body.get("base_commit"):
            # Contract: propose/edit REQUIRES base_commit. The real server 500s
            # without it; here we return a clean 400 so the bats suite asserts
            # the CLI actually sends base_commit (regression guard for the bug
            # where cmd_propose_edit omitted it).
            self._send(
                400,
                json.dumps(
                    {
                        "status": "rejected_missing_base_commit",
                        "reason": "base_commit is required",
                    }
                ),
            )
        elif path == "/api/v1/propose/edit" and body.get("base_commit") != "abc1234":
            # Value check: health-ok.json exposes kb_commit="abc1234", so a
            # correct CLI must fetch /health and send THAT exact value (not a
            # hardcoded "HEAD" or stale string). Guards presence != value.
            self._send(
                400,
                json.dumps(
                    {
                        "status": "rejected_wrong_base_commit",
                        "reason": "base_commit must equal the KB head (abc1234)",
                    }
                ),
            )
        elif path in ("/api/v1/propose/memory", "/api/v1/propose/edit"):
            confidence = float(body.get("confidence", 0.0))
            payload_text = body.get("text") or body.get("postimage", "")
            if "status: active" in payload_text:
                # Governed-lane demotion (issue #112): the real server demotes
                # a status-promoting write to pending with demotion_reason and
                # NO proposal_text; the CLI must print the prompt and stop --
                # never flow into interactive resolve (codex security review
                # blocker: same-command self-approval).
                self._send(
                    202,
                    json.dumps(
                        {
                            "status": "pending_confirmation",
                            "pending_id": "demoted-abc",
                            "demotion_reason": "status_promotion",
                            "operator_prompt": (
                                "DEMOTED to pending review by governed-lane "
                                "write protection (reason: status_promotion); "
                                "run `kb resolve demoted-abc "
                                "--decision approve|reject`."
                            ),
                        }
                    ),
                )
            elif confidence >= 0.85:
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
                resp: dict[str, object] = {
                    "status": "committed", "commit_sha": "resolved-sha",
                }
                # Echo back whether the CLI sent the operator-only secret-scan
                # override, so a bats test can assert the flag round-trips
                # through the CLI -> REST body without a real scanner (issue #71).
                if body.get("override_secret_scan"):
                    resp["secret_scan_override_seen"] = True
                self._send(200, json.dumps(resp))
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
