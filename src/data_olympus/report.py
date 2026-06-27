"""Detection-floor correlation engine.

Pure functions: no git and no network here. The CLI feeds in the raw `git log`
text and the audit events; these functions classify, correlate, and format. That
keeps the policy logic unit-testable without a repo or a server."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from data_olympus.enforce_policy import IntentClassifier


@dataclass(frozen=True)
class GovernedCommit:
    sha: str
    ts: int
    author: str
    files: list[str]


@dataclass(frozen=True)
class Consult:
    ts: float
    agent_identity: str
    source_session: str


@dataclass(frozen=True)
class ComplianceReport:
    verified: list[GovernedCommit] = field(default_factory=list)
    unverified: list[GovernedCommit] = field(default_factory=list)
    consult_count: int = 0

    @property
    def total_governed(self) -> int:
        return len(self.verified) + len(self.unverified)


def parse_governed_commits(git_log_text: str, classifier: IntentClassifier) -> list[GovernedCommit]:
    """Parse `git log --pretty=format:%H%x00%ct%x00%an --name-only -z` output.

    Records are separated by NUL. The first three NUL-separated tokens of a record
    are sha, unix-timestamp, author; the remaining NUL-separated tokens are file
    paths (until the next sha-looking token). A commit is kept only if at least one
    of its files is governed per the classifier."""
    tokens = list(git_log_text.split("\x00"))
    commits: list[GovernedCommit] = []
    i = 0
    n = len(tokens)
    while i < n:
        sha = tokens[i].strip()
        if not sha:
            i += 1
            continue
        # next two tokens are ts, author
        if i + 2 >= n:
            break
        ts_raw = tokens[i + 1].strip()
        author = tokens[i + 2].strip()
        i += 3
        files: list[str] = []
        while i < n:
            tok = tokens[i]
            stripped = tok.strip()
            if not stripped:
                i += 1
                # blank token terminates the file list for this record
                break
            # A new record begins with a bare sha followed by a numeric unix ts.
            # Detect it structurally: an all-hex token whose next token is
            # all-digits starts the next commit. (`git log -z` does not emit a
            # blank separator between records, so this hex+digit-follow rule is
            # the sole record boundary signal.)
            if (
                i + 1 < n
                and all(c in "0123456789abcdef" for c in stripped.lower())
                and tokens[i + 1].strip().isdigit()
            ):
                break
            files.append(stripped)
            i += 1
        if any(classifier.classify(action_path=f).is_governed_decision for f in files):
            try:
                ts = int(ts_raw)
            except ValueError:
                ts = 0
            commits.append(GovernedCommit(sha=sha, ts=ts, author=author, files=files))
    return commits


def extract_consults(audit_events: list[dict[str, Any]], workspace: str) -> list[Consult]:
    out: list[Consult] = []
    for ev in audit_events:
        if ev.get("event_type") == "consult" and ev.get("target_path") == workspace:
            out.append(Consult(
                ts=float(ev.get("ts", 0.0)),
                agent_identity=str(ev.get("agent_identity") or "unknown"),
                source_session=str(ev.get("source_session") or ""),
            ))
    return out


def correlate(
    commits: list[GovernedCommit],
    consults: list[Consult],
    window_sec: int,
    grace_sec: int = 120,
) -> ComplianceReport:
    """A governed commit is verified if a consult exists in [ts - window, ts + grace]."""
    consult_ts = sorted(c.ts for c in consults)
    verified: list[GovernedCommit] = []
    unverified: list[GovernedCommit] = []
    for c in commits:
        lo, hi = c.ts - window_sec, c.ts + grace_sec
        if any(lo <= t <= hi for t in consult_ts):
            verified.append(c)
        else:
            unverified.append(c)
    return ComplianceReport(verified=verified, unverified=unverified, consult_count=len(consults))


def format_report(report: ComplianceReport, *, as_json: bool) -> str:
    if as_json:
        return json.dumps({
            "total_governed": report.total_governed,
            "consult_count": report.consult_count,
            "verified": [c.sha for c in report.verified],
            "unverified": [
                {"sha": c.sha, "ts": c.ts, "author": c.author, "files": c.files}
                for c in report.unverified
            ],
        })
    lines = [
        f"governed changes: {report.total_governed} | "
        f"with a consult on record: {len(report.verified)} | "
        f"unverified: {len(report.unverified)} | consults seen: {report.consult_count}",
    ]
    for c in report.unverified:
        lines.append(f"  UNVERIFIED {c.sha[:10]} by {c.author}: {', '.join(c.files)}")
    if not report.unverified:
        lines.append("  no unverified governed changes")
    return "\n".join(lines)
