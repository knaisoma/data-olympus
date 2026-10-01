"""Writing rules on write-pipeline postimages: the scan itself (#283).

Only lines a write ADDS are checked, so an edit never fails on text it did not
write. "Added" is a count difference: a line counts as added when it occurs more
often in the postimage than in the preimage, both folded through the vendored
linter's `normalise`. Only the raw tier (em-dash, en-dash used as one, agent
authorship credit) applies, on raw lines, honouring the allow marker.
"""
from __future__ import annotations

import pytest

from data_olympus.writing_rules import (
    WritingRulesPolicy,
    added_line_findings,
    parse_mode,
)

DASH = "—"


def test_a_pre_existing_offending_line_is_not_a_finding() -> None:
    pre = f"# Title\n\nOld line {DASH} kept.\n"
    post = pre + "A clean new line.\n"
    assert added_line_findings(preimage=pre, postimage=post) == []


def test_an_added_offending_line_is_named_with_its_line_and_rule() -> None:
    pre = "# Title\n"
    post = f"# Title\n\nNew {DASH} line.\n"
    findings = added_line_findings(preimage=pre, postimage=post)
    assert [(f.line, f.rule) for f in findings] == [(3, "em-dash")]
    assert DASH in findings[0].excerpt


@pytest.mark.parametrize("line,rule", [
    (f"a {DASH} b", "em-dash"),
    ("a – b", "en-dash-as-em"),
    ("Co-Authored-By: Claude <noreply@example.com>", "ai-authorship"),
])
def test_every_raw_rule_applies_on_raw_lines_even_inside_a_code_fence(
    line: str, rule: str,
) -> None:
    post = f"```\n{line}\n```\n"
    findings = added_line_findings(preimage="", postimage=post)
    assert rule in {f.rule for f in findings}


def test_the_allow_marker_exempts_a_line_but_an_escaped_marker_does_not() -> None:
    allowed = f"Quoted {DASH} text <!-- prose-lint: allow -->\n"
    escaped = f"Quoted {DASH} text \\<!-- prose-lint: allow -->\n"
    assert added_line_findings(preimage="", postimage=allowed) == []
    assert added_line_findings(preimage="", postimage=escaped) != []


def test_curly_quotes_fold_exactly_as_the_linter_does() -> None:
    pre = "It’s fine.\n"
    post = "It's fine.\n"
    # The same line after folding: nothing is added.
    assert added_line_findings(preimage=pre, postimage=post) == []


def test_a_new_file_treats_every_line_as_added() -> None:
    post = f"one\ntwo {DASH} three\n"
    assert [f.line for f in added_line_findings(preimage="", postimage=post)] == [2]


def test_a_relocated_pre_existing_offending_line_is_not_a_finding() -> None:
    bad = f"Moved {DASH} line."
    pre = f"{bad}\nother\n"
    post = f"other\n{bad}\n"
    assert added_line_findings(preimage=pre, postimage=post) == []


def test_a_new_offending_line_is_flagged_even_when_another_one_is_deleted() -> None:
    pre = f"Old {DASH} line.\n"
    post = f"New {DASH} line.\n"
    assert [f.line for f in added_line_findings(preimage=pre, postimage=post)] == [1]


def test_a_repeated_offending_line_counts_only_the_extra_copies() -> None:
    bad = f"Same {DASH} line."
    pre = f"{bad}\n"
    post = f"{bad}\n{bad}\n{bad}\n"
    assert len(added_line_findings(preimage=pre, postimage=post)) == 2


def test_parse_mode_accepts_the_three_modes_and_refuses_anything_else() -> None:
    assert parse_mode("enforce") == "enforce"
    assert parse_mode(" WARN ") == "warn"
    assert parse_mode("off") == "off"
    with pytest.raises(ValueError, match="enforce, warn, off"):
        parse_mode("enfoce")


def test_policy_exclusions_match_like_the_write_blocklist() -> None:
    # Same matching as KB_WRITE_BLOCK_PATHS: fnmatch on the validated target path.
    policy = WritingRulesPolicy(mode="enforce", exclude_paths=("archive/*", "*.txt"))
    assert policy.excludes("archive/old.md")
    assert policy.excludes("notes/readme.txt")
    assert not policy.excludes("universal/rule.md")
