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
    """Parse `git log --no-merges -z --format=%x1e%H%x1f%ct%x1f%an --name-only` output.

    Records are separated by RS (\\x1e). Within a record the header fields are
    separated by US (\\x1f): the first three are sha, unix-timestamp, author. The
    `--name-only -z` flags then append a NUL after the last format field, a
    newline, and finally the NUL-separated (NUL-terminated) file list. So the
    third US-field of a record is `<author>\\x00\\n<file>\\x00<file>\\x00...`; we
    split the author off at the first newline and read the rest as the file list.
    Unambiguous record/field separators avoid the heuristic boundary-detection
    that real git output (author joined to the first filename by a literal
    newline) defeats. A commit is kept only if at least one of its files is
    governed per the classifier."""
    commits: list[GovernedCommit] = []
    for record in git_log_text.split("\x1e"):
        # The leading split produces an empty chunk; empty commits leave only the
        # header with no file list. Skip anything with no real header content.
        if not record.strip("\x00\n"):
            continue
        fields = record.split("\x1f")
        if len(fields) < 3:
            continue
        sha = fields[0].strip()
        ts_raw = fields[1].strip()
        rest = fields[2]
        if "\n" in rest:
            author_part, files_blob = rest.split("\n", 1)
        else:
            author_part, files_blob = rest, ""
        author = author_part.rstrip("\x00").strip()
        files = [f for f in files_blob.split("\x00") if f.strip()]
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
                ts=float(ev.get("ts") or 0.0),
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
