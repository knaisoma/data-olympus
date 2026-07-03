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
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.format.validate import RESERVED, TIERS, TYPES

if TYPE_CHECKING:
    from data_olympus.index import Index

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


def check_cas(
    *,
    worktree_path: str,
    target_path: str,
    base_commit: str | None,
    base_blob_sha: str | None,
    target_file_hash: str | None,
) -> CasResult:
    """Compare the caller's base markers against the CURRENT target content on the
    worktree (scope item 3).

    Semantics:

    - If NO base marker is supplied (all of ``base_commit`` in {None, "", "HEAD"}
      and ``base_blob_sha`` and ``target_file_hash`` are falsy), CAS is a no-op and
      returns ok: this preserves the pre-0.3.0 behavior for callers that did not
      opt in.
    - ``base_blob_sha``: the git blob id the caller believed the target held. We
      compute the blob id of the target's current bytes on the worktree and
      require equality. A target that does not yet exist matches only an empty
      base_blob_sha (i.e. the caller must not claim a base for a new file).
    - ``target_file_hash``: a sha256 of the current file bytes (an alternative
      marker some clients send). Same equality rule.

    ``base_commit`` alone is advisory (the worktree is rebased onto the refreshed
    base before this check, so a bare commit marker cannot be meaningfully
    compared per-file); the blob/file-hash markers are the enforced ones. A
    mismatch returns ok=False with ``rejected_stale_base`` so the caller does not
    commit."""
    # base_commit is accepted for API symmetry and audit context but is advisory
    # only: the worktree is rebased onto the refreshed base before this check, so
    # a bare commit marker cannot be meaningfully compared per file. The enforced
    # markers are the blob / file-hash ones below.
    _ = base_commit
    has_blob = bool(base_blob_sha)
    has_file_hash = bool(target_file_hash)
    if not has_blob and not has_file_hash:
        return CasResult(ok=True)

    real = os.path.join(worktree_path, target_path)
    current: bytes | None = None
    if os.path.isfile(real):
        with open(real, "rb") as f:
            current = f.read()

    if has_blob:
        actual = _blob_sha(current) if current is not None else ""
        if actual != base_blob_sha:
            return CasResult(
                ok=False,
                reason=(
                    f"base_blob_sha mismatch: caller={base_blob_sha} "
                    f"current={actual or '<absent>'}"
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
    return CasResult(ok=True)


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Outcome of the content-validation gate."""

    ok: bool
    errors: tuple[dict[str, str], ...] = ()


def _is_reserved(target_path: str) -> bool:
    return PurePosixPath(target_path).name in RESERVED


def validate_postimage(
    *,
    target_path: str,
    postimage: str,
    idx: Index | None,
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
    - **Duplicate / forged id.** A frontmatter ``id`` that already belongs to a
      DIFFERENT path in the live index. A duplicate id makes every subsequent
      index rebuild raise ``DuplicateIdError``, i.e. one bad write puts the server
      in a persistent degraded state. Rejected as ``duplicate_id``. An id that
      resolves to the SAME path (a legitimate edit-in-place) is allowed.

    Reserved filenames (``index.md`` / ``log.md`` / ``template.md``) are exempt
    from the concept schema (SPEC section 4.2), matching ``kb lint``.
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

    if _is_reserved(target_path):
        # Reserved files are schema-exempt; the parse check above is the only gate.
        return ValidationResult(ok=True)

    # 2. Enum values, when present, must be in vocabulary.
    for field, allowed in (("type", TYPES), ("status", _WRITE_STATUSES),
                           ("tier", TIERS)):
        value = fm.get(field)
        if value is not None and value not in allowed:
            errors.append({
                "field": field, "code": "invalid_enum",
                "message": f"invalid {field} '{value}' "
                           f"(allowed: {sorted(allowed)})",
            })

    # 3. Duplicate / forged id against the live index. A duplicate id at a
    # DIFFERENT path corrupts the next rebuild.
    doc_id = fm.get("id")
    if isinstance(doc_id, str) and doc_id and idx is not None:
        try:
            existing = idx.get(doc_id)
        except Exception:  # noqa: BLE001 - index read must never fail the gate open
            existing = None
        if existing is not None and existing.path != target_path:
            errors.append({
                "field": "id", "code": "duplicate_id",
                "message": f"id '{doc_id}' already used by '{existing.path}'",
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
