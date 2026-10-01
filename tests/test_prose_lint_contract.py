"""The packaged writing-rule module keeps the API the write gate imports (#283).

`data_olympus._vendor.prose_lint` is a vendored copy whose bytes upstream owns.
The write gate reads its raw tier through three names. An upstream refactor that
renames or reshapes them must fail here rather than silently disable the gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from data_olympus._vendor import prose_lint

ROOT = Path(__file__).resolve().parents[1]


def test_raw_rules_are_the_three_expected_name_pattern_message_triples() -> None:
    assert len(prose_lint.RAW_RULES) > 0
    for rule in prose_lint.RAW_RULES:
        assert len(rule) == 3
        name, pattern, message = rule
        assert isinstance(name, str)
        assert isinstance(pattern, re.Pattern)
        assert isinstance(message, str)
        assert message
    assert [name for name, _, _ in prose_lint.RAW_RULES] == [
        "em-dash", "en-dash-as-em", "ai-authorship",
    ]


def test_allow_marker_and_normalise_exist_with_their_documented_behaviour() -> None:
    assert isinstance(prose_lint.ALLOW, re.Pattern)
    assert prose_lint.ALLOW.search("quoted text <!-- prose-lint: allow -->")
    assert not prose_lint.ALLOW.search("quoted text \\<!-- prose-lint: allow -->")
    assert prose_lint.normalise("’‘“”") == "''\"\""


def test_the_scripts_shim_runs_the_packaged_linter(tmp_path: Path) -> None:
    clean = tmp_path / "clean.md"
    clean.write_text("A plain sentence.\n", encoding="utf-8")
    dirty = tmp_path / "dirty.md"
    dirty.write_text("A sentence — with a dash.\n", encoding="utf-8")
    shim = ROOT / "scripts" / "prose_lint.py"
    ok = subprocess.run([sys.executable, str(shim), str(clean)],
                        capture_output=True, text=True, check=False)
    bad = subprocess.run([sys.executable, str(shim), str(dirty)],
                         capture_output=True, text=True, check=False)
    assert ok.returncode == 0, ok.stderr
    assert bad.returncode == 1
    assert "em-dash" in bad.stdout
