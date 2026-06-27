"""Word-boundary keyword matching and command-pattern signals."""
from __future__ import annotations

from data_olympus.enforce_policy import IntentClassifier


def test_substring_false_positive_is_gone() -> None:
    c = IntentClassifier()
    # "authored" must NOT match the "auth" keyword; "standardize" must NOT match "standard"
    assert c.classify(intent="I authored a standardized memo").is_governed_decision is False


def test_real_keyword_still_matches() -> None:
    c = IntentClassifier()
    assert c.classify(intent="add a new auth library").is_governed_decision is True


def test_command_pattern_in_diff_is_governed() -> None:
    c = IntentClassifier()
    r = c.classify(action_diff="pip install requests && echo done")
    assert r.is_governed_decision is True
    assert any(s.startswith("command:") for s in r.signals)


def test_plain_command_not_governed() -> None:
    c = IntentClassifier()
    assert c.classify(action_diff="ls -la && cat README.md").is_governed_decision is False
