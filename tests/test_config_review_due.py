"""Config field for the review-due verification-age threshold (issue #142)."""
from __future__ import annotations

from data_olympus.config import load_config


def test_review_due_after_days_defaults_to_off(monkeypatch) -> None:
    """Feature-off default: no KB_REVIEW_DUE_AFTER_DAYS means no
    verification-age derivation runs, matching the pre-#142 default exactly
    (a corpus that never set this is unaffected by upgrading)."""
    monkeypatch.delenv("KB_REVIEW_DUE_AFTER_DAYS", raising=False)
    cfg = load_config()
    assert cfg.review_due_after_days is None


def test_review_due_after_days_from_env(monkeypatch) -> None:
    monkeypatch.setenv("KB_REVIEW_DUE_AFTER_DAYS", "180")
    cfg = load_config()
    assert cfg.review_due_after_days == 180


def test_review_due_after_days_empty_string_is_off(monkeypatch) -> None:
    monkeypatch.setenv("KB_REVIEW_DUE_AFTER_DAYS", "")
    cfg = load_config()
    assert cfg.review_due_after_days is None
