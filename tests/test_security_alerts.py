from __future__ import annotations

from scripts.security_alerts import evaluate


def test_evaluate_clean_when_no_open_alerts() -> None:
    code, report = evaluate([], [])
    assert code == 0
    assert "clear" in report.lower()


def test_evaluate_flags_open_dependabot() -> None:
    dep = [{
        "number": 7, "state": "open",
        "security_advisory": {"severity": "high"},
        "dependency": {"package": {"name": "requests"}},
        "html_url": "https://example/7",
    }]
    code, report = evaluate(dep, [])
    assert code == 5
    assert "requests" in report and "#7" in report and "high" in report.lower()


def test_evaluate_flags_open_codeql() -> None:
    cq = [{
        "number": 3, "state": "open",
        "rule": {"severity": "error", "description": "SQL injection"},
        "html_url": "https://example/3",
    }]
    code, report = evaluate([], cq)
    assert code == 5
    assert "SQL injection" in report and "#3" in report


def test_evaluate_ignores_non_open_states() -> None:
    dep = [{"number": 1, "state": "dismissed", "security_advisory": {}, "dependency": {}}]
    cq = [{"number": 2, "state": "fixed", "rule": {}}]
    expected_msg = "security clear: 0 open Dependabot alerts, 0 open CodeQL alerts"
    assert evaluate(dep, cq) == (0, expected_msg)
