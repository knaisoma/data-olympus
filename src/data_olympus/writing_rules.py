"""Writing rules on write-pipeline postimages (issue #283).

The project's own CI gate rejects em-dashes, en-dashes used as em-dashes, and
agent authorship credit in its prose (`data_olympus._vendor.prose_lint`, the
vendored tier-one "raw" rules). This module applies the same raw rules to the
documents agents write into a knowledge base, with two deliberate limits:

- Only lines the write ADDS are checked, so an edit never fails on text it did
  not write. A line counts as added when it occurs more often in the postimage
  than in the preimage, both folded through the linter's `normalise`. The
  comparison is a multiset count difference: linear, with no threshold, so no
  input size changes what is checked. A relocated pre-existing line is not
  added; a NEW offending line is, even when a different one is deleted.
- Only tier one applies. The Markdown-aware tiers trade misses for false
  positives, which suits a repository's CI and does not suit a write gate.

The allow marker (`<!-- prose-lint: allow -->` at the end of a line) exempts a
line here exactly as it does in CI, and it stays visible in the committed text.
"""
from __future__ import annotations

import fnmatch
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from data_olympus._vendor.prose_lint import ALLOW, RAW_RULES, normalise

Mode = Literal["enforce", "warn", "off"]
MODES: tuple[Mode, ...] = ("enforce", "warn", "off")
DEFAULT_MODE: Mode = "warn"

# The same excerpt window the linter prints, so a finding reads the same in a
# write response as it does in CI.
_EXCERPT_RADIUS = 30


def parse_mode(raw: str) -> Mode:
    """Parse KB_WRITING_RULES_MODE. A typo is refused, never defaulted, so it can
    not silently become the permissive mode."""
    value = raw.strip().lower()
    for mode in MODES:
        if value == mode:
            return mode
    raise ValueError(
        f"KB_WRITING_RULES_MODE={raw!r} is not one of: {', '.join(MODES)}")


@dataclass(frozen=True)
class WritingRulesPolicy:
    """How the write pipeline applies the writing rules."""

    mode: Mode = DEFAULT_MODE
    exclude_paths: tuple[str, ...] = ()

    def excludes(self, target_path: str) -> bool:
        # Matched exactly like KB_WRITE_BLOCK_PATHS (auth.WriteBlocklist.blocks):
        # fnmatch on the target path the write tools have already validated.
        return any(fnmatch.fnmatch(target_path, p) for p in self.exclude_paths)


@dataclass(frozen=True)
class Finding:
    line: int
    rule: str
    excerpt: str

    def render(self) -> str:
        return f"line {self.line}: {self.rule}: {self.excerpt}"


def added_line_findings(*, preimage: str, postimage: str) -> list[Finding]:
    """Raw-rule findings on the lines ``postimage`` adds over ``preimage``."""
    before = Counter(normalise(preimage).splitlines())
    seen: Counter[str] = Counter()
    findings: list[Finding] = []
    for n, line in enumerate(normalise(postimage).splitlines(), 1):
        seen[line] += 1
        if seen[line] <= before[line]:
            continue  # this copy of the line was already there
        if ALLOW.search(line):
            continue
        for name, rx, _why in RAW_RULES:
            for m in rx.finditer(line):
                start = max(0, m.start() - _EXCERPT_RADIUS)
                findings.append(Finding(
                    line=n, rule=name,
                    excerpt=line[start:m.end() + _EXCERPT_RADIUS].strip()))
    return findings


def policy_from_config(config: object) -> WritingRulesPolicy:
    """The policy a running server applies, read from its Config."""
    mode = parse_mode(str(getattr(config, "writing_rules_mode", DEFAULT_MODE)))
    excludes = tuple(getattr(config, "writing_rules_exclude_paths", ()) or ())
    return WritingRulesPolicy(mode=mode, exclude_paths=excludes)
