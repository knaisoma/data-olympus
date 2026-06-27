"""Pure correlation engine for the detection floor."""
from __future__ import annotations

import json

from data_olympus.enforce_policy import IntentClassifier
from data_olympus.report import (
    Consult,
    GovernedCommit,
    correlate,
    extract_consults,
    format_report,
    parse_governed_commits,
)


# git log --pretty=format:%H%x00%ct%x00%an --name-only -z output: records are
# separated by NUL; within a record, header fields are NUL-separated, then the
# file list is NUL-separated, and the commit block ends before the next %H.
# We build it explicitly so the parser contract is unambiguous.
def _log(*commits) -> str:
    # each commit: (sha, ts, author, [files])
    parts = []
    for sha, ts, author, files in commits:
        parts.append(f"{sha}\x00{ts}\x00{author}")
        for f in files:
            parts.append(f"\x00{f}")
        parts.append("\x00")  # record terminator before next sha
    return "".join(parts)


def test_parse_picks_only_governed_commits() -> None:
    raw = _log(
        ("aaa", "1000", "Dev One", ["pyproject.toml", "README.md"]),
        ("bbb", "1100", "Dev Two", ["src/util/strings.py"]),
        ("ccc", "1200", "Dev Three", ["db/migrations/0001_init.sql"]),
    )
    commits = parse_governed_commits(raw, IntentClassifier())
    shas = {c.sha for c in commits}
    assert shas == {"aaa", "ccc"}  # bbb touches only non-governed paths
    aaa = next(c for c in commits if c.sha == "aaa")
    assert aaa.ts == 1000
    assert aaa.author == "Dev One"
    assert "pyproject.toml" in aaa.files


def test_extract_consults_filters_by_workspace_and_type() -> None:
    events = [
        {"ts": 990.0, "event_type": "consult", "target_path": "proj", "agent_identity": "codex"},
        {"ts": 995.0, "event_type": "consult", "target_path": "other", "agent_identity": "x"},
        {"ts": 996.0, "event_type": "gate_block", "target_path": "proj", "agent_identity": "y"},
    ]
    consults = extract_consults(events, workspace="proj")
    assert len(consults) == 1
    assert consults[0].agent_identity == "codex"


def test_correlate_verified_within_window() -> None:
    commits = [GovernedCommit(sha="aaa", ts=1000, author="d", files=["pyproject.toml"])]
    consults = [Consult(ts=990.0, agent_identity="codex", source_session="s1")]
    rep = correlate(commits, consults, window_sec=3600)
    assert rep.total_governed == 1
    assert len(rep.verified) == 1
    assert rep.unverified == []


def test_correlate_unverified_when_no_consult_in_window() -> None:
    commits = [GovernedCommit(sha="aaa", ts=10000, author="d", files=["pyproject.toml"])]
    consults = [Consult(ts=100.0, agent_identity="codex", source_session="s1")]
    rep = correlate(commits, consults, window_sec=3600)
    assert rep.unverified and rep.unverified[0].sha == "aaa"
    assert rep.verified == []


def test_format_report_json_and_text() -> None:
    commits = [GovernedCommit(sha="aaa", ts=10000, author="d", files=["pyproject.toml"])]
    rep = correlate(commits, [], window_sec=3600)
    j = json.loads(format_report(rep, as_json=True))
    assert j["total_governed"] == 1
    assert j["unverified"][0]["sha"] == "aaa"
    text = format_report(rep, as_json=False)
    assert "aaa" in text and "1" in text
