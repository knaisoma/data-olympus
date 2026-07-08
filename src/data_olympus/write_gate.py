"""Write-path integrity gates: serialization lock, CAS enforcement, and the
content-validation gate (0.3.0 epic #72, scope items 1, 3, 4, 8).

These are the checks that make the project's headline write-safety claims true.
They run on the auto-commit path (``tools_write``) AND the operator-resolve path
so both surfaces share one policy.

- ``WriteSerializer``: a process-wide re-entrant lock. Write volume is
  rate-limited, so a single global mutex around the write -> git add -> commit ->
  enqueue critical section is cheap and eliminates the interleave where thread
  A's commit sweeps thread B's staged file. Per-path advisory locks live in the
  pending queue (``PendingQueue.path_lock``) and are shared between this path and
  the pending queue.
- ``check_cas``: enforce a caller-supplied base marker (``base_commit`` /
  ``base_blob_sha`` / ``target_file_hash``) against the CURRENT content of the
  target on the worktree's refreshed base. When no marker is supplied, behavior
  is unchanged.
- ``validate_postimage``: format-level validation of the postimage bytes plus a
  duplicate-id check against the live index, so a malformed / forged document
  never reaches ``origin/main`` (a duplicate id makes every subsequent index
  rebuild fail: one bad write -> persistent degraded state).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.format.validate import RESERVED, TIERS, TYPES

if TYPE_CHECKING:
    from collections.abc import Sequence

    from data_olympus.index import Index

_log = logging.getLogger("data_olympus.write_gate")

# The status vocabulary the write gate enforces. Mirrors validate.STATUSES but
# includes ``approved`` (the real KB uses it for accepted decisions; see
# validate.IN_FORCE_STATUSES) so a legitimate ``approved`` document is not
# rejected as an invalid enum value.
_WRITE_STATUSES = frozenset(
    {"draft", "active", "deprecated", "superseded", "proposed", "accepted",
     "rejected", "approved"}
)


class WriteSerializer:
    """Process-wide re-entrant write lock (scope item 1).

    A single instance is shared by every write path so the write -> git add ->
    commit -> enqueue critical section never interleaves across threads. Re-entrant
    so a future nested acquire (e.g. resolve calling a helper that also locks)
    does not self-deadlock. Held only for the brief duration of a commit; the
    per-path advisory lock (pending queue) provides the finer-grained guarantee
    that a path with a pending proposal cannot be auto-committed concurrently."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> WriteSerializer:
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


@dataclass(frozen=True, slots=True)
class CasResult:
    """Outcome of a compare-and-swap base check."""

    ok: bool
    reason: str = ""


def _blob_sha(content: bytes) -> str:
    """git blob object id of ``content`` (git hashes ``blob <len>\\0<content>``)."""
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 - git uses sha1


def _is_enforceable_base_commit(base_commit: str | None) -> bool:
    """A base_commit is enforceable when it names a SPECIFIC commit the caller
    read the target from. The sentinel ``HEAD`` (and empty/None) means "whatever
    the current base is" and carries no per-file expectation, so it is advisory
    only. A pinned sha / ref is enforceable."""
    if not base_commit:
        return False
    return base_commit.strip().upper() != "HEAD"


def check_cas(
    *,
    worktree_path: str,
    target_path: str,
    base_commit: str | None,
    base_blob_sha: str | None,
    target_file_hash: str | None,
) -> CasResult:
    """Compare the caller's base markers against the CURRENT target content on the
    worktree's refreshed base (scope item 3).

    Semantics:

    - If NO enforceable marker is supplied (``base_commit`` is None/""/``HEAD`` AND
      ``base_blob_sha``/``target_file_hash`` are falsy), CAS is a no-op and returns
      ok: this preserves the pre-0.3.0 behavior for callers that did not opt in.
    - ``base_blob_sha``: the git blob id the caller believed the target held. We
      compute the blob id of the target's current bytes on the worktree and
      require equality. A target that does not yet exist matches only an empty
      base_blob_sha (i.e. the caller must not claim a base for a new file).
    - ``target_file_hash``: a sha256 of the current file bytes (an alternative
      marker some clients send). Same equality rule.
    - ``base_commit`` naming a SPECIFIC commit (not the ``HEAD`` sentinel) is
      ENFORCED: the blob the target had at ``base_commit`` must equal the target's
      current blob on the refreshed base. This closes the bypass where a caller
      supplied only ``base_commit`` (the required field) and CAS silently no-oped
      (Codex Blocker 3). ``HEAD`` remains advisory because it means "the current
      base", which carries no stale-detection expectation.

    A mismatch returns ok=False with a machine-readable ``reason`` so the caller
    rejects ``rejected_stale_base`` rather than committing."""
    has_blob = bool(base_blob_sha)
    has_file_hash = bool(target_file_hash)
    has_commit = _is_enforceable_base_commit(base_commit)
    if not has_blob and not has_file_hash and not has_commit:
        return CasResult(ok=True)

    real = os.path.join(worktree_path, target_path)
    current: bytes | None = None
    if os.path.isfile(real):
        with open(real, "rb") as f:
            current = f.read()
    current_blob = _blob_sha(current) if current is not None else ""

    if has_blob and current_blob != base_blob_sha:
        return CasResult(
            ok=False,
            reason=(
                f"base_blob_sha mismatch: caller={base_blob_sha} "
                f"current={current_blob or '<absent>'}"
            ),
        )
    if has_file_hash:
        actual_h = (
            hashlib.sha256(current).hexdigest() if current is not None else ""
        )
        if actual_h != target_file_hash:
            return CasResult(
                ok=False,
                reason=(
                    f"target_file_hash mismatch: caller={target_file_hash} "
                    f"current={actual_h or '<absent>'}"
                ),
            )
    if has_commit:
        # The blob the caller read at base_commit must still be the current blob.
        # base_commit is None-checked by _is_enforceable_base_commit.
        assert base_commit is not None
        if not _commit_exists(worktree_path, base_commit):
            # An unknown/unreachable pinned commit is not an enforceable base: it
            # cannot vouch for the current content, so reject rather than silently
            # passing (a "" base blob would otherwise match a "" absent-target blob
            # and let a stale write through -- Codex round-2 Concern 1).
            return CasResult(
                ok=False,
                reason=f"base_commit unknown/unreachable: {base_commit}",
            )
        base_blob = _git_blob_at(worktree_path, base_commit, target_path)
        if base_blob != current_blob:
            return CasResult(
                ok=False,
                reason=(
                    f"base_commit mismatch: content at {base_commit} "
                    f"(blob={base_blob or '<absent>'}) differs from current "
                    f"(blob={current_blob or '<absent>'})"
                ),
            )
    return CasResult(ok=True)


def _commit_exists(worktree_path: str, commit: str) -> bool:
    """True if ``commit`` resolves to a commit object in the worktree."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "--verify", "--quiet",
         f"{commit}^{{commit}}"],
        check=False, capture_output=True, text=True,
    )
    return result.returncode == 0


def _git_blob_at(worktree_path: str, commit: str, target_path: str) -> str:
    """The git blob id of ``target_path`` as of ``commit`` in the worktree, or ""
    if the path did not exist there (or the commit is unknown). Uses
    ``git rev-parse <commit>:<path>`` which prints the blob object id directly."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", f"{commit}:{target_path}"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of the content-validation gate."""

    ok: bool
    errors: tuple[dict[str, str], ...] = ()


def _is_reserved(target_path: str) -> bool:
    return PurePosixPath(target_path).name in RESERVED


def _effective_doc_id(fm: dict[str, object], target_path: str) -> str:
    """The id the indexer will assign to ``target_path``: the explicit
    frontmatter ``id`` when present and a plain string, else the path-derived id.

    This mirrors ``index.build`` (``doc_id = doc.id or _derive_id_from_path(rel)``)
    and ``markdown_parse.parse_file`` (which drops an id containing ``:``), so the
    duplicate-id gate reasons about the SAME id the rebuild will compute. A
    path-derived id lets the gate catch a NEW file whose derived id collides with
    an existing explicit id (Codex Blocker 1)."""
    from data_olympus.index import _derive_id_from_path

    raw = fm.get("id")
    if isinstance(raw, str) and raw and ":" not in raw:
        return raw
    return _derive_id_from_path(Path(target_path))


def _worktree_id_map(worktree_path: str) -> dict[str, str]:
    """``{doc_id: path}`` for every non-excluded ``.md`` file COMMITTED in the
    worktree's current HEAD tree (Codex Blocker 1, concurrent case).

    The live index rebuilds only on the git_pull_loop, so two auto-commits that
    introduce the same NEW id in quick succession both pass an ``idx``-only check
    (neither is indexed yet) and then break the next rebuild. Scanning the
    worktree tree (which already contains the first commit by the time the second
    validates, because the write lock serializes them) closes that window. Returns
    {} on any git error (fail open on the tree scan; the idx check still applies)."""
    from data_olympus.index import _derive_id_from_path, _is_excluded

    result = subprocess.run(
        ["git", "-C", worktree_path, "ls-tree", "-r", "--name-only", "HEAD"],
        check=False, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    id_map: dict[str, str] = {}
    for rel in result.stdout.splitlines():
        rel = rel.strip()
        if not rel.endswith(".md"):
            continue
        if _is_excluded(Path(rel)):
            continue
        blob = subprocess.run(
            ["git", "-C", worktree_path, "show", f"HEAD:{rel}"],
            check=False, capture_output=True, text=True,
        )
        if blob.returncode != 0:
            continue
        try:
            fm, _body = parse_frontmatter(blob.stdout)
        except ValueError:
            fm = {}
        raw = fm.get("id") if isinstance(fm, dict) else None
        doc_id = (raw if isinstance(raw, str) and raw and ":" not in raw
                  else _derive_id_from_path(Path(rel)))
        id_map[doc_id] = rel
    return id_map


def validate_postimage(
    *,
    target_path: str,
    postimage: str,
    idx: Index | None,
    worktree_path: str | None = None,
) -> ValidationResult:
    """Format-level validation of ``postimage`` before it is committed (item 4).

    Rejects, with machine-readable errors, the failure classes that either break
    parsing or corrupt the index on the next rebuild:

    - **Malformed YAML frontmatter.** A block opened but never closed, or one that
      does not parse to a mapping, currently commits and then breaks the index
      build. Rejected as ``invalid_frontmatter``.
    - **Invalid enum value.** ``type`` / ``status`` / ``tier`` present but outside
      the controlled vocabulary. Rejected as ``invalid_enum``. (Missing required
      fields are NOT rejected here: memory-inbox documents legitimately carry no
      ``id``/``type``/``status``/``tier`` and derive their id from the path. The
      gate blocks documents that are actively malformed, not merely sparse.)
    - **Duplicate / forged id.** The EFFECTIVE id the rebuild will assign (explicit
      frontmatter ``id`` or the path-derived id) already belongs to a DIFFERENT
      path, in the live index OR in the worktree's committed tree. A duplicate id
      makes every subsequent index rebuild raise ``DuplicateIdError``: one bad
      write puts the server in a persistent degraded state. Rejected as
      ``duplicate_id``. An id that resolves to the SAME path (a legitimate
      edit-in-place) is allowed. This applies to reserved files too, since the
      indexer still assigns them an id.

    The concept-schema exemption for reserved filenames (``index.md`` / ``log.md``
    / ``template.md``, SPEC section 4.2) suppresses only the enum checks, NOT the
    duplicate-id check.
    """
    errors: list[dict[str, str]] = []

    # 1. Frontmatter must parse. parse_frontmatter raises ValueError on an
    # unterminated block or a non-mapping block; either is a hard reject.
    try:
        fm, _body = parse_frontmatter(postimage)
    except ValueError as exc:
        return ValidationResult(
            ok=False,
            errors=({"field": "frontmatter", "code": "invalid_frontmatter",
                     "message": str(exc)},),
        )

    # 2. Enum values, when present, must be in vocabulary. Reserved files are
    # schema-exempt from this check (but NOT from the duplicate-id check below).
    if not _is_reserved(target_path):
        for field, allowed in (("type", TYPES), ("status", _WRITE_STATUSES),
                               ("tier", TIERS)):
            value = fm.get(field)
            if value is not None and value not in allowed:
                errors.append({
                    "field": field, "code": "invalid_enum",
                    "message": f"invalid {field} '{value}' "
                               f"(allowed: {sorted(allowed)})",
                })

    # 3. Duplicate / forged id against BOTH the live index and the worktree tree,
    # using the EFFECTIVE id (explicit or path-derived) so a new file whose derived
    # id collides with an existing explicit id is caught, and so a same-new-id race
    # between two serialized commits is caught (the first is already committed in
    # the tree when the second validates).
    effective_id = _effective_doc_id(fm, target_path)
    collision_path: str | None = None
    if effective_id:
        if idx is not None:
            try:
                index_map = idx.id_to_path_map()
            except Exception:  # noqa: BLE001 - index read must never fail-closed here
                index_map = {}
            if isinstance(index_map, dict):
                other = index_map.get(effective_id)
                if other is not None and other != target_path:
                    collision_path = other
        if collision_path is None and worktree_path is not None:
            tree_map = _worktree_id_map(worktree_path)
            other = tree_map.get(effective_id)
            if other is not None and other != target_path:
                collision_path = other
    if collision_path is not None:
        errors.append({
            "field": "id", "code": "duplicate_id",
            "message": f"id '{effective_id}' already used by '{collision_path}'",
        })

    if errors:
        return ValidationResult(ok=False, errors=tuple(errors))
    return ValidationResult(ok=True)


def reset_worktree(worktree_path: str) -> None:
    """Discard any staged/working changes in the worktree (scope item 8).

    Ordering-of-side-effects fix: if a write fails a late gate (CAS, validation,
    or trailer/message validation) AFTER the postimage was written and
    ``git add``-ed, the staged file would otherwise be swept into the session's
    NEXT commit. A hard reset to HEAD leaves no staged leftovers so a rejected
    write is a true no-op. Best-effort: a reset failure is logged by the caller,
    not raised, because the primary rejection is what must surface."""
    subprocess.run(
        ["git", "-C", worktree_path, "reset", "--hard", "HEAD"],
        check=True, capture_output=True,
    )


# --- secret-scanning gate (issue #71) --------------------------------------
#
# Scans a postimage for credential-shaped content BEFORE it is committed, so a
# leaked credential in an agent-captured memory/edit/bootstrap file never
# becomes a permanent git object on the hosted remote. Every regex below is a
# bounded, non-backtracking character-class match (no nested quantifiers over
# overlapping alternatives), so a large postimage cannot trigger catastrophic
# backtracking (ReDoS).
#
# REDACTION IS THE POINT of this module: callers must only ever surface
# ``SecretMatch.pattern_name`` and ``SecretMatch.line`` (an approximate,
# 1-indexed line number) to a tool response, pending meta, audit event, or log
# line. The matched substring itself never leaves ``scan_postimage_for_secrets``.


@dataclass(frozen=True, slots=True)
class SecretMatch:
    """One detected secret occurrence. Carries ONLY the pattern name and an
    approximate line number, never the matched text."""

    pattern_name: str
    line: int


@dataclass(frozen=True, slots=True)
class SecretScanResult:
    """Outcome of scanning a postimage for credential-shaped content."""

    ok: bool
    match: SecretMatch | None = None


_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?:(?:RSA|EC|OPENSSH|DSA|PGP) )?PRIVATE KEY-----"
)
# gh[oprs]_ covers ghp_/gho_/ghs_/ghr_ in one alternation; github_pat_ is a
# distinct, longer-lived token format introduced later. Both are grouped under
# one pattern name since the issue treats them as a single "GitHub tokens"
# class.
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[oprs]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255})\b"
)
_AWS_ACCESS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
_SLACK_TOKEN_RE = re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,200}\b")
# Generic key=value / key: value credential assignment. Matches both a bare
# key (``password=``) and a prefixed key (``DB_PASSWORD=``, ``API_SECRET:``) --
# real leaks are far more often an env-style prefixed key than a bare word, so
# requiring a leading word boundary on ``password``/``secret`` itself (which
# ``\b`` would, since ``_`` is a word character with no boundary before
# ``PASSWORD`` in ``DB_PASSWORD``) would miss the common case. The value is
# captured so obvious placeholders (``password=changeme``,
# ``password=<your password>``) can be excluded below rather than flagged as a
# real leak.
_GENERIC_CRED_RE = re.compile(
    r"""(?:^|[^A-Za-z0-9_])[A-Za-z0-9_]{0,40}(?:password|passwd|secret)"""
    r"""\s*[:=]\s*(['"]?)(?P<value>[^\s'"]{1,200})\1""",
    re.IGNORECASE,
)
# scheme://user:password@host -- a connection string carrying an inline
# password. Every segment is a bounded negated-character-class match.
_CONN_STRING_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://"
    r"[^\s:/@'\"]{1,200}:[^\s@/'\"]{1,200}@[^\s/'\"]{1,200}"
)

_BUILTIN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", _PRIVATE_KEY_RE),
    ("github_token", _GITHUB_TOKEN_RE),
    ("aws_access_key_id", _AWS_ACCESS_KEY_RE),
    ("slack_token", _SLACK_TOKEN_RE),
    ("generic_credential_assignment", _GENERIC_CRED_RE),
    ("connection_string_password", _CONN_STRING_RE),
)

_PLACEHOLDER_VALUES = frozenset({
    "", "changeme", "change_me", "change-me", "placeholder", "example",
    "redacted", "xxx", "xxxx", "todo", "fixme", "password", "secret",
    "test", "null", "none", "n/a", "your_password", "yourpassword",
})


def _is_placeholder_value(value: str) -> bool:
    """True when a matched credential VALUE is an obvious non-secret
    placeholder (docs/templates commonly write ``password=changeme`` or
    ``password=<your password>``). Reduces false positives on the generic
    key=value pattern without weakening detection of a real-looking value."""
    v = value.strip().strip("'\"")
    if not v:
        return True
    if v.lower() in _PLACEHOLDER_VALUES:
        return True
    return v[0] in "<{$" or set(v.lower()) <= {"x"} or set(v) <= {"*"}


def _line_of(content: str, index: int) -> int:
    """1-indexed line number of ``index`` within ``content``."""
    return content.count("\n", 0, index) + 1


# Heuristic ReDoS guard for operator-supplied extra patterns. Matches a
# parenthesized group that itself contains a `+`/`*` quantifier, immediately
# followed by another `+`/`*` quantifying the whole group -- the classic
# catastrophic-backtracking shape (``(a+)+``, ``(\d*)*``, ``([a-z]+)*``, ...).
# This is a heuristic, not a proof of safety (it does not catch every ReDoS
# shape, e.g. one spread across nested groups), but it catches the common,
# well-known evil-regex pattern with no false negatives on the built-in set
# (none of which have a quantifier nested inside a quantified group). Bounded
# repetition (``{n,m}``) and non-nested quantifiers are not flagged.
_REDOS_NESTED_QUANTIFIER_RE = re.compile(r"\([^()]*[+*][^()]*\)[+*]")


def _looks_redos_prone(pattern_src: str) -> bool:
    """True when ``pattern_src`` contains the classic nested-quantifier shape
    that causes catastrophic backtracking in a backtracking regex engine."""
    return bool(_REDOS_NESTED_QUANTIFIER_RE.search(pattern_src))


def load_extra_secret_patterns(
    env_value: str | None = None,
) -> list[tuple[str, re.Pattern[str]]]:
    """Parse ``KB_SECRET_SCAN_EXTRA_PATTERNS``: a comma-separated list of extra
    regexes an operator wants scanned in addition to the built-in set. Each
    entry becomes its own named pattern (``custom_1``, ``custom_2``, ...). An
    invalid regex is logged and SKIPPED, never raised, so one operator typo in
    the env var cannot crash the write path. A pattern with the classic
    nested-quantifier ReDoS shape (see :func:`_looks_redos_prone`) is also
    logged and skipped: it runs against every proposed postimage on the
    single-writer critical path, so a catastrophic-backtracking pattern would
    hang the whole write pipeline, not just one request."""
    raw = (
        env_value if env_value is not None
        else os.environ.get("KB_SECRET_SCAN_EXTRA_PATTERNS", "")
    )
    out: list[tuple[str, re.Pattern[str]]] = []
    for piece in raw.split(","):
        pattern_src = piece.strip()
        if not pattern_src:
            continue
        if _looks_redos_prone(pattern_src):
            _log.warning(
                "KB_SECRET_SCAN_EXTRA_PATTERNS entry %r has a nested-quantifier "
                "shape that risks catastrophic backtracking and will be "
                "skipped; rewrite it without a quantifier nested inside a "
                "quantified group", pattern_src,
            )
            continue
        try:
            compiled = re.compile(pattern_src)
        except re.error as exc:
            _log.warning(
                "KB_SECRET_SCAN_EXTRA_PATTERNS entry %r is not a valid regex "
                "and will be skipped: %s", pattern_src, exc,
            )
            continue
        out.append((f"custom_{len(out) + 1}", compiled))
    return out


def scan_postimage_for_secrets(
    *,
    postimage: str,
    extra_patterns: Sequence[tuple[str, re.Pattern[str]]] | None = None,
) -> SecretScanResult:
    """Scan ``postimage`` for credential-shaped content (issue #71).

    Checks the built-in pattern set plus ``extra_patterns`` (default: parsed
    fresh from ``KB_SECRET_SCAN_EXTRA_PATTERNS`` on every call, so a changed env
    var takes effect without threading config through every caller). Returns
    the EARLIEST match across all patterns. Only the pattern NAME and an
    approximate 1-indexed line number are returned in the result -- never the
    matched substring -- so a caller can safely put it in a tool response,
    audit event, or log line without leaking the secret value itself."""
    patterns = list(_BUILTIN_PATTERNS)
    patterns.extend(
        extra_patterns if extra_patterns is not None
        else load_extra_secret_patterns()
    )

    best: tuple[int, str] | None = None  # (start_index, pattern_name)
    for name, pattern in patterns:
        match_start: int | None = None
        for m in pattern.finditer(postimage):
            if name == "generic_credential_assignment" and _is_placeholder_value(
                m.group("value")
            ):
                continue
            match_start = m.start()
            break
        if match_start is None:
            continue
        if best is None or match_start < best[0]:
            best = (match_start, name)

    if best is None:
        return SecretScanResult(ok=True)
    start, name = best
    return SecretScanResult(
        ok=False, match=SecretMatch(pattern_name=name, line=_line_of(postimage, start))
    )
