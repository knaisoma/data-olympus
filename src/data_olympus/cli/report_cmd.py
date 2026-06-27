"""`data-olympus report`: correlate governed git commits with the consult audit."""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from typing import Any

from data_olympus.enforce_policy import IntentClassifier
from data_olympus.report import (
    GovernedCommit,
    correlate,
    extract_consults,
    format_report,
    parse_governed_commits,
)

# Record separator (RS, \x1e) between commits; unit separator (US, \x1f) between
# header fields. Combined with --name-only -z this yields an unambiguous stream
# that parse_governed_commits() decodes without boundary heuristics. The parser
# and this format MUST stay in lock-step.
_GIT_FORMAT = "%x1e%H%x1f%ct%x1f%an"


def _git_log(rng: str | None, since: str | None) -> str:
    args = ["git", "log", "--no-merges", "-z", f"--format={_GIT_FORMAT}", "--name-only"]
    if rng:
        args.append(rng)
    elif since:
        args.append(f"--since={since}")
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout


def _git_staged_files() -> list[str]:
    proc = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return [p for p in proc.stdout.split("\x00") if p.strip()]


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


def run_report(
    *,
    workspace: str,
    rng: str | None,
    since: str | None,
    window_sec: int,
    as_json: bool,
    fail_on_unverified: bool,
    staged: bool = False,
) -> int:
    classifier = IntentClassifier()
    if staged:
        gfiles = [f for f in _git_staged_files()
                  if classifier.classify(action_path=f).is_governed_decision]
        commits = [GovernedCommit(sha="STAGED", ts=0, author="", files=gfiles)] if gfiles else []
    else:
        commits = parse_governed_commits(_git_log(rng, since), classifier)
    earliest = min((c.ts for c in commits), default=0)
    endpoint = os.getenv("KB_ENDPOINT", "http://localhost:8080")
    token = os.getenv("KB_AUTH_TOKEN", "")
    reachable, events = _fetch_audit(endpoint, token, max(0, earliest - window_sec))
    consults = extract_consults(events, workspace) if reachable else []

    if staged and commits:
        # Staged changes have no commit timestamp; pin the synthetic commit ts to
        # the newest consult so correlate() marks it verified iff a consult exists
        # for this workspace (and unverified when none do).
        newest = max((c.ts for c in consults), default=None)
        ts = int(newest) if newest is not None else 0
        commits = [GovernedCommit(sha="STAGED", ts=ts, author=commits[0].author,
                                  files=commits[0].files)]

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

    if fail_on_unverified and report.unverified:
        return 3
    return 0
