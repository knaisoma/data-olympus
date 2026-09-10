"""Pending queue: full postimage + CAS metadata + same-path lock.

Low-confidence proposals enter the pending queue rather
than committing immediately. The operator approves or rejects (or edits the
text and approves). On approve, the resolved record carries enough metadata
for the caller to commit it cleanly through the audit-trailer pipeline.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager

from data_olympus.durable import atomic_remove, atomic_write_json

# The exact shape of ``uuid.uuid4().hex`` (32 lowercase hex chars). Used to reject
# path-traversal in a client-supplied pending_id before it hits the disk join.
_PENDING_ID_RE = re.compile(r"[0-9a-f]{32}")


class PathLockBusyError(Exception):
    """Raised when another pending entry already locks the same target_path."""


class PendingQueueFullError(Exception):
    """Raised when the pending queue is at capacity (KB_PENDING_QUEUE_CAP)."""


class PendingNotFoundError(Exception):
    """Raised when a pending_id does not resolve to an entry on disk.

    Previously ``get`` let the bare ``FileNotFoundError`` from ``open`` propagate,
    which the REST resolve route surfaced as an opaque HTTP 500. This typed error
    lets the route map an unknown/expired id to a 404 (item 9)."""


class PendingAlreadyResolvedError(Exception):
    """Raised when a pending entry is claimed but has already been claimed by a
    concurrent resolve (item 5). ``approve``/``reject`` are get-then-remove, so two
    concurrent resolves of the same id both saw the entry and both committed. The
    atomic ``claim`` renames the entry to a ``.claimed`` sidecar in one
    ``os.rename`` step; the loser of the race sees ``FileNotFoundError`` and this
    error is raised so exactly one resolve proceeds."""


class ClaimFencedError(Exception):
    """The caller's claim on this entry is no longer the one on disk.

    A resolver can be reclaimed while it waits on the write serializer. If it
    then ran its unconditional finalize or restore it would release a lock and
    delete a sidecar that now belong to a successor. Every completion is
    therefore fenced on the ``claim_token`` stamped at claim time, and a caller
    whose token no longer matches mutates NOTHING and sees this instead.
    """


@dataclass(frozen=True, slots=True)
class ResolvedPending:
    pending_id: str
    proposal_type: Literal["memory", "edit"]
    target_path: str
    postimage: str
    base_commit: str
    base_blob_sha: str | None
    target_file_hash: str | None
    meta: dict[str, Any]
    # Ownership token for this claim, minted by ``_claim``. Hand it back to
    # ``finalize_resolve`` / ``restore_resolve`` / ``record_outcome`` so those
    # calls are fenced against a reclaim that happened in between.
    claim_token: str = ""


# ``PendingQueue.list`` shadows the builtin inside the class body, so a bare
# ``list[...]`` annotation on any method defined after it resolves to the method
# rather than to the type. This alias keeps those annotations unambiguous.
_Records = list[dict[str, Any]]


def _path_lock_filename(target_path: str) -> str:
    import hashlib
    return hashlib.sha256(target_path.encode("utf-8")).hexdigest() + ".lock"


class PendingQueue:
    def __init__(self, *, pending_root: str, cap: int = 0) -> None:
        self._root = pending_root
        self._cap = cap  # 0 = unlimited
        self._locks_dir = os.path.join(self._root, "locks")
        os.makedirs(self._root, exist_ok=True)
        os.makedirs(self._locks_dir, exist_ok=True)

    @property
    def root(self) -> str:
        """The on-disk directory backing this pending queue. Public so callers
        (e.g. onboarding's pending-root resolution) do not reach into the private
        ``_root`` attribute."""
        return self._root

    def _acquire_lock(
        self,
        target_path: str,
        pending_id: str,
        *,
        owner_kind: str = "pending",
    ) -> float:
        """Create the exclusive lock file for ``target_path``. Returns the
        ``acquired_at`` timestamp stamped into the file so the caller can hand it
        back to ``_release_lock`` for an ownership-checked delete."""
        lock_path = os.path.join(self._locks_dir, _path_lock_filename(target_path))
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PathLockBusyError(target_path) from None
        acquired_at = time.time()
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"pending_id": pending_id, "target_path": target_path,
                           "owner_kind": owner_kind, "acquired_at": acquired_at}, f)
        except Exception:
            os.unlink(lock_path)
            raise
        return acquired_at

    def _release_lock(
        self, target_path: str, *, expected_acquired_at: float | None = None,
    ) -> bool:
        """Delete the lock file for ``target_path``. Returns True iff a file was
        actually unlinked (an ownership-checked call that skips returns False).

        When ``expected_acquired_at`` is given, the delete is ownership-checked so a
        holder or the reclaimer removes ONLY the lock it acquired/inspected, never a
        successor's. Two independent guards:

        - **``acquired_at`` is the ownership token.** It is ``time.time()`` at
          acquisition, unique per acquire to sub-microsecond, so a successor (which
          re-acquires with its own fresh timestamp) never matches the token of the
          lock we meant to delete. A file whose on-disk ``acquired_at`` differs is
          left alone.
        - **inode re-stat closes this call's own open-to-unlink window.** We open
          the file, pin its inode, verify ``acquired_at``, then confirm the path
          still resolves to the SAME inode immediately before ``unlink`` (a
          successor is a fresh ``O_EXCL`` inode). This stops a delete that raced a
          concurrent free+re-acquire between our read and our unlink.

        The residual gap between the final re-stat and ``unlink`` is a single
        syscall and is not closable portably (the stdlib has no ``funlinkat``); it
        is vanishingly narrow next to the minutes-long staleness that gates a
        reclaim, and a successor would still need a colliding ``acquired_at`` (which
        cannot happen) to be wrongly deleted.

        When ``expected_acquired_at`` is ``None`` (the pending queue's own release
        paths, which hold the lock unbroken from enqueue to resolve and are never
        TTL-reclaimed) the delete is unconditional, as before."""
        lock_path = os.path.join(self._locks_dir, _path_lock_filename(target_path))
        if expected_acquired_at is None:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                return False
            return True
        # Ownership-checked delete. Open + fstat pins the inode we verify.
        try:
            fd = os.open(lock_path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            pinned_ino = os.fstat(fd).st_ino
            try:
                info = json.load(os.fdopen(os.dup(fd), "r"))
            except ValueError:
                return False
            if info.get("acquired_at") != expected_acquired_at:
                return False
            # Confirm the path still resolves to the inode we verified (a successor
            # would have a different, freshly-created inode) right before unlinking.
            try:
                if os.stat(lock_path).st_ino != pinned_ino:
                    return False
            except FileNotFoundError:
                return False
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                return False
            return True
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def path_lock(self, target_path: str, *, owner: str) -> Iterator[None]:
        """Advisory per-path lock SHARED between the auto-commit path and the
        pending queue (scope item 1).

        Both surfaces write into the same on-disk ``locks/`` directory keyed by the
        canonical target path, so an auto-commit cannot land on a path that has a
        pending proposal in flight (the later approval would clobber it) and two
        auto-commits to the same path cannot interleave. ``owner`` is a marker
        (e.g. ``auto-commit:<sha_or_session>``) recorded in the lock file for
        operator diagnosis; it is not a pending_id. The lock records
        ``owner_kind="auto_commit"`` so ``reclaim_stale_auto_commit_locks`` can
        safely free it if a crash orphans it (an auto-commit critical section is
        seconds long, so a lock older than the TTL cannot have a live holder).
        Raises :class:`PathLockBusyError` when the path is already locked.
        Releases on exit."""
        acquired_at = self._acquire_lock(
            target_path, owner, owner_kind="auto_commit",
        )
        try:
            yield
        finally:
            # Ownership-checked release: if the lock was TTL-reclaimed while this
            # (slow) holder ran and a successor re-acquired the path, the
            # acquired_at will not match and the successor's lock is left intact.
            self._release_lock(target_path, expected_acquired_at=acquired_at)

    def enqueue(
        self,
        *,
        proposal_type: Literal["memory", "edit"],
        target_path: str,
        postimage: str,
        base_commit: str,
        base_blob_sha: str | None,
        target_file_hash: str | None,
        meta: dict[str, Any],
    ) -> str:
        if self._cap > 0 and self.size() >= self._cap:
            raise PendingQueueFullError(
                f"pending queue at capacity ({self._cap})"
            )
        pending_id = uuid.uuid4().hex
        self._acquire_lock(target_path, pending_id)
        try:
            entry = {
                "pending_id": pending_id,
                "proposal_type": proposal_type,
                "target_path": target_path,
                "postimage": postimage,
                "base_commit": base_commit,
                "base_blob_sha": base_blob_sha,
                "target_file_hash": target_file_hash,
                "meta": meta,
                "enqueued_at": time.time(),
            }
            atomic_write_json(os.path.join(self._root, f"{pending_id}.json"), entry)
        except Exception:
            self._release_lock(target_path)
            raise
        return pending_id

    def list(self) -> list[dict[str, Any]]:
        """Every entry on disk, each labelled with the state it is in.

        Claimed entries are INCLUDED (issue #254). They used to be invisible:
        ``_claim`` renames ``<pid>.json`` to ``<pid>.claimed`` and this method
        read only ``*.json``, so a resolve interrupted after the claim left the
        entry in a state that was neither pending, nor committed, nor listed.
        An operator who approved a batch and then read an empty queue
        reasonably concluded every decision had been applied. The entry is
        still not awaiting a decision, so it does not count towards
        :meth:`size`; it is reported so that a stuck one is visible.
        """
        out: list[dict[str, Any]] = []
        for name in sorted(os.listdir(self._root)):
            if name.endswith(".json"):
                state = "pending"
            elif name.endswith(".claimed"):
                state = "claimed"
            else:
                continue
            path = os.path.join(self._root, name)
            try:
                with open(path) as f:
                    entry = json.load(f)
            except (OSError, json.JSONDecodeError):
                # A concurrent claim renames the file out from under this walk.
                # Skipping is correct: the next pass sees it under its new name.
                continue
            if state == "claimed":
                reconcile = entry.get("reconcile") or {}
                if reconcile.get("state") == "uncertain":
                    state = "uncertain"
            out.append({
                "state": state,
                "pending_id": entry["pending_id"],
                "proposal_type": entry["proposal_type"],
                "target_path": entry["target_path"],
                "confidence": entry["meta"].get("confidence"),
                "agent_identity": entry["meta"].get("agent_identity"),
                "created_at": entry["enqueued_at"],
                # issue #71: surface the secret-scan flag (name only, never
                # the matched value) so an operator sees the warning via
                # `kb pending` without inspecting the raw postimage.
                "secret_scan_flagged": bool(entry["meta"].get("secret_scan_flagged", False)),
                "matching_pattern": entry["meta"].get("matching_pattern"),
                # issue #109: provenance already persisted in meta at enqueue
                # time, simply not surfaced until now. `.get(...)` defaults to
                # None so an entry from before this field existed (or a
                # proposal type that doesn't carry it, e.g. memory has no
                # `reason`) omits it cleanly rather than raising KeyError.
                "source_session": entry["meta"].get("source_session"),
                "reason": entry["meta"].get("reason"),
                "evidence": entry["meta"].get("evidence"),
                # Governed-lane write protection (issue #112): surfaced the
                # same way as secret_scan_flagged above -- already persisted
                # in meta at enqueue time, simply not read until now. None/
                # False for any entry that predates this field or parked for
                # a plain low-confidence reason rather than a demotion.
                "demotion_reason": entry["meta"].get("demotion_reason"),
                "injection_suspect": bool(entry["meta"].get("injection_suspect", False)),
                "injection_patterns": entry["meta"].get("injection_patterns"),
            })
        return out

    def get(self, pending_id: str) -> dict[str, Any]:
        # pending_id reaches here straight from a URL path param; reject anything
        # that is not the hex-uuid shape we mint so a value like
        # ``../../etc/passwd`` can never be interpolated into the on-disk join.
        if not _PENDING_ID_RE.fullmatch(pending_id):
            raise PendingNotFoundError(pending_id)
        path = os.path.join(self._root, f"{pending_id}.json")
        if not os.path.exists(path):
            raise PendingNotFoundError(pending_id)
        with open(path) as f:
            data: dict[str, Any] = json.load(f)
            return data

    def _claim(self, pending_id: str) -> dict[str, Any]:
        """Atomically claim a pending entry so exactly one resolver proceeds
        (item 5). ``approve``/``reject`` were get-then-remove: two concurrent
        resolves of the same id both read the entry and both committed (two
        commits, two audit events, one decision). ``os.rename`` is atomic on a
        POSIX filesystem, so renaming ``<pid>.json`` to ``<pid>.claimed`` is a
        test-and-set: the winner gets the file, the loser hits ``FileNotFoundError``
        and we surface :class:`PendingAlreadyResolvedError`. The claimed sidecar is
        removed by the caller once the entry is fully processed (or on failure it
        lingers as a ``.claimed`` file that ``list``/``size`` ignore and the GC
        cleans up its lock)."""
        if not _PENDING_ID_RE.fullmatch(pending_id):
            raise PendingNotFoundError(pending_id)
        src = os.path.join(self._root, f"{pending_id}.json")
        if not os.path.exists(src):
            raise PendingNotFoundError(pending_id)
        claimed = os.path.join(self._root, f"{pending_id}.claimed")
        try:
            os.rename(src, claimed)
        except FileNotFoundError:
            # Lost the race: another resolver renamed it first between our
            # exists() check and here.
            raise PendingAlreadyResolvedError(pending_id) from None
        with open(claimed) as f:
            data: dict[str, Any] = json.load(f)
        # Stamp the claim EXPLICITLY. The rename alone records neither when the
        # claim happened nor who holds it, and recovery needs both: an age to
        # decide when to look, and a token to fence completion against a
        # reclaim. Written back through the durable helper so a crash here
        # leaves either the pre-stamp or the post-stamp record, never a torn one.
        data["claim"] = {
            "claim_token": uuid.uuid4().hex,
            "claimed_at": time.time(),
        }
        atomic_write_json(claimed, data)
        return data

    def _finish_claim(self, pending_id: str, target_path: str) -> None:
        """Release the path lock and remove the ``.claimed`` sidecar. Called after
        a claimed entry has been fully processed (committed or rejected)."""
        self._release_lock(target_path)
        atomic_remove(os.path.join(self._root, f"{pending_id}.claimed"))

    def _to_resolved(
        self, pending_id: str, entry: dict[str, Any], edited_text: str | None,
    ) -> ResolvedPending:
        postimage = edited_text if edited_text is not None else entry["postimage"]
        return ResolvedPending(
            claim_token=entry.get("claim", {}).get("claim_token", ""),
            pending_id=pending_id,
            proposal_type=entry["proposal_type"],
            target_path=entry["target_path"],
            postimage=postimage,
            base_commit=entry["base_commit"],
            base_blob_sha=entry["base_blob_sha"],
            target_file_hash=entry["target_file_hash"],
            meta=entry["meta"],
        )

    def approve(self, pending_id: str, *, edited_text: str | None = None) -> ResolvedPending:
        """Claim + consume in one step (path lock released, entry removed).

        Kept for callers that commit unconditionally. The gated resolve path uses
        ``claim_for_resolve`` / ``finalize_resolve`` / ``restore_resolve`` instead,
        so a post-claim gate rejection can put the entry back (Codex round-2
        Blocker B)."""
        entry = self._claim(pending_id)
        resolved = self._to_resolved(pending_id, entry, edited_text)
        self._finish_claim(pending_id, entry["target_path"])
        return resolved

    def claim_for_resolve(
        self, pending_id: str, *, edited_text: str | None = None,
    ) -> ResolvedPending:
        """Atomically claim the entry for a GATED resolve, HOLDING the path lock and
        the ``.claimed`` sidecar (Codex round-2 Blocker B).

        Unlike ``approve``, this does NOT release the lock or delete the entry: the
        caller runs the CAS/validation gates and then calls ``finalize_resolve`` on
        success or ``restore_resolve`` on a gate rejection. The path lock is the one
        acquired at ``enqueue`` time and stays held throughout, so no other write
        can grab the path in the window between claim and commit, and the operator's
        proposal is never lost to a post-claim gate rejection."""
        entry = self._claim(pending_id)
        resolved = self._to_resolved(pending_id, entry, edited_text)
        if edited_text is not None:
            # Persist the EFFECTIVE approved bytes. ``_to_resolved`` substitutes
            # ``edited_text`` in memory only, so without this the stored
            # postimage is not what the operator approved and not what would be
            # committed. Recovery must reason about the approved operation, not
            # about the superseded proposal.
            entry["effective_postimage"] = edited_text
            atomic_write_json(
                os.path.join(self._root, f"{pending_id}.claimed"), entry,
            )
        return resolved

    # --- claim records, fencing and outcome reconciliation -------------------

    def claim_record(self, pending_id: str) -> dict[str, Any]:
        """The on-disk ``<pid>.claimed`` document. Raises if there is none."""
        path = os.path.join(self._root, f"{pending_id}.claimed")
        try:
            with open(path) as f:
                entry: dict[str, Any] = json.load(f)
        except FileNotFoundError:
            raise PendingNotFoundError(pending_id) from None
        claim = entry.get("claim", {})
        return {**entry, **claim}

    def _read_claim(self, pending_id: str) -> tuple[str, dict[str, Any]]:
        path = os.path.join(self._root, f"{pending_id}.claimed")
        try:
            with open(path) as f:
                entry: dict[str, Any] = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            raise ClaimFencedError(pending_id) from None
        return path, entry

    def _check_token(self, entry: dict[str, Any], claim_token: str | None) -> None:
        """Fence a completion on the token stamped at claim time.

        ``claim_token=None`` means an unfenced legacy call and is accepted, so a
        caller that predates the token still works; every in-tree caller passes
        one. A token that is present and does NOT match is refused, because the
        entry on disk belongs to somebody else now.
        """
        if claim_token is None:
            return
        if entry.get("claim", {}).get("claim_token") != claim_token:
            raise ClaimFencedError(entry.get("pending_id", ""))

    def record_write_context(
        self, pending_id: str, *, claim_token: str | None,
        session_ref: str, pre_write_ref_sha: str,
    ) -> None:
        """Record where the commit will land, BEFORE the write happens.

        Recovery needs the ref the commit would be on and an immutable pre-write
        sha to search from. Both must be captured before the write, because
        after a crash there is nobody left to capture them.
        """
        path, entry = self._read_claim(pending_id)
        self._check_token(entry, claim_token)
        entry["write_context"] = {
            "session_ref": session_ref,
            "pre_write_ref_sha": pre_write_ref_sha,
        }
        atomic_write_json(path, entry)

    def record_outcome(
        self, pending_id: str, *, claim_token: str | None,
        commit_sha: str | None = None, failure: str | None = None,
        unknown: str | None = None,
    ) -> None:
        """Record what the write actually did, as durable positive evidence.

        Exactly one of ``commit_sha`` (it committed), ``failure`` (it provably
        did not) or ``unknown`` (it may have) must be given. ``unknown`` is a
        real outcome, not an omission: a commit whose result could not be
        observed must never be classified as a proven non-commit, because a
        restore would then re-present a decision that had already been applied.

        Call this BEFORE any post-commit work that could obscure the result.
        """
        given = [x is not None for x in (commit_sha, failure, unknown)]
        if sum(given) != 1:
            raise ValueError(
                "record_outcome takes exactly one of commit_sha, failure, unknown"
            )
        path, entry = self._read_claim(pending_id)
        self._check_token(entry, claim_token)
        if commit_sha is not None:
            outcome = {"outcome": "committed", "commit_sha": commit_sha}
        elif failure is not None:
            outcome = {"outcome": "failed", "reason": failure}
        else:
            outcome = {"outcome": "unknown", "reason": unknown or ""}
        entry["write_outcome"] = {**outcome, "recorded_at": time.time()}
        atomic_write_json(path, entry)

    def finalize_resolve(
        self, pending_id: str, target_path: str, *, claim_token: str | None = None,
    ) -> None:
        """Commit succeeded: release the path lock and remove the claimed entry.

        Fenced on ``claim_token``: a resolver that was reclaimed while it waited
        on the write serializer would otherwise release a successor's lock and
        delete a successor's sidecar.
        """
        if claim_token is not None:
            _path, entry = self._read_claim(pending_id)
            self._check_token(entry, claim_token)
        self._finish_claim(pending_id, target_path)

    def restore_resolve(
        self, pending_id: str, *, claim_token: str | None = None,
    ) -> None:
        """A gate rejected the claimed entry: rename ``<pid>.claimed`` back to
        ``<pid>.json`` so the operator can re-resolve it, keeping the path lock
        held (it was never released). Idempotent: a missing sidecar is a no-op
        for an unfenced call. Fenced on ``claim_token`` for the same reason as
        :meth:`finalize_resolve`."""
        claimed = os.path.join(self._root, f"{pending_id}.claimed")
        live = os.path.join(self._root, f"{pending_id}.json")
        if claim_token is not None:
            _path, entry = self._read_claim(pending_id)
            self._check_token(entry, claim_token)
            entry.pop("claim", None)
            entry.pop("write_outcome", None)
            entry.pop("write_context", None)
            entry.pop("reconcile", None)
            atomic_write_json(live, entry)
            atomic_remove(claimed)
            return
        with contextlib.suppress(FileNotFoundError):
            os.rename(claimed, live)

    def claims_at_risk(self, session_ref: str) -> _Records:
        """Claims on ``session_ref`` whose evidence a history rewrite would
        destroy.

        A rebase can drop a commit upstream already acquired as an equivalent
        patch, a squash can rewrite its trailer away, and deleting the branch
        removes it outright. Any of those turns "this decision committed" into
        an unanswerable question, so the operations that do them reconcile
        first and defer while this is non-empty.

        Only claims that have STARTED their write count. A claim with no
        recorded write context has no commit to lose, and counting it would
        deadlock a resolve's own base refresh against its own claim, since that
        refresh runs before the write.
        """
        if not session_ref or not os.path.isdir(self._root):
            return []
        out: _Records = []
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".claimed"):
                continue
            try:
                with open(os.path.join(self._root, name)) as f:
                    entry = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            context = entry.get("write_context") or {}
            if context.get("session_ref") != session_ref:
                continue
            out.append({
                "pending_id": entry.get("pending_id"),
                "target_path": entry.get("target_path"),
                "session_ref": session_ref,
            })
        return out

    def reconcile_claims(
        self,
        *,
        min_age_sec: float,
        find_commit: Callable[[dict[str, Any]], bool | None] | None = None,
        now: float | None = None,
        serializer: AbstractContextManager[Any] | None = None,
    ) -> _Records:
        """Resolve claims that outlived a live resolver, on EVIDENCE not on age.

        A ``<pid>.claimed`` sidecar is unreachable by every other reaper: it is
        excluded from :meth:`size`, ``gc_orphan_locks`` treats it as a
        legitimate lock holder, and pending-owned locks are never TTL-reclaimed.
        So an interrupted resolve holds its path lock forever and every write to
        that path is refused (issues #253, #254).

        ``min_age_sec`` only decides WHICH claims to look at. It never decides
        the outcome. The outcome comes from durable evidence, and the default is
        to do nothing:

        - a recorded commit sha means the decision committed. Finalize it, and
          never present it again: re-resolving a committed entry duplicates it.
        - a recorded failure means it provably did not commit. Restore it to
          pending, keeping the path lock, so the operator can re-resolve.
        - otherwise the outcome was never durably recorded, which covers a crash
          before git's exit was observed, a failed sha lookup, and a failure to
          persist the record. ``find_commit`` may search for the claim-linked
          commit; True means it committed. ANYTHING ELSE is ``uncertain``: the
          entry keeps its lock, is reported with a reason, and is retried on the
          next sweep so a transient fault heals itself.

        A negative search is deliberately NOT a restore. A rebase or a squash can
        drop the trailer-bearing commit while leaving a perfectly readable
        branch, so absence of evidence would otherwise re-present a decision that
        had already been applied.
        """
        if serializer is not None:
            with serializer:
                return self._reconcile_claims_locked(min_age_sec, find_commit, now)
        return self._reconcile_claims_locked(min_age_sec, find_commit, now)

    def _reconcile_claims_locked(
        self,
        min_age_sec: float,
        find_commit: Callable[[dict[str, Any]], bool | None] | None,
        now: float | None,
    ) -> _Records:
        if not os.path.isdir(self._root):
            return []
        moment = time.time() if now is None else now
        results: _Records = []
        for name in sorted(os.listdir(self._root)):
            if not name.endswith(".claimed"):
                continue
            pending_id = name[: -len(".claimed")]
            try:
                _path, entry = self._read_claim(pending_id)
            except ClaimFencedError:
                continue
            claim = entry.get("claim", {})
            claimed_at = claim.get("claimed_at")
            if not isinstance(claimed_at, (int, float)):
                # A sidecar with no claim stamp predates this record. Treat it
                # as arbitrarily old: it cannot belong to a live resolver in
                # this process, and leaving it is the wedge being fixed.
                claimed_at = 0.0
            if moment - claimed_at < min_age_sec:
                continue
            results.append(
                self._reconcile_one(pending_id, entry, find_commit, moment)
            )
        return results

    def _reconcile_one(
        self,
        pending_id: str,
        entry: dict[str, Any],
        find_commit: Callable[[dict[str, Any]], bool | None] | None,
        moment: float,
    ) -> dict[str, Any]:
        target_path = entry["target_path"]
        token = entry.get("claim", {}).get("claim_token")
        outcome = entry.get("write_outcome") or {}
        kind = outcome.get("outcome")
        base = {"pending_id": pending_id, "target_path": target_path}

        if kind == "committed":
            self.finalize_resolve(pending_id, target_path, claim_token=token)
            return {**base, "outcome": "committed",
                    "commit_sha": outcome.get("commit_sha"),
                    "reason": "the write recorded a durable commit"}
        if kind == "failed":
            self.restore_resolve(pending_id, claim_token=token)
            return {**base, "outcome": "restored",
                    "reason": outcome.get("reason")
                              or "the write recorded a failure before committing"}

        record = {
            **{k: v for k, v in entry.items() if k != "claim"},
            **entry.get("claim", {}),
            **entry.get("write_context", {}),
            "pending_id": pending_id,
        }
        found = None
        search_error = ""
        if find_commit is not None:
            try:
                found = find_commit(record)
            except Exception as exc:  # noqa: BLE001 - a failed search is uncertainty
                search_error = f"{type(exc).__name__}: {exc}"
        if found is True:
            self.finalize_resolve(pending_id, target_path, claim_token=token)
            return {**base, "outcome": "committed",
                    "reason": "the claim-linked commit was found in the "
                              "recorded ref"}

        reason = (
            "no durable outcome was recorded and the claim-linked commit could "
            "not be found, so it is not known whether this decision committed. "
            "The path lock is retained deliberately; reconciliation is retried "
            "on every sweep."
        )
        if search_error:
            reason += f" Search error: {search_error}"
        elif find_commit is None:
            reason = (
                "no durable outcome was recorded and no commit search was "
                "available, so it is not known whether this decision committed. "
                "The path lock is retained deliberately."
            )
        entry["reconcile"] = {"state": "uncertain", "reason": reason,
                              "last_checked_at": moment}
        atomic_write_json(
            os.path.join(self._root, f"{pending_id}.claimed"), entry,
        )
        return {**base, "outcome": "uncertain", "reason": reason}

    def reject(self, pending_id: str) -> None:
        entry = self._claim(pending_id)
        self._finish_claim(pending_id, entry["target_path"])

    def gc_orphan_locks(self) -> int:
        """Remove lock files whose ``pending_id`` has no live entry (item 5).

        A crash between ``_acquire_lock`` and the entry write in ``enqueue`` leaves
        the path locked forever with no entry to release it, so every future
        proposal to that path is rejected ``rejected_path_lock_busy``. Each lock
        file records the ``pending_id`` that holds it; if neither ``<pid>.json``
        nor ``<pid>.claimed`` exists, the lock is orphaned and is removed. Locks
        held by the shared auto-commit path (``owner_kind == "auto_commit"``) are
        left alone HERE: they have no pending entry to key off, so a hung/crashed
        auto-commit lock is reclaimed by the age-bounded
        :meth:`reclaim_stale_auto_commit_locks` instead. Returns the number of
        locks removed."""
        if not os.path.isdir(self._locks_dir):
            return 0
        removed = 0
        for name in os.listdir(self._locks_dir):
            if not name.endswith(".lock"):
                continue
            lock_path = os.path.join(self._locks_dir, name)
            try:
                with open(lock_path) as f:
                    info = json.load(f)
            except (FileNotFoundError, ValueError):
                continue
            holder = info.get("pending_id", "")
            # Only reclaim locks held by a pending entry (uuid holder). A
            # non-uuid holder is the transient auto-commit owner; skip it.
            if not _PENDING_ID_RE.fullmatch(str(holder)):
                continue
            live = os.path.join(self._root, f"{holder}.json")
            claimed = os.path.join(self._root, f"{holder}.claimed")
            if os.path.exists(live) or os.path.exists(claimed):
                continue
            with contextlib.suppress(FileNotFoundError):
                os.unlink(lock_path)
                removed += 1
        return removed

    def reclaim_stale_auto_commit_locks(
        self,
        *,
        max_age_sec: float,
        serializer: AbstractContextManager[Any] | None = None,
    ) -> int:
        """Reclaim auto-commit path locks left behind by a crash.

        An auto-commit takes a per-path lock (``owner_kind == "auto_commit"``) for
        the duration of its write -> git add -> commit critical section, which is
        bounded by the process-wide write serializer and completes in seconds. If
        the process is killed while holding one, the lock file survives on the
        ``/state`` volume and wedges that path forever
        (``rejected_path_lock_busy``): unlike a pending-proposal lock, there is no
        entry to key an orphan-reclaim off, and unlike a clean shutdown the
        ``finally`` in :meth:`path_lock` never ran.

        A lock is reclaimed only when ALL of these hold:

        - it is an auto-commit lock (``owner_kind == "auto_commit"``, or a pre-fix
          legacy lock recognised by its ``auto-commit:`` ``pending_id`` prefix so an
          upgrade on a persistent ``/state`` volume still frees old wedged locks).
          Pending-proposal locks legitimately live until the operator resolves or
          the entry expires, so they are NEVER TTL-reclaimed here;
        - its ``acquired_at`` is older than ``max_age_sec``.

        Concurrency safety. ``serializer`` MUST be the process-wide
        :class:`~data_olympus.write_gate.WriteSerializer` that
        :meth:`path_lock` (via ``tools_write``) already runs its acquire/release
        under. Holding it for the whole reclaim scan makes lock deletion mutually
        exclusive with every auto-commit acquire/release, which is what closes the
        two-deleter race: a stale holder that resumes cannot run its own
        ``path_lock`` release (and no successor can acquire) while the reclaimer is
        mid-scan, so the reclaimer's ``unlink`` can never land on a successor that
        replaced the file after an interleaved free. The inode+``acquired_at``
        ownership check in :meth:`_release_lock` is retained as defense in depth.
        When ``serializer`` is ``None`` (tests, or the startup sweep where the write
        loops have not started so nothing contends) the scan runs without it.

        The age bound is the other half: a real auto-commit critical section is
        seconds long, so any auto-commit lock older than the (minutes-long) TTL
        cannot have a live holder still inside its section.

        Pass ``max_age_sec=0`` to reclaim EVERY auto-commit lock unconditionally;
        the server uses this at startup, where a fresh process provably holds none.
        Returns the number of locks reclaimed."""
        ctx: AbstractContextManager[Any] = (
            serializer if serializer is not None else contextlib.nullcontext()
        )
        with ctx:
            return self._reclaim_stale_auto_commit_locks_locked(max_age_sec)

    def _reclaim_stale_auto_commit_locks_locked(self, max_age_sec: float) -> int:
        if not os.path.isdir(self._locks_dir):
            return 0
        now = time.time()
        removed = 0
        for name in os.listdir(self._locks_dir):
            if not name.endswith(".lock"):
                continue
            lock_path = os.path.join(self._locks_dir, name)
            try:
                with open(lock_path) as f:
                    info = json.load(f)
            except (FileNotFoundError, ValueError):
                continue
            if not self._is_auto_commit_lock(info):
                continue
            target_path = info.get("target_path")
            if not isinstance(target_path, str):
                continue
            acquired_at = info.get("acquired_at")
            if not isinstance(acquired_at, int | float):
                # Malformed/legacy lock with no usable timestamp: treat as stale
                # only in the unconditional (startup) sweep, never on the TTL path.
                if max_age_sec > 0:
                    continue
                if self._lock_still_matches(lock_path, expected=info):
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(lock_path)
                        removed += 1
                continue
            if now - acquired_at <= max_age_sec:
                continue
            # Ownership-checked delete (defense in depth under the serializer):
            # removes ONLY the exact lock we inspected.
            if self._release_lock(target_path, expected_acquired_at=acquired_at):
                removed += 1
        return removed

    @staticmethod
    def _is_auto_commit_lock(info: dict[str, Any]) -> bool:
        """Whether a lock file describes an auto-commit hold (vs a pending
        proposal). New-format locks carry ``owner_kind == "auto_commit"``. Locks
        written by a PRE-fix build have no ``owner_kind`` and stash the auto-commit
        marker in ``pending_id`` (``"auto-commit:<...>"``); those are recognised
        here so an upgrade on a persistent ``/state`` volume still reclaims them.
        A pending-proposal lock always has a 32-hex-char UUID ``pending_id`` and no
        ``auto-commit:`` prefix, so it never matches either arm."""
        if info.get("owner_kind") == "auto_commit":
            return True
        if "owner_kind" in info:
            # A new-format lock that is explicitly some other kind: not ours.
            return False
        holder = info.get("pending_id", "")
        return isinstance(holder, str) and holder.startswith("auto-commit:")

    def _lock_still_matches(self, lock_path: str, *, expected: dict[str, Any]) -> bool:
        """True if the lock file at ``lock_path`` still holds exactly ``expected``
        (used for the timestamp-less legacy-lock reclaim, where there is no
        ``acquired_at`` to key an ownership check off)."""
        try:
            with open(lock_path) as f:
                return bool(json.load(f) == expected)
        except (FileNotFoundError, ValueError):
            return False

    def would_exceed(self, n: int) -> bool:
        """True if enqueuing ``n`` more entries would exceed the cap (0 = unlimited).

        Lets a multi-file caller (onboarding bootstrap) check capacity up front so
        it can reject atomically instead of enqueuing a partial bundle."""
        return self._cap > 0 and self.size() + n > self._cap

    def size(self) -> int:
        return sum(
            1 for f in os.listdir(self._root)
            if f.endswith(".json") and not f.startswith("locks")
        )

    def locks_held(self) -> int:
        if not os.path.isdir(self._locks_dir):
            return 0
        return sum(1 for f in os.listdir(self._locks_dir) if f.endswith(".lock"))

    def held_locks(self) -> _Records:
        """Which paths are locked, by whom, and for how long (issue #253).

        ``locks_held`` is a bare count, and a count of 1 gave an operator no way
        to tell WHICH path was wedged: that had to be found by exec-ing into the
        pod and reading the state volume. Each record carries the target path,
        the owner kind, the acquiring pending id and the lock's age.
        """
        if not os.path.isdir(self._locks_dir):
            return []
        now = time.time()
        out: _Records = []
        for name in sorted(os.listdir(self._locks_dir)):
            if not name.endswith(".lock"):
                continue
            try:
                with open(os.path.join(self._locks_dir, name)) as f:
                    info = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            acquired_at = info.get("acquired_at")
            out.append({
                "target_path": info.get("target_path"),
                "owner_kind": info.get("owner_kind", "pending"),
                "pending_id": info.get("pending_id"),
                "acquired_at": acquired_at,
                "age_seconds": (
                    max(0.0, now - acquired_at)
                    if isinstance(acquired_at, (int, float))
                    else None
                ),
            })
        return out
