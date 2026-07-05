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


def test_keyword_in_action_diff_is_not_governed_when_path_is_known() -> None:
    """A Write/Edit to a concrete file (action_path present) is classified by
    the path-glob check, not by free-text keywords in its content. A scratch
    write whose text merely mentions a keyword (RLS, auth, tenant, ...) as
    prose, to a file the path globs don't consider governed, must not trip
    the gate - this was blocked regardless of the actual write target before
    the fix."""
    r = IntentClassifier().classify(
        action_path="/tmp/scratch/bisect-test.txt",
        action_diff="RLS tenant boundaries auth trust model",
    )
    assert r.is_governed_decision is False
    assert r.signals == []


def test_keyword_in_action_diff_still_governs_when_path_is_unknown() -> None:
    """Bash commands and OpenCode's `patch` tool send no action_path at all -
    action_diff is the only content available - so the free-text keyword net
    still applies there; dropping it would silently lose the only signal for
    those two tool shapes."""
    r = IntentClassifier().classify(
        action_diff="RLS tenant boundaries auth trust model"
    )
    assert r.is_governed_decision is True
    assert any(s.startswith("keyword:") for s in r.signals)


def test_keyword_in_intent_still_governs_regardless_of_action_diff() -> None:
    r = IntentClassifier().classify(
        intent="add a new auth library", action_diff="ls -la"
    )
    assert r.is_governed_decision is True
    assert any(s.startswith("keyword:") for s in r.signals)
