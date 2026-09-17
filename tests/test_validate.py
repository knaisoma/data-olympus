from pathlib import Path

import pytest

from data_olympus.format.document import Document
from data_olympus.format.validate import (
    compute_freshness,
    is_expired,
    is_in_force,
    is_upcoming,
    normalize_validity_date,
    validate_document,
)


def _doc(tmp_path: Path, name: str, text: str) -> Document:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return Document.load(p)


def test_conformant_document_has_no_errors(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "STD-U-002.md",
        "---\nid: STD-U-002\ntype: standard\nstatus: active\ntier: T1\n"
        "title: Writing Style\ndescription: how to write\ntags: [foundation]\n"
        "timestamp: 2026-06-01\n---\nbody\n",
    )
    errors = [f for f in validate_document(doc) if f.severity == "error"]
    assert errors == []


def test_missing_required_fields_are_errors(tmp_path: Path):
    doc = _doc(tmp_path, "x.md", "---\ntitle: only a title\n---\nbody\n")
    fields = {f.field for f in validate_document(doc) if f.severity == "error"}
    assert {"id", "type", "status", "tier"} <= fields


def test_unknown_enum_values_are_errors(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "x.md",
        "---\nid: X-1\ntype: bogus\nstatus: weird\ntier: T9\n---\nbody\n",
    )
    errs = {f.field for f in validate_document(doc) if f.severity == "error"}
    assert {"type", "status", "tier"} <= errs


def test_missing_recommended_fields_are_warnings(tmp_path: Path):
    doc = _doc(
        tmp_path,
        "x.md",
        "---\nid: X-1\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
    )
    warns = {f.field for f in validate_document(doc) if f.severity == "warning"}
    assert {"title", "description", "tags", "timestamp"} <= warns


_RECOMMENDED_OK = (
    "---\nid: X-1\ntype: standard\nstatus: active\ntier: T1\n"
    "title: t\ndescription: d\ntags: [x]\n"
)


def _warnings_by_field(tmp_path: Path, extra: str) -> dict[str, list[str]]:
    doc = _doc(tmp_path, "x.md", _RECOMMENDED_OK + extra + "---\nbody\n")
    found: dict[str, list[str]] = {}
    for f in validate_document(doc, today="2026-09-11"):
        if f.severity == "warning":
            found.setdefault(f.field, []).append(f.message)
    return found


@pytest.mark.parametrize(
    "extra",
    [
        'generated: { by: human:x, at: "2026-06-20T22:53:05Z" }\n',
        "generated: { by: human:x, at: 2026-06-20T22:53:05Z }\n",
        'timestamp: "2026-01-01"\n',
    ],
    ids=["quoted-generated-at", "unquoted-generated-at", "legacy-timestamp"],
)
def test_a_content_change_time_satisfies_the_recommendation(tmp_path: Path, extra: str):
    found = _warnings_by_field(tmp_path, extra)
    assert "timestamp" not in found
    assert "generated" not in found


def test_no_content_change_time_warns_on_the_timestamp_field(tmp_path: Path):
    found = _warnings_by_field(tmp_path, "")
    assert "timestamp" in found
    message = found["timestamp"][0]
    assert "generated.at" in message and "timestamp" in message


@pytest.mark.parametrize(
    "extra",
    [
        "generated: null\n",
        "generated: yesterday\n",
        "generated: { by: x, at: 2026-06-20T22:53:05 }\n",
        "generated: { by: x, at: 2026-06-20 }\n",
        "generated: { by: x, at: 5 }\n",
        'generated: { by: x, at: "" }\n',
        "generated: { by: x, at: null }\n",
    ],
    ids=["null", "scalar", "naive-datetime", "date", "number", "blank-at", "null-at"],
)
def test_malformed_generated_warns_and_does_not_satisfy(tmp_path: Path, extra: str):
    found = _warnings_by_field(tmp_path, extra)
    assert "generated" in found
    assert "timestamp" in found


@pytest.mark.parametrize(
    "extra",
    [
        'generated: { at: "2026-06-20T22:53:05Z" }\n',
        'generated: { by: "", at: "2026-06-20T22:53:05Z" }\n',
    ],
    ids=["missing-by", "empty-by"],
)
def test_a_bad_by_warns_but_a_valid_at_still_satisfies(tmp_path: Path, extra: str):
    found = _warnings_by_field(tmp_path, extra)
    assert "generated" in found
    assert "timestamp" not in found


def test_reserved_files_are_exempt(tmp_path: Path):
    doc = _doc(tmp_path, "index.md", "# Index\n\n* [a](a.md)\n")
    assert validate_document(doc) == []


def test_zero_valued_required_field_is_not_missing(tmp_path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: 0\ntype: standard\nstatus: active\ntier: T1\n---\nbody\n",
    )
    missing = [
        f for f in validate_document(doc)
        if f.message.startswith("missing required field 'id'")
    ]
    assert missing == []


def test_adr_accepted_status_is_valid(tmp_path):
    doc = _doc(
        tmp_path, "DEC-021.md",
        "---\nid: DEC-021\ntype: decision\nstatus: accepted\ntier: meta\n---\nbody\n",
    )
    status_errs = [
        f for f in validate_document(doc)
        if f.field == "status" and f.severity == "error"
    ]
    assert status_errs == []


def test_tags_as_string_is_a_warning(tmp_path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\ntags: foundation\n---\nbody\n",
    )
    tag_warns = [
        f for f in validate_document(doc)
        if f.field == "tags" and f.severity == "warning"
    ]
    assert tag_warns and "should be a list" in tag_warns[0].message


# ---------------------------------------------------------------------------
# Validity / freshness predicates (issue #107)
# ---------------------------------------------------------------------------


def test_is_expired_true_when_valid_until_in_past():
    assert is_expired("2026-01-01", today="2026-01-02") is True


def test_is_expired_false_on_boundary_day():
    # valid_until == today is still in force (inclusive boundary).
    assert is_expired("2026-01-02", today="2026-01-02") is False


def test_is_expired_false_when_absent():
    assert is_expired(None, today="2026-01-02") is False
    assert is_expired("", today="2026-01-02") is False


def test_is_upcoming_true_when_valid_from_in_future():
    assert is_upcoming("2026-02-01", today="2026-01-02") is True


def test_is_upcoming_false_on_boundary_day():
    assert is_upcoming("2026-01-02", today="2026-01-02") is False


def test_is_upcoming_false_when_absent():
    assert is_upcoming(None, today="2026-01-02") is False


def test_is_in_force_requires_status_class_and_window():
    # active + no window -> in force.
    assert is_in_force("active", None, None, today="2026-01-02") is True
    # active + expired window -> not in force.
    assert is_in_force("active", None, "2026-01-01", today="2026-01-02") is False
    # active + upcoming window -> not in force.
    assert is_in_force("active", "2026-02-01", None, today="2026-01-02") is False
    # superseded (not in the status class) -> not in force even with a clean window.
    assert is_in_force("superseded", None, None, today="2026-01-02") is False


def test_is_in_force_boundary_days_are_inclusive():
    assert is_in_force("active", "2026-01-02", "2026-01-02", today="2026-01-02") is True


def test_normalize_validity_date_accepts_date_string():
    normalized, malformed = normalize_validity_date("2026-06-01")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_datetime_string_with_tz():
    normalized, malformed = normalize_validity_date("2026-06-01T12:00:00+02:00")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_zulu_suffix():
    normalized, malformed = normalize_validity_date("2026-06-01T00:00:00Z")
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_python_date_object():
    import datetime
    normalized, malformed = normalize_validity_date(datetime.date(2026, 6, 1))
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_accepts_python_datetime_object():
    import datetime
    normalized, malformed = normalize_validity_date(datetime.datetime(2026, 6, 1, 8, 30))
    assert normalized == "2026-06-01"
    assert malformed is False


def test_normalize_validity_date_none_is_absent_not_malformed():
    normalized, malformed = normalize_validity_date(None)
    assert normalized == ""
    assert malformed is False


def test_normalize_validity_date_garbage_is_malformed():
    normalized, malformed = normalize_validity_date("not-a-date")
    assert normalized == ""
    assert malformed is True


def test_normalize_validity_date_wrong_type_is_malformed():
    normalized, malformed = normalize_validity_date(12345)
    assert normalized == ""
    assert malformed is True


# ---------------------------------------------------------------------------
# kb lint validity warnings (issue #107): always warnings, never errors.
# ---------------------------------------------------------------------------


def test_lint_warns_on_recheck_by_in_the_past(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  recheck_by: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("recheck_by" in f.message for f in warns)
    assert all(f.severity == "warning" for f in findings if f.field == "validity")


def test_lint_warns_on_expired_but_active_status(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("valid_until" in f.message for f in warns)
    assert all(f.severity == "warning" for f in findings if f.field == "validity")


def test_lint_no_warning_when_expired_and_status_not_in_force(tmp_path: Path):
    # Only "expired but ACTIVE" is a warning; a properly superseded doc with an
    # old valid_until is not flagged (its expiry is not surprising).
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: superseded\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.field == "validity" and "valid_until" in f.message]
    assert warns == []


def test_lint_warns_on_malformed_validity_value(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: not-a-date\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    warns = [f for f in findings if f.severity == "warning" and f.field == "validity"]
    assert any("malformed" in f.message for f in warns)
    assert all(f.severity != "error" for f in findings)


def test_lint_no_validity_findings_when_validity_absent(tmp_path: Path):
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    assert [f for f in findings if f.field == "validity"] == []


def test_lint_validity_findings_are_never_errors(tmp_path: Path):
    """Wall-clock-based checks must never block CI (they would flake with time)."""
    doc = _doc(
        tmp_path, "x.md",
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        "validity:\n  valid_until: 2000-01-01\n  recheck_by: bogus\n---\nbody\n",
    )
    findings = validate_document(doc, today="2026-06-01")
    assert all(f.severity != "error" for f in findings)


# ---------------------------------------------------------------------------
# Memory-inbox in-force floor (issue #109): a doc under the memory-inbox
# prefix is never in force, regardless of claimed status. Single-sourced
# inside is_in_force via the `is_inbox` keyword, NOT a forked predicate.
# ---------------------------------------------------------------------------


def test_is_in_force_inbox_floor_overrides_active_status():
    from data_olympus.format.validate import is_in_force
    # Without the inbox floor this would be in force (active, clean window).
    assert is_in_force("active", None, None, today="2026-01-02") is True
    # The SAME status/window, but flagged is_inbox=True, is never in force.
    assert is_in_force(
        "active", None, None, today="2026-01-02", is_inbox=True,
    ) is False


def test_is_in_force_inbox_floor_wins_even_with_clean_validity_window():
    from data_olympus.format.validate import is_in_force
    assert is_in_force(
        "accepted", "2026-01-01", "2026-12-31", today="2026-01-02", is_inbox=True,
    ) is False


def test_is_in_force_default_is_inbox_false_is_backward_compatible():
    from data_olympus.format.validate import is_in_force
    assert is_in_force("active", None, None, today="2026-01-02") == (
        is_in_force("active", None, None, today="2026-01-02", is_inbox=False)
    )


def test_memory_inbox_prefix_default():
    from data_olympus.format.validate import memory_inbox_prefix
    assert memory_inbox_prefix() == "memory/inbox/"


def test_memory_inbox_prefix_override(monkeypatch):
    from data_olympus.format.validate import memory_inbox_prefix
    monkeypatch.setenv("KB_MEMORY_INBOX_PREFIX", "operator/memory/inbox")
    # Trailing slash normalized in even when the override omits it.
    assert memory_inbox_prefix() == "operator/memory/inbox/"


def test_is_inbox_path_matches_default_prefix():
    from data_olympus.format.validate import is_inbox_path
    assert is_inbox_path("memory/inbox/2026-06-01-x.md") is True
    assert is_inbox_path("memory/accepted/x.md") is False
    assert is_inbox_path("universal/foundation/x.md") is False


def test_is_inbox_path_normalizes_backslashes():
    from data_olympus.format.validate import is_inbox_path
    assert is_inbox_path("memory\\inbox\\x.md") is True


def test_is_inbox_path_respects_prefix_override(monkeypatch):
    from data_olympus.format.validate import is_inbox_path
    monkeypatch.setenv("KB_MEMORY_INBOX_PREFIX", "operator/memory/inbox/")
    assert is_inbox_path("operator/memory/inbox/x.md") is True
    # The default prefix no longer applies once overridden.
    assert is_inbox_path("memory/inbox/x.md") is False


def test_not_inbox_sql_fragment_is_a_static_no_param_condition():
    from data_olympus.format.validate import not_inbox_sql_fragment
    assert not_inbox_sql_fragment() == "docs.is_inbox = 0"


# ---------------------------------------------------------------------------
# compute_freshness (issue #142): review-due derived from verification age,
# recheck_by as an override, an explicit no-last_verified rule.
# ---------------------------------------------------------------------------

def test_compute_freshness_fresh_returns_none_state_and_reason():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="2026-01-01", today="2026-01-15",
        review_due_after_days=90,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_expired_beats_recheck_by_and_verification_age():
    state, reason = compute_freshness(
        valid_from=None, valid_until="2026-01-01", recheck_by="2026-01-01",
        last_verified="2020-01-01", today="2026-02-01",
        review_due_after_days=90,
    )
    assert state == "expired"
    assert "valid_until" in reason
    assert "2026-01-01" in reason


def test_compute_freshness_upcoming_beats_stale():
    state, reason = compute_freshness(
        valid_from="2026-03-01", valid_until=None, recheck_by="2026-01-01",
        last_verified=None, today="2026-02-01",
        review_due_after_days=90,
    )
    assert state == "upcoming"
    assert "valid_from" in reason


def test_compute_freshness_recheck_by_in_past_is_stale_with_reason():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by="2026-01-01",
        last_verified="2026-01-10", today="2026-02-01",
        review_due_after_days=90,
    )
    assert state == "stale"
    assert "recheck_by" in reason
    assert "2026-01-01" in reason


def test_compute_freshness_explicit_future_recheck_by_overrides_age_derivation():
    """An operator-set recheck_by, even far in the future, is an explicit
    decision that overrides the automatic verification-age check -- a
    long-unverified doc with a deliberately deferred recheck_by is NOT
    reported stale before that date."""
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by="2027-01-01",
        last_verified="2020-01-01", today="2026-02-01",
        review_due_after_days=30,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_no_recheck_by_derives_stale_from_verification_age():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="2025-01-01", today="2026-01-01",
        review_due_after_days=90,
    )
    assert state == "stale"
    assert "last_verified" in reason
    assert "2025-01-01" in reason
    assert "365" in reason  # exact day count, not just a boolean


def test_compute_freshness_no_recheck_by_recent_verification_is_fresh():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="2026-01-10", today="2026-02-01",
        review_due_after_days=90,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_no_last_verified_at_all_is_stale_not_fresh():
    """The explicit no-last_verified rule (#142): a document nobody has ever
    verified must NOT default to fresh -- that would hide exactly the
    documents most worth looking at."""
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified=None, today="2026-02-01",
        review_due_after_days=90,
    )
    assert state == "stale"
    assert "last_verified" in reason
    assert "not set" in reason


def test_compute_freshness_review_due_after_days_none_disables_age_derivation():
    """review_due_after_days=None is the feature-off state (matches the
    pre-#142 default exactly): no last_verified age check runs at all, and a
    document with no last_verified is fresh, as it always was."""
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified=None, today="2026-02-01",
        review_due_after_days=None,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_review_due_after_days_zero_also_disables_derivation():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified=None, today="2026-02-01",
        review_due_after_days=0,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_verification_age_exactly_at_threshold_is_fresh():
    """The boundary day is inclusive-fresh, matching is_expired/is_upcoming's
    existing boundary convention elsewhere in this module."""
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="2026-01-01", today="2026-04-01",  # exactly 90 days
        review_due_after_days=90,
    )
    assert state is None
    assert reason is None


def test_compute_freshness_verification_age_one_day_past_threshold_is_stale():
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="2025-12-31", today="2026-04-01",  # 91 days
        review_due_after_days=90,
    )
    assert state == "stale"


def test_compute_freshness_malformed_last_verified_does_not_crash():
    """A malformed date must not raise -- the corpus can carry hand-authored
    dates, and a search request must not 500 on one bad value."""
    state, reason = compute_freshness(
        valid_from=None, valid_until=None, recheck_by=None,
        last_verified="not-a-date", today="2026-02-01",
        review_due_after_days=90,
    )
    assert state in (None, "stale")  # either is defensible; must not raise
