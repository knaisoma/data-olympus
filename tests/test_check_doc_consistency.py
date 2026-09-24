"""Tests for the doc-consistency CI guard (scripts/check_doc_consistency.py).

Covers both the pure extraction/diff logic (no filesystem) and an
end-to-end pass/fail run against a scratch root with real SPEC.md /
docs/adoption.md text, so a regression in the sentence-boundary regex or the
diffing logic is caught even if the two never disagree in practice.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.check_doc_consistency import (
    _ENV_DEFAULT_FIELDS,
    ParseError,
    _check_env_defaults,
    _diff_message,
    _extract_enum_occurrences,
    _extract_env_defaults,
    _extract_reserved,
    check_doc_consistency,
)

# --- _extract_enum_occurrences -----------------------------------------------


def test_extract_enum_single_line() -> None:
    text = "- `type`: one of `standard`, `decision`, `workflow`.\nMore prose."
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 1
    line_no, values = occurrences[0]
    assert line_no == 1
    assert values == {"standard", "decision", "workflow"}


def test_extract_enum_wraps_across_lines() -> None:
    text = (
        "- `type`: one of `standard`, `decision`, `workflow`, `project`,\n"
        "  `memory`, `reference`.\n"
    )
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 1
    _, values = occurrences[0]
    assert values == {"standard", "decision", "workflow", "project", "memory", "reference"}


def test_extract_enum_tolerates_oxford_or() -> None:
    text = "`status`: one of `draft`, `active`, or `deprecated`.\n"
    _, values = _extract_enum_occurrences(text, field="status")[0]
    assert values == {"draft", "active", "deprecated"}


def test_extract_enum_does_not_stop_at_period_inside_backticks() -> None:
    # A period embedded in a backtick-quoted value (e.g. a filename) must not
    # be mistaken for the sentence-ending period.
    text = "`type`: one of `index.md`, `standard`. Trailing prose.\n"
    _, values = _extract_enum_occurrences(text, field="type")[0]
    assert values == {"index.md", "standard"}


def test_extract_enum_finds_multiple_occurrences() -> None:
    text = (
        "- `type`: controlled vocabulary: `standard`, `decision`.\n"
        "...\n"
        "   - `type`: one of `standard`, `decision`\n"
    )
    # Second occurrence has no terminating period at all before EOF; give it
    # one so both occurrences parse (mirrors the real SPEC.md fix).
    text = text.rstrip("\n") + ".\n"
    occurrences = _extract_enum_occurrences(text, field="type")
    assert len(occurrences) == 2
    assert occurrences[0][1] == {"standard", "decision"}
    assert occurrences[1][1] == {"standard", "decision"}


def test_extract_enum_raises_when_marker_absent() -> None:
    with pytest.raises(ParseError, match="no '`type`:' marker found"):
        _extract_enum_occurrences("nothing relevant here", field="type")


def test_extract_enum_raises_when_no_values_follow() -> None:
    with pytest.raises(ParseError, match="no backtick-quoted values followed"):
        _extract_enum_occurrences("`type`: nothing quoted here.\n", field="type")


def test_extract_enum_raises_when_no_sentence_end() -> None:
    with pytest.raises(ParseError, match="no sentence-ending"):
        _extract_enum_occurrences("`type`: one of `standard`, `decision`", field="type")


# --- _extract_reserved --------------------------------------------------------


def test_extract_reserved() -> None:
    text = (
        "**Reserved filenames.** The filenames `index.md`, `log.md`, and "
        "`template.md` are reserved in every directory. More prose.\n"
    )
    line_no, values = _extract_reserved(text)
    assert line_no == 1
    assert values == {"index.md", "log.md", "template.md"}


def test_extract_reserved_raises_when_sentence_absent() -> None:
    with pytest.raises(ParseError, match="no 'Reserved filenames.' sentence found"):
        _extract_reserved("no mention of reserved anything here.\n")


# --- _diff_message -------------------------------------------------------------


def test_diff_message_none_when_in_sync() -> None:
    msg = _diff_message(
        source="SPEC.md", field="type", line_no=1,
        extracted={"a", "b"}, canonical={"a", "b"},
    )
    assert msg is None


def test_diff_message_reports_missing_and_extra() -> None:
    msg = _diff_message(
        source="SPEC.md", field="type", line_no=42,
        extracted={"a", "c"}, canonical={"a", "b"},
    )
    assert msg is not None
    assert "SPEC.md:42" in msg
    assert "'b'" in msg  # missing from doc
    assert "'c'" in msg  # stale in doc


# --- check_doc_consistency (end-to-end against a scratch root) --------------


_IN_SYNC_TIER_LINE = "`tier`: one of `T1`, `T2`, `T3`, `T4`, `meta`."


def _write_bundle(
    tmp_path: Path,
    *,
    spec_type_line: str,
    adoption_type_line: str,
    spec_tier_line: str = _IN_SYNC_TIER_LINE,
    adoption_tier_line: str = _IN_SYNC_TIER_LINE,
) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "SPEC.md").write_text(
        "## 4.2 Governance extensions\n\n"
        f"- {spec_type_line}\n"
        "- `status`: lifecycle state: `draft`, `active`, `deprecated`, `superseded`, "
        "`proposed`, `accepted`, `rejected`. More prose.\n"
        f"- {spec_tier_line}\n\n"
        "**Reserved filenames.** The filenames `index.md`, `log.md`, and "
        "`template.md` are reserved in every directory. More prose.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "adoption.md").write_text(
        f"- {adoption_type_line}\n"
        "- `status`: one of `draft`, `active`, `deprecated`, `superseded`,\n"
        "  `proposed`, `accepted`, `rejected`.\n"
        f"- {adoption_tier_line}\n",
        encoding="utf-8",
    )
    return tmp_path


_IN_SYNC_TYPE_LINE = (
    "`type`: controlled vocabulary: `standard`, `decision`, `workflow`, "
    "`project`, `memory`, `reference`. Unknown values are an error."
)


def test_check_doc_consistency_passes_when_in_sync(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        spec_type_line=_IN_SYNC_TYPE_LINE,
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`memory`, `reference`."
        ),
    )
    assert check_doc_consistency(tmp_path) == []


def test_check_doc_consistency_detects_drift_in_spec(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        # Drops 'reference', adds a stale 'obsolete-type'.
        spec_type_line=(
            "`type`: controlled vocabulary: `standard`, `decision`, `workflow`, "
            "`project`, `memory`, `obsolete-type`. Unknown values are an error."
        ),
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`memory`, `reference`."
        ),
    )
    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "SPEC.md" in errors[0]
    assert "'reference'" in errors[0]
    assert "'obsolete-type'" in errors[0]


def test_check_doc_consistency_detects_drift_in_adoption_doc(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        spec_type_line=_IN_SYNC_TYPE_LINE,
        # adoption.md forgot to add 'memory' when it was introduced.
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`reference`."
        ),
    )
    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "docs/adoption.md" in errors[0]
    assert "'memory'" in errors[0]


def test_check_doc_consistency_detects_tier_drift_in_spec(tmp_path: Path) -> None:
    _write_bundle(
        tmp_path,
        spec_type_line=_IN_SYNC_TYPE_LINE,
        adoption_type_line=(
            "`type`: one of `standard`, `decision`, `workflow`, `project`, "
            "`memory`, `reference`."
        ),
        # Drops 'meta' from the tier enum restatement.
        spec_tier_line="`tier`: one of `T1`, `T2`, `T3`, `T4`.",
    )
    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "SPEC.md" in errors[0]
    assert "'tier'" in errors[0]
    assert "'meta'" in errors[0]


def test_check_doc_consistency_detects_reserved_drift(tmp_path: Path) -> None:
    _write_bundle(tmp_path, spec_type_line=_IN_SYNC_TYPE_LINE, adoption_type_line=(
        "`type`: one of `standard`, `decision`, `workflow`, `project`, "
        "`memory`, `reference`."
    ))
    spec_path = tmp_path / "SPEC.md"
    text = spec_path.read_text(encoding="utf-8")
    # Drop template.md from the reserved-filename sentence.
    text = text.replace(
        "The filenames `index.md`, `log.md`, and `template.md` are reserved",
        "The filenames `index.md`, `log.md` are reserved",
    )
    spec_path.write_text(text, encoding="utf-8")

    errors = check_doc_consistency(tmp_path)
    assert len(errors) == 1
    assert "RESERVED" in errors[0]
    assert "'template.md'" in errors[0]


def test_check_doc_consistency_reports_parse_error_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "SPEC.md").write_text("nothing relevant in this file.\n", encoding="utf-8")
    (tmp_path / "docs" / "adoption.md").write_text("also nothing relevant.\n", encoding="utf-8")

    errors = check_doc_consistency(tmp_path)
    # Every check should fail to parse (both files, both fields, plus
    # reserved), but the function must return messages, not raise.
    assert len(errors) >= 4
    assert all("no '" in e or "marker found" in e or "sentence found" in e for e in errors)


def test_check_doc_consistency_real_repo_docs_are_in_sync() -> None:
    """The actual SPEC.md / docs/adoption.md in this repo must pass today.

    This is the guard's own dogfood check: if this test fails, either the
    real docs have drifted (fix the docs) or the parser broke on real
    formatting (fix the parser) — either way CI should have caught it.
    """
    repo_root = Path(__file__).resolve().parent.parent
    assert check_doc_consistency(repo_root) == []


# --- environment-variable defaults (issue #286) ------------------------------


def _config_default(field: str) -> int:
    """The canonical default for a ``Config`` field.

    ``Config`` is a ``slots=True`` dataclass, so ``Config.<field>`` is the slot
    descriptor rather than the default value; the default lives in the field
    metadata. The guard reads it the same way, and these tests must not
    hard-code the numbers or they stop tracking the code.
    """
    from dataclasses import fields as dataclass_fields

    from data_olympus.config import Config

    value = next(f.default for f in dataclass_fields(Config) if f.name == field)
    assert isinstance(value, int)
    return value


_IDLE = _config_default("session_idle_timeout_sec")
_REAP = _config_default("session_reap_interval_sec")
_TOUCH = _config_default("session_touch_interval_sec")



def test_extract_env_defaults_single_occurrence() -> None:
    text = "`KB_SESSION_REAP_INTERVAL_SEC` (default `60`). Next sentence.\n"
    assert _extract_env_defaults(text, name="KB_SESSION_REAP_INTERVAL_SEC") == [(1, 60)]


def test_extract_env_defaults_wraps_between_default_and_value() -> None:
    # serving.md's actual wrap: the line breaks after the word "default".
    text = "and `KB_SESSION_IDLE_TIMEOUT_SEC` (default\n  `300`) terminates a session.\n"
    assert _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC") == [(1, 300)]


def test_extract_env_defaults_wraps_between_variable_and_parenthetical() -> None:
    # The other place the line can break: after the variable itself.
    text = "and `KB_SESSION_IDLE_TIMEOUT_SEC`\n  (default `300`) terminates a session.\n"
    assert _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC") == [(1, 300)]


def test_extract_env_defaults_accepts_an_unquoted_number() -> None:
    text = "`KB_SESSION_REAP_INTERVAL_SEC` (default 60). More prose.\n"
    assert _extract_env_defaults(text, name="KB_SESSION_REAP_INTERVAL_SEC") == [(1, 60)]


def test_extract_env_defaults_finds_every_restatement() -> None:
    # The whole point of #286: one knob stated twice, differently.
    text = (
        "top: `KB_SESSION_IDLE_TIMEOUT_SEC` (default `300`).\n"
        "reference: `KB_SESSION_IDLE_TIMEOUT_SEC` (default 1800).\n"
    )
    assert _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC") == [
        (1, 300), (2, 1800),
    ]


def test_extract_env_defaults_does_not_cross_a_sentence_boundary() -> None:
    # A mention with no default of its own must not pick up the next
    # sentence's default for a different knob.
    text = (
        "Set `KB_SESSION_IDLE_TIMEOUT_SEC=0` to disable reaping. "
        "`KB_SESSION_REAP_INTERVAL_SEC` (default `60`) sets the scan period.\n"
    )
    with pytest.raises(ParseError):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC=0")
    assert _extract_env_defaults(text, name="KB_SESSION_REAP_INTERVAL_SEC") == [(1, 60)]


def test_extract_env_defaults_raises_when_no_default_is_stated() -> None:
    text = "`KB_SESSION_IDLE_TIMEOUT_SEC` is mentioned but never given a default.\n"
    with pytest.raises(ParseError):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_check_env_defaults_detects_a_stale_documented_default(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    idle = _IDLE
    (docs / "serving.md").write_text(
        f"- `KB_SESSION_IDLE_TIMEOUT_SEC` (default `{idle}`) bounds an idle session.\n"
        f"- `KB_SESSION_REAP_INTERVAL_SEC` (default `{_REAP}`).\n"
        f"- `KB_SESSION_TOUCH_INTERVAL_SEC` (default `{_TOUCH}`).\n"
        # The #286 shape: a second restatement that was never updated.
        f"- reference: `KB_SESSION_IDLE_TIMEOUT_SEC` (default {idle * 6}).\n",
        encoding="utf-8",
    )

    errors = _check_env_defaults(tmp_path)
    assert len(errors) == 1
    assert "line 4" in errors[0]
    assert "KB_SESSION_IDLE_TIMEOUT_SEC" in errors[0]
    assert str(idle * 6) in errors[0]
    assert str(idle) in errors[0]


def test_check_env_defaults_passes_when_every_statement_matches(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "serving.md").write_text(
        f"- `KB_SESSION_IDLE_TIMEOUT_SEC` (default `{_IDLE}`).\n"
        f"- `KB_SESSION_REAP_INTERVAL_SEC` (default `{_REAP}`).\n"
        f"- `KB_SESSION_TOUCH_INTERVAL_SEC` (default `{_TOUCH}`).\n",
        encoding="utf-8",
    )
    assert _check_env_defaults(tmp_path) == []


def test_check_env_defaults_reports_a_variable_that_lost_its_default(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "serving.md").write_text(
        f"- `KB_SESSION_IDLE_TIMEOUT_SEC` (default `{_IDLE}`).\n"
        f"- `KB_SESSION_REAP_INTERVAL_SEC` (default `{_REAP}`).\n"
        "- `KB_SESSION_TOUCH_INTERVAL_SEC` is described without a default.\n",
        encoding="utf-8",
    )
    errors = _check_env_defaults(tmp_path)
    assert len(errors) == 1
    assert "KB_SESSION_TOUCH_INTERVAL_SEC" in errors[0]


def test_check_env_defaults_is_silent_without_a_serving_doc(tmp_path: Path) -> None:
    # The scratch roots used by the enum tests have no docs/serving.md; the
    # env-default check must not turn those into failures. The real-repo test
    # below is what stops this tolerance from making the check vacuous.
    assert _check_env_defaults(tmp_path) == []


def test_real_repo_serving_doc_states_every_guarded_default() -> None:
    """Every variable in _ENV_DEFAULT_FIELDS is genuinely checked against the
    real docs/serving.md, so a rename or a reshaped sentence fails here rather
    than silently checking nothing."""
    repo_root = Path(__file__).resolve().parent.parent
    text = (repo_root / "docs" / "serving.md").read_text(encoding="utf-8")
    for name, _field in _ENV_DEFAULT_FIELDS:
        stated = _extract_env_defaults(text, name=name)
        assert stated, name
    assert _check_env_defaults(repo_root) == []


# --- regressions from review round 3 -----------------------------------------


def test_extract_env_defaults_refuses_a_value_carrying_a_unit() -> None:
    # The first version read the leading digits, so "(default 300 minutes)"
    # certified a 300-SECOND config as correctly documented.
    text = "`KB_SESSION_IDLE_TIMEOUT_SEC` (default 300 minutes).\n"
    with pytest.raises(ParseError, match="whole number"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_refuses_a_non_integer_value() -> None:
    text = "`KB_SESSION_IDLE_TIMEOUT_SEC` (default 300.5).\n"
    with pytest.raises(ParseError, match="whole number"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_refuses_the_stale_reference_wording() -> None:
    # The exact string this issue was about.
    text = "`KB_SESSION_IDLE_TIMEOUT_SEC` (default 1800s / 30 min).\n"
    with pytest.raises(ParseError, match="whole number"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_does_not_borrow_a_neighbours_default() -> None:
    # A bare mention beside another variable's declaration used to report that
    # other variable's number as its own.
    text = (
        "`KB_SESSION_IDLE_TIMEOUT_SEC` and `KB_SESSION_REAP_INTERVAL_SEC` "
        "(default 60) both matter.\n"
    )
    with pytest.raises(ParseError, match="no .* default stated"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")
    assert _extract_env_defaults(text, name="KB_SESSION_REAP_INTERVAL_SEC") == [(1, 60)]


def test_extract_env_defaults_does_not_cross_a_question_mark() -> None:
    text = "Is `KB_SESSION_IDLE_TIMEOUT_SEC` set? Something else (default 99).\n"
    with pytest.raises(ParseError, match="no .* default stated"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_does_not_cross_a_paragraph_break() -> None:
    text = (
        "`KB_SESSION_IDLE_TIMEOUT_SEC` is described here.\n"
        "\n"
        "Unrelated paragraph (default 99).\n"
    )
    with pytest.raises(ParseError, match="no .* default stated"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_ignores_fenced_examples() -> None:
    # An example is an illustration, not the document's statement of the
    # default, and must not satisfy the required-default check on its own.
    text = (
        "```bash\n"
        "`KB_SESSION_IDLE_TIMEOUT_SEC` (default 7)\n"
        "```\n"
        "Prose that states nothing.\n"
    )
    with pytest.raises(ParseError, match="no .* default stated"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")


def test_extract_env_defaults_keeps_line_numbers_after_a_fence() -> None:
    text = (
        "```\n"
        "example\n"
        "```\n"
        "`KB_SESSION_REAP_INTERVAL_SEC` (default `60`).\n"
    )
    assert _extract_env_defaults(text, name="KB_SESSION_REAP_INTERVAL_SEC") == [(4, 60)]


def test_check_env_defaults_surfaces_an_unreadable_value_as_an_error(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "serving.md").write_text(
        f"- `KB_SESSION_IDLE_TIMEOUT_SEC` (default `{_IDLE}`).\n"
        f"- `KB_SESSION_REAP_INTERVAL_SEC` (default `{_REAP}`).\n"
        f"- `KB_SESSION_TOUCH_INTERVAL_SEC` (default {_TOUCH} seconds).\n",
        encoding="utf-8",
    )
    errors = _check_env_defaults(tmp_path)
    assert len(errors) == 1
    assert "KB_SESSION_TOUCH_INTERVAL_SEC" in errors[0]
    assert "whole number" in errors[0]


def test_extract_env_defaults_does_not_span_a_blank_line() -> None:
    # One wrap is prose; a blank line is a new block and a different subject.
    text = "`KB_SESSION_IDLE_TIMEOUT_SEC`\n\n(default 9).\n"
    with pytest.raises(ParseError, match="no .* default stated"):
        _extract_env_defaults(text, name="KB_SESSION_IDLE_TIMEOUT_SEC")
