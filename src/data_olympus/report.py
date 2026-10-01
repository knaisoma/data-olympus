"""Detection-floor correlation engine.

Pure functions: no git and no network here. The CLI feeds in the raw `git log`
text and the audit events; these functions classify, correlate, and format. That
keeps the policy logic unit-testable without a repo or a server."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from data_olympus.enforce_policy import (
    EXPLICIT_TRIGGER,
    GATE_CLEARANCE_INTENT,
    GATE_CLEARANCE_PAIR,
    IntentClassifier,
    uncovered_signals,
)


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
    # Issue #309: the consult's coverage set from its audit row. None on a row
    # written before coverage was recorded, which can be judged by timing only.
    coverage: tuple[str, ...] | None = None
    trigger: str = EXPLICIT_TRIGGER


@dataclass(frozen=True)
class ComplianceReport:
    verified: list[GovernedCommit] = field(default_factory=list)
    unverified: list[GovernedCommit] = field(default_factory=list)
    consult_count: int = 0
    clearance: str = GATE_CLEARANCE_INTENT
    # Verified commits whose only clearing evidence was a consult row with no
    # coverage recorded, so they were judged by timing alone (issue #309).
    timing_only: list[GovernedCommit] = field(default_factory=list)
    # Under intent clearance: each unverified commit's signals that no consult
    # in its window covered, keyed by sha.
    uncovered: dict[str, list[str]] = field(default_factory=dict)

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
            coverage = ev.get("coverage")
            out.append(Consult(
                ts=float(ev.get("ts") or 0.0),
                agent_identity=str(ev.get("agent_identity") or "unknown"),
                source_session=str(ev.get("source_session") or ""),
                coverage=(
                    tuple(str(sig) for sig in coverage)
                    if isinstance(coverage, list) else None
                ),
                # A row from before the trigger split was a gate-clearing
                # consult, as the ledger also treats it.
                trigger=str(ev.get("trigger") or EXPLICIT_TRIGGER),
            ))
    return out


def commit_signals(commit: GovernedCommit, classifier: IntentClassifier) -> list[str]:
    """The gate signals of a commit's files, in file order, without repeats."""
    signals: list[str] = []
    for f in commit.files:
        for sig in classifier.classify(action_path=f).signals:
            if sig not in signals:
                signals.append(sig)
    return signals


def correlate(
    commits: list[GovernedCommit],
    consults: list[Consult],
    window_sec: int,
    grace_sec: int = 120,
    *,
    classifier: IntentClassifier | None = None,
    clearance: str = GATE_CLEARANCE_INTENT,
) -> ComplianceReport:
    """Judge each governed commit against the consults in [ts - window, ts + grace].

    Under ``clearance="pair"`` any consult in the window verifies it. Under
    ``"intent"`` (issue #309) the explicit consults in the window must together
    cover every signal of the commit's files, by the live gate's rule
    (``uncovered_signals``); in-window consults combine as fresh consults do at
    the gate, and a prompt-hook consult covers nothing. A consult row with no
    coverage recorded predates the rule: when one is in the window and coverage
    does not settle the commit, it is verified by timing only and listed in
    ``timing_only``."""
    classifier = classifier or IntentClassifier()
    verified: list[GovernedCommit] = []
    unverified: list[GovernedCommit] = []
    timing_only: list[GovernedCommit] = []
    uncovered: dict[str, list[str]] = {}
    for c in commits:
        lo, hi = c.ts - window_sec, c.ts + grace_sec
        in_window = [k for k in consults if lo <= k.ts <= hi]
        if clearance == GATE_CLEARANCE_PAIR:
            (verified if in_window else unverified).append(c)
            continue
        covered: set[str] = set()
        for k in in_window:
            if k.coverage is not None and k.trigger == EXPLICIT_TRIGGER:
                covered.update(k.coverage)
        missing = uncovered_signals(commit_signals(c, classifier), covered)
        if not missing:
            verified.append(c)
        elif any(k.coverage is None for k in in_window):
            verified.append(c)
            timing_only.append(c)
        else:
            unverified.append(c)
            uncovered[c.sha] = missing
    return ComplianceReport(
        verified=verified, unverified=unverified, consult_count=len(consults),
        clearance=clearance, timing_only=timing_only, uncovered=uncovered,
    )


def format_report(report: ComplianceReport, *, as_json: bool) -> str:
    if as_json:
        unverified: list[dict[str, Any]] = []
        for c in report.unverified:
            entry: dict[str, Any] = {
                "sha": c.sha, "ts": c.ts, "author": c.author, "files": c.files,
            }
            if c.sha in report.uncovered:
                entry["uncovered"] = report.uncovered[c.sha]
            unverified.append(entry)
        return json.dumps({
            "total_governed": report.total_governed,
            "consult_count": report.consult_count,
            "clearance": report.clearance,
            "verified": [c.sha for c in report.verified],
            "timing_only": [c.sha for c in report.timing_only],
            "unverified": unverified,
        })
    lines = [
        f"governed changes: {report.total_governed} | "
        f"verified: {len(report.verified)} | "
        f"unverified: {len(report.unverified)} | consults seen: {report.consult_count}",
    ]
    if report.clearance == GATE_CLEARANCE_PAIR:
        lines.append("  clearance pair: any consult in the window verifies (timing only)")
    for c in report.timing_only:
        lines.append(
            f"  TIMING ONLY {c.sha[:10]} by {c.author}: {', '.join(c.files)} "
            "(consult recorded without coverage; judged by timing only)"
        )
    for c in report.unverified:
        line = f"  UNVERIFIED {c.sha[:10]} by {c.author}: {', '.join(c.files)}"
        if c.sha in report.uncovered:
            line += f" (uncovered: {', '.join(report.uncovered[c.sha])})"
        lines.append(line)
    if not report.unverified:
        lines.append("  no unverified governed changes")
    return "\n".join(lines)
