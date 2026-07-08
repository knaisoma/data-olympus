"""Governed-lane write protection (issue #112): "agents can propose, only
humans can promote".

Three mechanisms, composed at the tool-function layer (``tools_write.py``'s
``kb_propose_memory_fn`` / ``kb_propose_edit_fn``, and
``tools_onboarding.py``'s bootstrap path), all gated behind
``KB_GOVERNED_LANE_PROTECTION`` (default ON; ``off`` restores the pre-#112
behavior exactly):

- **Status clamp** (:func:`check_status_clamp`): a non-operator-confirmed
  write whose postimage sets/changes ``status`` INTO the in-force class
  (:data:`~data_olympus.format.validate.IN_FORCE_STATUSES`, single-sourced --
  never a locally hardcoded copy) is demoted to a pending entry rather than
  auto-committed. The pending queue / operator-resolve path is UNCHANGED: a
  human resolving a pending entry (``operator_confirmed=True`` in
  ``_commit_in_worktree``) IS the promotion, so this module is never consulted
  on that path.
- **Governed-target edit demotion** (:func:`is_target_in_force`): an edit
  whose target document is CURRENTLY in force (the full composed predicate --
  status class AND validity window AND not-inbox AND not-graph-excluded, via
  :func:`data_olympus.format.validate.is_in_force` against the LIVE index) is
  demoted regardless of confidence. An expired or superseded-out target is NOT
  in force and so is NOT protected by this rule (issue #112 non-goal).
- **Injection-pattern annotation** (:func:`scan_for_injection_patterns`):
  advisory only, never blocks or demotes by itself. Mirrors the issue #71
  secret-scan redaction discipline: only the pattern NAME and an approximate
  line number are ever surfaced, never the matched text.

Nothing in this module ever REJECTS a write; it only computes whether one
should be DEMOTED to pending, and annotates why. The demotion decision itself
is applied by the caller (see ``tools_write.py``), which also encodes the
"secret-scan gate runs first" ordering rule: a postimage that would ALSO be
rejected by the issue #71 secret scanner must be rejected outright, never
silently demoted (issue #112 test scenario: ordering).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.format.validate import IN_FORCE_STATUSES, is_in_force, today_iso

if TYPE_CHECKING:
    from data_olympus.index import Index

_ENV_VAR = "KB_GOVERNED_LANE_PROTECTION"


def governed_lane_protection_enabled(env_value: str | None = None) -> bool:
    """True unless ``KB_GOVERNED_LANE_PROTECTION`` is explicitly set to an
    "off" value (off/0/false/no, case-insensitive). Default ON, matching the
    issue #112 rollout ("enabled by default; env opt-out restores current
    behavior exactly"). ``env_value`` is injectable for tests; None reads the
    live environment on every call (same pattern as
    ``write_gate.load_extra_secret_patterns``), so a changed env var takes
    effect without threading config through every caller.
    """
    raw = env_value if env_value is not None else os.environ.get(_ENV_VAR, "")
    return raw.strip().lower() not in ("off", "0", "false", "no")


@dataclass(frozen=True, slots=True)
class StatusClampResult:
    """Outcome of the status-clamp check (rule 1)."""

    demoted: bool
    new_status: str | None = None


def check_status_clamp(postimage: str) -> StatusClampResult:
    """True when ``postimage``'s frontmatter ``status`` is being set/changed
    INTO the in-force class (:data:`IN_FORCE_STATUSES`, single-sourced from
    ``format.validate`` -- never a locally hardcoded copy).

    This is intentionally UNCONDITIONAL on the prior status: a
    non-operator-confirmed write claiming an in-force status is clamped
    whether the target previously had no status at all (a new file), a
    non-in-force status (a promotion), or -- defense in depth -- already
    claimed an in-force status (an unreviewed "edit that keeps it accepted").
    A malformed/unparseable postimage is treated as "no status claimed" here;
    the content-validation gate (``write_gate.validate_postimage``) is the
    authority on malformed frontmatter and runs as its own, separate check.
    """
    try:
        fm, _body = parse_frontmatter(postimage)
    except ValueError:
        return StatusClampResult(demoted=False)
    status = fm.get("status") if isinstance(fm, dict) else None
    if isinstance(status, str) and status in IN_FORCE_STATUSES:
        return StatusClampResult(demoted=True, new_status=status)
    return StatusClampResult(demoted=False)


# Tristate outcome of the governed-target lookup (rule 2). ``unknown`` means
# the lookup could not be completed (no index wired, or an index read
# failed); the caller treats it as FAIL CLOSED -- an edit whose target's
# in-force state cannot be verified is demoted to pending, never
# auto-committed on the strength of a broken lookup (codex security review
# blocker: the earlier fail-open version let an unhealthy index bypass the
# governed-target rule entirely).
TARGET_IN_FORCE = "in_force"
TARGET_NOT_IN_FORCE = "not_in_force"
TARGET_UNKNOWN = "unknown"


def governed_target_state(
    idx: Index | None, target_path: str, *, today: str | None = None,
) -> str:
    """Classify ``target_path``'s CURRENT in-force state in the LIVE index
    (rule 2): :data:`TARGET_IN_FORCE`, :data:`TARGET_NOT_IN_FORCE`, or
    :data:`TARGET_UNKNOWN`.

    Uses the SAME composed predicate every other in-force surface uses
    (status class AND validity window AND not-inbox AND not-graph-excluded --
    see ``format.validate.is_in_force`` + ``Index.graph_excluded_ids``,
    mirroring ``tools_read.kb_get_fn``'s computed ``in_force`` field), so an
    expired or graph-excluded (superseded-out) target is correctly NOT
    protected by this rule -- it is not currently governing anything.

    Definitive vs unknown:

    - A target with NO entry in a healthy index map is definitively
      :data:`TARGET_NOT_IN_FORCE` (a brand-new file cannot already be in
      force).
    - ``idx is None`` (no index wired at all) and ANY index read failure
      (``id_to_path_map``/``get`` raising, a non-dict map, a map entry whose
      doc vanished between the two reads) are :data:`TARGET_UNKNOWN` -- the
      caller fails CLOSED on it (demote, never auto-commit unverified).
    - The graph-exclusion lookup is the one place a failure stays lenient in
      the PROTECTIVE direction: graph exclusion can only REMOVE protection
      (an excluded doc is not in force), so when that lookup fails the doc is
      treated as NOT excluded and the status/window verdict stands --
      protection is kept, never dropped, on that failure.
    """
    if idx is None:
        return TARGET_UNKNOWN
    today = today if today is not None else today_iso()
    try:
        id_to_path = idx.id_to_path_map()
    except Exception:  # noqa: BLE001 - classified as unknown (fail closed)
        return TARGET_UNKNOWN
    if not isinstance(id_to_path, dict):
        return TARGET_UNKNOWN
    doc_id = next((i for i, p in id_to_path.items() if p == target_path), None)
    if doc_id is None:
        return TARGET_NOT_IN_FORCE
    try:
        doc = idx.get(doc_id)
    except Exception:  # noqa: BLE001 - classified as unknown (fail closed)
        return TARGET_UNKNOWN
    if doc is None:
        return TARGET_UNKNOWN
    graph_excluded_fn = getattr(idx, "graph_excluded_ids", None)
    try:
        excluded = graph_excluded_fn(today=today) if graph_excluded_fn is not None else set()
    except Exception:  # noqa: BLE001 - keep protection on this failure (see docstring)
        excluded = set()
    if doc.id in excluded:
        return TARGET_NOT_IN_FORCE
    in_force = is_in_force(
        doc.status, doc.valid_from, doc.valid_until, today, is_inbox=doc.is_inbox,
    )
    return TARGET_IN_FORCE if in_force else TARGET_NOT_IN_FORCE


def is_target_in_force(
    idx: Index | None, target_path: str, *, today: str | None = None,
) -> bool:
    """Back-compat boolean wrapper over :func:`governed_target_state`: True
    ONLY on a definitive :data:`TARGET_IN_FORCE`. Enforcement callers must
    use :func:`governed_target_state` directly -- the boolean cannot
    distinguish ``not_in_force`` from ``unknown`` and so cannot implement
    the fail-closed contract."""
    return governed_target_state(idx, target_path, today=today) == TARGET_IN_FORCE


@dataclass(frozen=True, slots=True)
class InjectionMatch:
    """One detected agent-directed injection pattern occurrence. Carries ONLY
    the pattern name and an approximate line number, mirroring
    ``write_gate.SecretMatch`` -- the matched text itself never leaves
    :func:`scan_for_injection_patterns`."""

    pattern_name: str
    line: int


def _line_of(content: str, index: int) -> int:
    """1-indexed line number of ``index`` within ``content``."""
    return content.count("\n", 0, index) + 1


# Every pattern below is a bounded, linear-time regex (no nested quantifiers
# over overlapping alternatives), matching the write_gate secret-scan
# discipline: this scan runs on every propose call, so it must never risk
# catastrophic backtracking on an adversarial postimage.
_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "ignore_previous_instructions",
        re.compile(
            r"ignore\s+(?:all\s+)?previous\s+(?:instructions|rules)",
            re.IGNORECASE,
        ),
    ),
    (
        "disregard_policy",
        re.compile(
            r"disregard\b[^\n]{0,40}?\b(?:instructions|policy)\b", re.IGNORECASE,
        ),
    ),
    (
        "imperative_exfiltration",
        re.compile(
            r"\b(?:send|post|upload)\b[^\n]{0,60}?\bhttps?://", re.IGNORECASE,
        ),
    ),
    # A long base64-alphabet run is "blob-shaped" content worth flagging for
    # review (a real base64 secret, or an attempt to smuggle an encoded
    # instruction past a naive keyword scan). Bounded length (no unbounded
    # backtracking): a fixed repetition count over a simple character class.
    ("base64_like_blob", re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")),
    (
        "do_not_tell_operator",
        re.compile(
            r"do\s+not\s+tell\s+the\s+(?:user|operator)", re.IGNORECASE,
        ),
    ),
)


def scan_for_injection_patterns(postimage: str) -> list[InjectionMatch]:
    """Scan ``postimage`` for agent-directed injection patterns (rule 3,
    advisory only). Returns one :class:`InjectionMatch` per DISTINCT pattern
    that matched (the first occurrence's line), never the full match list --
    a reviewer needs to know WHICH patterns fired, not every occurrence.
    """
    matches: list[InjectionMatch] = []
    for name, pattern in _INJECTION_PATTERNS:
        m = pattern.search(postimage)
        if m is not None:
            matches.append(InjectionMatch(pattern_name=name, line=_line_of(postimage, m.start())))
    return matches


@dataclass(frozen=True, slots=True)
class GovernedLaneVerdict:
    """Combined verdict from evaluating all three governed-lane rules against
    one candidate write."""

    # "status_promotion" | "governed_target" | "governed_target_unverified"
    # | None. The _unverified variant is the FAIL-CLOSED outcome: the
    # governed-target lookup could not be completed (no index / index read
    # failure), so the write is demoted rather than auto-committed on the
    # strength of a broken lookup.
    demotion_reason: str | None
    injection_matches: tuple[InjectionMatch, ...] = ()

    @property
    def demoted(self) -> bool:
        return self.demotion_reason is not None

    @property
    def injection_suspect(self) -> bool:
        return bool(self.injection_matches)

    def injection_pattern_names(self) -> list[str]:
        """``["name:line", ...]`` -- the loggable/meta-safe shape (never the
        matched text), mirroring the issue #71 ``matching_pattern`` field."""
        return [f"{m.pattern_name}:{m.line}" for m in self.injection_matches]


def evaluate_governed_lane(
    *,
    postimage: str,
    target_path: str,
    idx: Index | None,
    check_governed_target: bool,
    today: str | None = None,
) -> GovernedLaneVerdict:
    """Evaluate all three governed-lane rules for one candidate write.

    ``check_governed_target`` scopes rule 2 to the callers where it applies
    (``kb_propose_edit_fn``): a memory proposal always targets a brand-new
    inbox file (never already in force) and a bootstrap file is only ever
    written into an absent/partial workspace, so both pass False and rely
    solely on the status clamp (rule 1). The status clamp (rule 1) is checked
    FIRST and short-circuits rule 2: if the postimage itself claims an
    in-force status, the target's PRIOR in-force state is irrelevant to the
    demotion decision (it demotes either way), and skipping the extra index
    lookup is a modest efficiency win.

    Rule 2 fails CLOSED (codex security review blocker): a lookup that
    cannot be completed (:data:`TARGET_UNKNOWN` -- no index wired, or an
    index read failure) demotes with the distinct machine-readable reason
    ``governed_target_unverified``, so an unhealthy index can never be used
    to slip an edit past the governed-target rule, and the audit trail
    truthfully distinguishes "target verified in force" from "target could
    not be verified".
    """
    injection_matches = tuple(scan_for_injection_patterns(postimage))
    clamp = check_status_clamp(postimage)
    if clamp.demoted:
        return GovernedLaneVerdict(
            demotion_reason="status_promotion", injection_matches=injection_matches,
        )
    if check_governed_target:
        state = governed_target_state(idx, target_path, today=today)
        if state == TARGET_IN_FORCE:
            return GovernedLaneVerdict(
                demotion_reason="governed_target", injection_matches=injection_matches,
            )
        if state == TARGET_UNKNOWN:
            return GovernedLaneVerdict(
                demotion_reason="governed_target_unverified",
                injection_matches=injection_matches,
            )
    return GovernedLaneVerdict(demotion_reason=None, injection_matches=injection_matches)
