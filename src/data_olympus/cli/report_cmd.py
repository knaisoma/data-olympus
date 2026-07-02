"""`data-olympus report`: correlate governed git commits with the consult audit."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from data_olympus.enforce_policy import IntentClassifier
from data_olympus.report import (
    GovernedCommit,
    correlate,
    extract_consults,
    format_report,
    parse_governed_commits,
)


def resolve_default_workspace(start: str | None = None) -> str:
    """Return the workspace key for ``start`` (default: the current directory).

    Resolves to the MAIN worktree's basename, which is identical from the main
    checkout and from any linked git worktree. This is the same key the enforce
    pre-tool hook (``resolve_workspace`` in ``bin/kb-enforce-hook``) and
    ``kb_consult`` use, so a single consultation clears both the pre-tool gate
    and this pre-commit report. Outside a git repository it falls back to the
    plain directory basename.
    """
    base = Path(start) if start else Path.cwd()
    try:
        out = subprocess.run(
            ["git", "-C", str(base), "worktree", "list", "--porcelain"],
            capture_output=True, text=True,
        )
    except OSError:
        return base.name
    if out.returncode == 0:
        for line in out.stdout.splitlines():
            # The FIRST `worktree` entry is always the main worktree, regardless
            # of which linked worktree we run from. This is correct even for
            # separate-git-dir layouts where the git dir is not `<worktree>/.git`.
            if line.startswith("worktree "):
                main_wt = line[len("worktree "):].strip()
                if main_wt:
                    return Path(main_wt).name
                break
    return base.name


# Record separator (RS, \x1e) between commits; unit separator (US, \x1f) between
# header fields. Combined with --name-only -z this yields an unambiguous stream
# that parse_governed_commits() decodes without boundary heuristics. The parser
# and this format MUST stay in lock-step.
_GIT_FORMAT = "%x1e%H%x1f%ct%x1f%an"


def _git_log(rng: str | None, since: str | None) -> tuple[bool, str]:
    args = ["git", "log", "--no-merges", "-z", f"--format={_GIT_FORMAT}", "--name-only"]
    if rng:
        args.append(rng)
    elif since:
        args.append(f"--since={since}")
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        # Surface git's error instead of masquerading as a clean (0-commit) scan.
        if proc.stderr:
            print(proc.stderr.rstrip("\n"), file=sys.stderr)
        return False, ""
    return True, proc.stdout


def _git_staged_files() -> tuple[bool, list[str]]:
    proc = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        if proc.stderr:
            print(proc.stderr.rstrip("\n"), file=sys.stderr)
        return False, []
    return True, [p for p in proc.stdout.split("\x00") if p.strip()]


def _fetch_audit(endpoint: str, token: str, since_ts: int) -> tuple[bool, list[dict[str, Any]]]:
    url = f"{endpoint}/api/v1/audit?since={since_ts}&limit=1000"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            return True, list(data.get("events", []))
    except Exception:  # noqa: BLE001 - unreachable audit degrades gracefully
        return False, []


def _post_event(endpoint: str, token: str, payload: dict[str, Any]) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f"{endpoint}/api/v1/audit/event", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with contextlib.suppress(Exception):  # best-effort emission
        urllib.request.urlopen(req, timeout=5).close()


def run_report(
    *,
    workspace: str,
    rng: str | None,
    since: str | None,
    window_sec: int,
    as_json: bool,
    fail_on_unverified: bool,
    staged: bool = False,
    emit_events: bool = False,
) -> int:
    classifier = IntentClassifier()
    endpoint = os.getenv("KB_ENDPOINT", "http://localhost:8080")
    token = os.getenv("KB_AUTH_TOKEN", "")

    if staged:
        ok, staged_files = _git_staged_files()
        if not ok:
            return 2  # git scan failed; do not mistake an error for a clean repo
        gfiles = [f for f in staged_files
                  if classifier.classify(action_path=f).is_governed_decision]
        # Staged changes have no commit timestamp, so use the real clock: the gate
        # must verify a RECENT consult, not any consult ever recorded. Pin the
        # synthetic commit to `now` and only fetch (and accept) consults within
        # [now - window_sec, now], so a stale consult cannot keep the gate green.
        now = int(time.time())
        commits = (
            [GovernedCommit(sha="STAGED", ts=now, author="", files=gfiles)] if gfiles else []
        )
        since_ts = max(0, now - window_sec)
    else:
        ok, log_text = _git_log(rng, since)
        if not ok:
            return 2  # git scan failed (e.g. bad --range); not a clean repo
        commits = parse_governed_commits(log_text, classifier)
        earliest = min((c.ts for c in commits), default=0)
        since_ts = max(0, earliest - window_sec)

    reachable, events = _fetch_audit(endpoint, token, since_ts)
    consults = extract_consults(events, workspace) if reachable else []

    report = correlate(commits, consults, window_sec=window_sec)

    if as_json:
        body = json.loads(format_report(report, as_json=True))
        body["audit_reachable"] = reachable
        print(json.dumps(body))
    else:
        if not reachable:
            print(f"[KB] warn: audit endpoint {endpoint} unreachable; "
                  f"consult state unknown, listing governed changes only.")
        print(format_report(report, as_json=False))

    if emit_events and report.unverified:
        for c in report.unverified:
            _post_event(endpoint, token, {
                "event_type": "gate_bypass", "workspace": workspace,
                "agent_identity": c.author or "unknown", "source_session": "report",
                "reason": f"{c.sha[:10]}: {', '.join(c.files)}",
            })

    if fail_on_unverified and report.unverified:
        return 3
    return 0
