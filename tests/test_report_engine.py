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


# Reproduces the EXACT byte structure of
#   git log --no-merges -z --format=%x1e%H%x1f%ct%x1f%an --name-only
# Records are separated by RS (\x1e). Within a record the header is
# <sha>\x1f<ts>\x1f<author>; then `--name-only -z` appends a NUL after the last
# format field, then a newline, then the NUL-separated, NUL-terminated file list.
# Verified empirically against real git output before being committed here.
def _log(*commits) -> str:
    # each commit: (sha, ts, author, [files])
    parts = []
    for sha, ts, author, files in commits:
        rec = f"\x1e{sha}\x1f{ts}\x1f{author}"
        if files:
            rec += "\x00\n" + "".join(f"{f}\x00" for f in files)
        else:
            rec += "\x00"
        parts.append(rec)
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


def test_parse_handles_spaces_in_author_and_filename() -> None:
    # Guards the real-git case the parser fix targets: a multi-word author and a
    # governed path containing a space. The old NUL-token heuristic swallowed the
    # first filename into the author; the RS/US format must keep them separate.
    raw = _log(
        ("ddd", "1300", "Carol Space", ["weird dir/pyproject.toml", "weird dir/notes.txt"]),
    )
    commits = parse_governed_commits(raw, IntentClassifier())
    assert len(commits) == 1
    ddd = commits[0]
    assert ddd.author == "Carol Space"  # no filename pollution
    assert "weird dir/pyproject.toml" in ddd.files  # spaced path retained


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


# --- issue #309: the report judges consult coverage like the live gate -------


def _commit(*files: str, sha: str = "aaa", ts: int = 1000) -> GovernedCommit:
    return GovernedCommit(sha=sha, ts=ts, author="d", files=list(files))


def _consult(*coverage: str, ts: float = 990.0, trigger: str = "explicit") -> Consult:
    return Consult(ts=ts, agent_identity="codex", source_session="s1",
                   coverage=tuple(coverage), trigger=trigger)


def _legacy(ts: float = 990.0) -> Consult:
    return Consult(ts=ts, agent_identity="codex", source_session="s1")


def test_extract_consults_reads_coverage_and_trigger() -> None:
    events = [
        {"ts": 990.0, "event_type": "consult", "target_path": "proj",
         "trigger": "prompt_hook", "coverage": ["keyword:schema"]},
        {"ts": 991.0, "event_type": "consult", "target_path": "proj",
         "coverage": None},
        {"ts": 992.0, "event_type": "consult", "target_path": "proj"},
    ]
    new, null_row, legacy = extract_consults(events, workspace="proj")
    assert new.coverage == ("keyword:schema",)
    assert new.trigger == "prompt_hook"
    assert null_row.coverage is None
    assert legacy.coverage is None
    assert legacy.trigger == "explicit"


def test_a_schema_consult_does_not_verify_a_manifest_commit() -> None:
    rep = correlate([_commit("pyproject.toml")], [_consult("keyword:schema")],
                    window_sec=3600)
    assert [c.sha for c in rep.unverified] == ["aaa"]
    assert rep.uncovered == {"aaa": ["path:pyproject.toml"]}
    j = json.loads(format_report(rep, as_json=True))
    assert j["unverified"][0]["uncovered"] == ["path:pyproject.toml"]
    assert "path:pyproject.toml" in format_report(rep, as_json=False)


def test_a_dependency_consult_verifies_manifest_commits_by_family() -> None:
    commit = _commit("pyproject.toml", "services/api/go.mod", "README.md")
    rep = correlate([commit], [_consult("keyword:dependency")], window_sec=3600)
    assert [c.sha for c in rep.verified] == ["aaa"]
    assert rep.timing_only == []


def test_every_signal_of_the_commit_must_be_covered() -> None:
    commit = _commit("pyproject.toml", "db/migrations/0001.sql")
    rep = correlate([commit], [_consult("keyword:dependency")], window_sec=3600)
    assert rep.uncovered == {"aaa": ["path:*/migrations/*"]}


def test_consults_in_the_window_combine_like_fresh_consults_at_the_gate() -> None:
    commit = _commit("pyproject.toml", "db/migrations/0001.sql")
    consults = [_consult("keyword:dependency", ts=980.0),
                _consult("keyword:migration", ts=990.0)]
    rep = correlate([commit], consults, window_sec=3600)
    assert [c.sha for c in rep.verified] == ["aaa"]


def test_a_covering_consult_outside_the_window_does_not_verify() -> None:
    rep = correlate([_commit("pyproject.toml", ts=10000)],
                    [_consult("keyword:dependency", ts=100.0)], window_sec=3600)
    assert [c.sha for c in rep.unverified] == ["aaa"]


def test_a_prompt_hook_consult_never_covers_as_at_the_gate() -> None:
    rep = correlate([_commit("pyproject.toml")],
                    [_consult("keyword:dependency", trigger="prompt_hook")],
                    window_sec=3600)
    assert [c.sha for c in rep.unverified] == ["aaa"]


def test_a_legacy_consult_keeps_timing_only_and_says_so() -> None:
    rep = correlate([_commit("pyproject.toml")], [_legacy()], window_sec=3600)
    assert [c.sha for c in rep.verified] == ["aaa"]
    assert [c.sha for c in rep.timing_only] == ["aaa"]
    j = json.loads(format_report(rep, as_json=True))
    assert j["timing_only"] == ["aaa"]
    assert "timing only" in format_report(rep, as_json=False)


def test_a_covering_consult_is_preferred_over_a_legacy_one() -> None:
    rep = correlate([_commit("pyproject.toml")],
                    [_legacy(), _consult("keyword:dependency")], window_sec=3600)
    assert [c.sha for c in rep.verified] == ["aaa"]
    assert rep.timing_only == []


def test_pair_clearance_keeps_timing_only_behaviour() -> None:
    rep = correlate([_commit("pyproject.toml")], [_consult("keyword:schema")],
                    window_sec=3600, clearance="pair")
    assert [c.sha for c in rep.verified] == ["aaa"]
    assert rep.uncovered == {}
    j = json.loads(format_report(rep, as_json=True))
    assert j["clearance"] == "pair"
    assert "unverified" in j and "uncovered" not in json.dumps(j["unverified"])
