"""Unit tests for the maintenance inventory's planning and parsing.

The deduplication rule is the part that must not regress: a weekly run must
open at most one issue per application, dependency and upgrade step, and must
recognise the issues it opened itself on every later run, open or closed.
"""
from __future__ import annotations

import datetime as dt

from scripts.maintenance_inventory import (
    BOT_LOGIN,
    Finding,
    direct_dependencies,
    existing_keys,
    next_major_release,
    pinned_actions,
    plan_issues,
    render_body,
)

TODAY = dt.date(2026, 10, 18)


def _finding(name: str = "fastmcp", step: str = "3-to-4", target: str = "4.1.0") -> Finding:
    return Finding(
        ecosystem="pypi", name=name, current="3.2.0", target=target, step=step,
        released=dt.date(2026, 9, 1), references=(f"https://pypi.org/project/{name}/",),
    )


def _issue(finding: Finding, state: str = "open", **extra: object) -> dict:
    issue: dict = {"state": state, "body": render_body(finding, TODAY),
                   "user": {"login": BOT_LOGIN}}
    issue.update(extra)
    return issue


def test_first_run_plans_one_issue_per_step() -> None:
    first, again = _finding(), _finding()  # the same step found twice in one run
    other = _finding(name="mcp", step="1-to-2", target="2.0.1")
    assert [f.key for f in plan_issues([first, again, other], [])] == [first.key, other.key]


def test_an_opened_issue_suppresses_the_same_step_on_the_next_run() -> None:
    finding = _finding()
    assert plan_issues([finding], [_issue(finding)]) == []


def test_a_closed_issue_still_suppresses_the_step() -> None:
    """A triaged or rejected assessment is not reopened by a new weekly run."""
    finding = _finding()
    assert plan_issues([finding], [_issue(finding, state="closed")]) == []


def test_a_newer_release_of_the_same_major_is_the_same_step() -> None:
    recorded = _finding(target="4.0.0")
    newer = _finding(target="4.2.0")
    assert plan_issues([newer], [_issue(recorded)]) == []


def test_the_next_step_after_a_recorded_one_is_new() -> None:
    recorded = _finding(step="3-to-4")
    following = _finding(step="4-to-5", target="5.0.0")
    assert plan_issues([following], [_issue(recorded)]) == [following]


def test_pull_requests_and_other_applications_do_not_count() -> None:
    finding = _finding()
    pr = _issue(finding, pull_request={"url": "x"})
    foreign = {"state": "open", "user": {"login": BOT_LOGIN},
               "body": "<!-- maintenance-key: other-app/pypi/fastmcp/3-to-4 -->"}
    assert plan_issues([finding], [pr, foreign, {"state": "open", "body": None}]) == [finding]
    assert existing_keys([foreign]) == {"other-app/pypi/fastmcp/3-to-4"}


def test_body_carries_every_assessment_field() -> None:
    body = render_body(_finding(), TODAY)
    for field in (
        "Current version", "Target version",
        "Official release, migration and support references",
        "Maturity eligibility date and evidence",
        "Breaking changes and compatibility constraints",
        "Potential effects on configuration, runtime, data and integrations",
        "Prerequisite upgrades", "Test and recovery plans",
        "Owner and target release or milestone",
        "Security, adoption and end-of-support deadlines",
    ):
        assert f"**{field}:**" in body, field
    # 2026-09-01 + 60 days = 2026-10-31, adoption 90 days later.
    assert "2026-10-31" in body and "2027-01-29" in body
    assert "Maturity state: **Observed**" in body


def test_next_major_release_takes_one_step_and_its_first_ga_date() -> None:
    releases = {
        "3.9.0": dt.date(2026, 1, 1),
        "4.0.0rc1": dt.date(2026, 5, 1),
        "4.0.0": dt.date(2026, 6, 1),
        "4.10.1": dt.date(2026, 9, 1),
        "4.2.0": dt.date(2026, 7, 1),
        "5.0.0": dt.date(2026, 10, 1),
    }
    assert next_major_release(releases, 3) == ("4.10.1", dt.date(2026, 6, 1))
    assert next_major_release(releases, 5) is None
    assert next_major_release({"v7.1.0": dt.date(2026, 2, 2)}, 6) == (
        "v7.1.0", dt.date(2026, 2, 2))


def test_pinned_actions_needs_a_sha_and_a_major_comment() -> None:
    text = "\n".join((
        "      - uses: actions/checkout@" + "a" * 40 + " # v7",
        "      - uses: actions/checkout@" + "b" * 40 + "  # v6",
        "      - uses: github/codeql-action/init@" + "c" * 40 + " # v4.1.0",
        "      - uses: pypa/gh-action-pypi-publish@" + "d" * 40 + " # release/v1",
        "      - uses: actions/setup-python@v6",
    ))
    assert pinned_actions([text]) == {"actions/checkout": 6, "github/codeql-action": 4}


def test_direct_dependencies_cover_all_extras_and_only_direct_ones() -> None:
    lock = {"package": [
        {"name": "data-olympus", "version": "0.0.0+unreleased",
         "dependencies": [{"name": "mcp"}],
         "optional-dependencies": {"dev": [{"name": "ruff"}]}},
        {"name": "mcp", "version": "1.30.0"},
        {"name": "ruff", "version": "0.17.0"},
        {"name": "anyio", "version": "4.9.0"},
    ]}
    assert direct_dependencies(lock) == {"mcp": "1.30.0", "ruff": "0.17.0"}


def test_a_marker_in_an_issue_by_another_author_does_not_suppress() -> None:
    """On a public repository anyone can paste the marker into an issue."""
    finding = _finding()
    outsider = _issue(finding, user={"login": "someone-else"})
    no_author = _issue(finding, user=None)
    assert plan_issues([finding], [outsider, no_author]) == [finding]
    assert existing_keys([outsider, no_author]) == set()
