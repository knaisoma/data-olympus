"""Tests for the heuristic intent classifier."""
from __future__ import annotations

from data_olympus.enforce_policy import IntentClassifier


def test_keyword_in_intent_is_governed() -> None:
    c = IntentClassifier()
    r = c.classify(intent="should we add a new logging library here?")
    assert r.is_governed_decision is True
    assert any(s.startswith("keyword:") for s in r.signals)


def test_plain_chat_is_not_governed() -> None:
    c = IntentClassifier()
    r = c.classify(intent="what time is it?")
    assert r.is_governed_decision is False
    assert r.signals == []


def test_dependency_manifest_path_is_governed() -> None:
    c = IntentClassifier()
    r = c.classify(action_path="/home/me/proj/pyproject.toml")
    assert r.is_governed_decision is True
    assert any(s.startswith("path:") for s in r.signals)


def test_ordinary_source_edit_is_not_governed_by_path_alone() -> None:
    c = IntentClassifier()
    r = c.classify(action_path="/home/me/proj/src/util/strings.py")
    assert r.is_governed_decision is False
