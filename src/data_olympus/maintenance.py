"""Maintenance ledger: corpus-state audit + committed status doc (issue #113).

Computed at every index build (the build already walks the corpus, so this is
nearly free): which docs are missing a ``status`` field (the migration vehicle
for making ``status`` mandatory), and which docs have recently expired or are
expiring soon (issue #107 validity data). The computed :class:`MaintenanceState`
is cached on the ``Index`` instance (``Index.maintenance_state``) and consumed
two ways:

- :func:`pending_actions_for` turns it into the ``pending_actions`` CTA
  envelope surfaced on ``kb_consult`` and ``kb_health`` responses (deliberately
  NEVER ``kb_search`` -- per-hit noise trains agents to ignore it).
- :func:`maybe_update_ledger` commits a frontmatter-only markdown doc recording
  the state through the SAME serialized write machinery every other write uses
  (``tools_write.commit_multifile_in_worktree``), and only when the state
  actually changed since the last committed copy (idempotence / no commit
  loop): the comparison is over the STRUCTURED state, never the rendered
  markdown text, so the volatile ``computed_at`` timestamp can never itself
  trigger a spurious re-commit.

This module has no import-time dependency on ``Index`` or the write pipeline
(only under ``TYPE_CHECKING`` / lazy function-local imports), so a caller that
only wants the pure computation (e.g. ``kb_consult_fn``) does not pull in the
git-writing machinery.
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import time as _time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

    from data_olympus.audit_log import AuditLog
    from data_olympus.index import Index
    from data_olympus.pending import PendingQueue
    from data_olympus.push_queue import PushQueue
    from data_olympus.worktrees import WorktreeRegistry
    from data_olympus.write_gate import WriteSerializer

_log = logging.getLogger("data_olympus.maintenance")

# Default committed location, consistent with the generic path taxonomy
# (index._DEFAULT_PATH_RULES maps "tooling/" -> tier "tooling"): a real
# deployment's tooling/ directory is exactly where an ops runbook like this one
# lives (mirrors e.g. a real KB's tooling/data-olympus.md). A private
# deployment using a custom KB_TAXONOMY_PATH must make sure this path still
# resolves inside an INDEXED prefix, or the ledger is committed but never
# searchable/gettable; see docs/operations.md.
DEFAULT_LEDGER_PATH = "tooling/maintenance-ledger.md"
DEFAULT_RECENTLY_EXPIRED_DAYS = 30
DEFAULT_EXPIRING_SOON_DAYS = 30

# Stable id for the committed ledger doc. Excluded by PATH from its own
# missing-status/expiry audit (belt), and always rendered with full, valid
# concept front matter (suspenders) so it also lints clean on its own merits
# rather than relying on the RESERVED-filename exemption (issue #113 design
# note: avoid reserved-filename semantics when a plain doc will do).
LEDGER_ID = "maintenance-ledger"

# Cap applied to every list surfaced in the ledger doc / pending_actions
# envelope: a full corpus scan can be thousands of paths; a bounded sample plus
# a total count is enough for an operator to act on without inflating every
# kb_health / kb_consult response or the committed ledger file itself.
CAP = 50

# System writer identity for the ledger's own commits (server-side, never
# attributable to an agent session). Kept distinct from any real agent
# identity so an audit event or commit trailer is unambiguous about its
# origin.
_MAINTENANCE_SOURCE_SESSION = "system:maintenance-ledger"
_MAINTENANCE_AGENT_IDENTITY = "data-olympus-system"


@dataclass(frozen=True, slots=True)
class DocAuditRow:
    """One document's audit-relevant fields, gathered during the index build's
    single corpus walk (see ``Index.build``)."""

    path: str
    id: str
    status: str
    valid_until: str
    is_reserved: bool


@dataclass(frozen=True, slots=True)
class ExpiryItem:
    """One document surfaced in a recently-expired / expiring-soon bucket."""

    id: str
    path: str
    valid_until: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "path": self.path, "valid_until": self.valid_until}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExpiryItem:
        return cls(
            id=str(data.get("id", "")),
            path=str(data.get("path", "")),
            valid_until=str(data.get("valid_until", "")),
        )


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    """Corpus-state audit computed at index build time.

    A plain frozen dataclass: equality is by VALUE, which is exactly what lets
    :func:`maybe_update_ledger` compare a freshly computed state against the
    state parsed back out of the last committed ledger doc to decide whether a
    new commit is needed. Deliberately carries no ``computed_at`` timestamp --
    that lives only in the rendered markdown (see :func:`render_ledger_markdown`),
    never in this struct, so recomputing an unchanged corpus can never itself
    look "different" and trigger a spurious commit.
    """

    status_present_in_all_kb_entries: bool
    missing_status_paths: tuple[str, ...] = ()
    missing_status_count: int = 0
    recently_expired: tuple[ExpiryItem, ...] = ()
    recently_expired_count: int = 0
    expiring_soon: tuple[ExpiryItem, ...] = ()
    expiring_soon_count: int = 0

    @property
    def is_dirty(self) -> bool:
        """True when there is at least one open maintenance item to surface."""
        return (
            not self.status_present_in_all_kb_entries
            or self.recently_expired_count > 0
            or self.expiring_soon_count > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status_present_in_all_kb_entries": self.status_present_in_all_kb_entries,
            "missing_status": {
                "count": self.missing_status_count,
                "paths": list(self.missing_status_paths),
            },
            "recently_expired": {
                "count": self.recently_expired_count,
                "items": [i.to_dict() for i in self.recently_expired],
            },
            "expiring_soon": {
                "count": self.expiring_soon_count,
                "items": [i.to_dict() for i in self.expiring_soon],
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaintenanceState:
        missing = data.get("missing_status") or {}
        recently = data.get("recently_expired") or {}
        soon = data.get("expiring_soon") or {}
        return cls(
            status_present_in_all_kb_entries=bool(
                data.get("status_present_in_all_kb_entries", True)
            ),
            missing_status_paths=tuple(str(p) for p in missing.get("paths", [])),
            missing_status_count=int(missing.get("count", 0)),
            recently_expired=tuple(
                ExpiryItem.from_dict(i) for i in recently.get("items", [])
            ),
            recently_expired_count=int(recently.get("count", 0)),
            expiring_soon=tuple(
                ExpiryItem.from_dict(i) for i in soon.get("items", [])
            ),
            expiring_soon_count=int(soon.get("count", 0)),
        )


def _add_days(today: str, days: int) -> str:
    """Return ``today`` (ISO YYYY-MM-DD) plus ``days`` calendar days, as ISO."""
    return (datetime.date.fromisoformat(today) + datetime.timedelta(days=days)).isoformat()


def compute_maintenance_state(
    rows: Iterable[DocAuditRow],
    *,
    today: str,
    ledger_path: str,
    recently_expired_days: int = DEFAULT_RECENTLY_EXPIRED_DAYS,
    expiring_soon_days: int = DEFAULT_EXPIRING_SOON_DAYS,
    cap: int = CAP,
) -> MaintenanceState:
    """Pure computation over a corpus snapshot (issue #113).

    ``ledger_path`` is excluded from every bucket: the ledger doc audits the
    REST of the corpus, never itself (it always carries valid frontmatter
    anyway, but this is an explicit belt-and-suspenders guard against a commit
    loop). A reserved filename (index.md/log.md/template.md,
    ``format.validate.RESERVED``) is exempt from the missing-status check only,
    matching the same exemption ``validate_document`` grants those files from
    the full concept schema.

    Windows are inclusive of the boundary day: "recently expired" is
    ``valid_until`` strictly before ``today`` (expired) AND on/after
    ``today - recently_expired_days``; "expiring soon" is ``valid_until`` on/
    after ``today`` (not yet expired) AND on/before ``today +
    expiring_soon_days``.
    """
    from data_olympus.format.validate import is_expired

    missing_status: list[str] = []
    recently_expired: list[ExpiryItem] = []
    expiring_soon: list[ExpiryItem] = []
    recently_cutoff = _add_days(today, -recently_expired_days)
    soon_cutoff = _add_days(today, expiring_soon_days)
    for row in rows:
        if row.path == ledger_path:
            continue
        if not row.is_reserved and not row.status:
            missing_status.append(row.path)
        if not row.valid_until:
            continue
        if is_expired(row.valid_until, today):
            if row.valid_until >= recently_cutoff:
                recently_expired.append(
                    ExpiryItem(id=row.id, path=row.path, valid_until=row.valid_until)
                )
        elif row.valid_until <= soon_cutoff:
            expiring_soon.append(
                ExpiryItem(id=row.id, path=row.path, valid_until=row.valid_until)
            )
    missing_status.sort()
    recently_expired.sort(key=lambda i: (i.valid_until, i.path))
    expiring_soon.sort(key=lambda i: (i.valid_until, i.path))
    return MaintenanceState(
        status_present_in_all_kb_entries=not missing_status,
        missing_status_paths=tuple(missing_status[:cap]),
        missing_status_count=len(missing_status),
        recently_expired=tuple(recently_expired[:cap]),
        recently_expired_count=len(recently_expired),
        expiring_soon=tuple(expiring_soon[:cap]),
        expiring_soon_count=len(expiring_soon),
    )


def render_ledger_markdown(
    state: MaintenanceState, *, computed_at_iso: str,
) -> str:
    """Render the committed ledger doc: full, valid concept frontmatter (so it
    lints clean and is never itself flagged missing-status) plus the
    structured ``maintenance`` block the state round-trips through, and a short
    human-readable body."""
    import yaml

    fm: dict[str, Any] = {
        "id": LEDGER_ID,
        "type": "reference",
        "status": "active",
        "tier": "meta",
        "title": "Data Olympus Maintenance Ledger",
        "description": (
            "Auto-generated corpus maintenance ledger, regenerated on every "
            "index build when the computed state changes. Do not edit by hand."
        ),
        "tags": ["maintenance", "auto-generated"],
        "maintenance": {
            "computed_at": computed_at_iso,
            **state.to_dict(),
        },
    }
    dumped = yaml.safe_dump(
        fm, sort_keys=False, default_flow_style=False, allow_unicode=True
    )
    lines = [
        "# Data Olympus Maintenance Ledger",
        "",
        "Auto-generated by data-olympus at every index build; do not edit by "
        "hand (edits are overwritten the next time the computed state "
        "changes). See docs/operations.md.",
        "",
    ]
    if state.status_present_in_all_kb_entries:
        lines.append("- `status` is present on every indexed document.")
    else:
        lines.append(
            f"- {state.missing_status_count} document(s) are missing a "
            f"`status` field (showing up to {len(state.missing_status_paths)})."
        )
    if state.recently_expired_count:
        lines.append(f"- {state.recently_expired_count} document(s) recently expired.")
    if state.expiring_soon_count:
        lines.append(f"- {state.expiring_soon_count} document(s) are expiring soon.")
    if state.is_dirty:
        lines.append("")
        lines.append(
            "Run an audit session to review and remediate the open item(s) above."
        )
    return "---\n" + dumped + "---\n\n" + "\n".join(lines) + "\n"


def parse_ledger_state(content_markdown: str | None) -> MaintenanceState | None:
    """Parse a previously-committed ledger doc's frontmatter back into a
    MaintenanceState, for the idempotence check in :func:`maybe_update_ledger`.

    Returns None when there is no content, or the ``maintenance`` block is
    absent / unparseable (a fresh deployment with no ledger yet, or a
    hand-edited ledger) -- treated as "no prior state", so the caller commits a
    fresh one rather than crashing.
    """
    if not content_markdown:
        return None
    from data_olympus.format.frontmatter import parse_frontmatter

    try:
        fm, _body = parse_frontmatter(content_markdown)
    except ValueError:
        return None
    maintenance = fm.get("maintenance") if isinstance(fm, dict) else None
    if not isinstance(maintenance, dict):
        return None
    try:
        return MaintenanceState.from_dict(maintenance)
    except Exception:  # noqa: BLE001 - a corrupt/hand-edited block must never crash a build
        return None


def pending_actions_for(
    state: MaintenanceState | None,
) -> list[dict[str, object]] | None:
    """Build the ``pending_actions`` CTA envelope from a MaintenanceState.

    Returns None (the field is OMITTED entirely -- token discipline) when there
    is no state yet, or the state is clean. Each item is a short structured
    ``{kind, message, count}`` dict; the message instructs the model to surface
    the item to the operator and act only on operator confirmation (mirrored in
    the kb_consult / kb_health tool descriptions). Never attached to kb_search.
    """
    if state is None or not state.is_dirty:
        return None
    items: list[dict[str, object]] = []
    if not state.status_present_in_all_kb_entries:
        items.append({
            "kind": "missing_status",
            "message": (
                f"{state.missing_status_count} document(s) are missing a "
                f"'status' field. Surface this to the operator; act only on "
                f"operator confirmation."
            ),
            "count": state.missing_status_count,
        })
    if state.recently_expired_count:
        items.append({
            "kind": "recently_expired",
            "message": (
                f"{state.recently_expired_count} document(s) recently expired. "
                f"Surface this to the operator and suggest running an audit "
                f"session; act only on operator confirmation."
            ),
            "count": state.recently_expired_count,
        })
    if state.expiring_soon_count:
        items.append({
            "kind": "expiring_soon",
            "message": (
                f"{state.expiring_soon_count} document(s) are expiring soon. "
                f"Surface this to the operator and suggest running an audit "
                f"session; act only on operator confirmation."
            ),
            "count": state.expiring_soon_count,
        })
    return items


def _last_committed_in_worktree(
    worktrees: WorktreeRegistry, ledger_path: str,
) -> str | None:
    """The ledger doc's content at the system worktree's HEAD, or None.

    The system worktree is where every ledger commit lands, so its HEAD is the
    authoritative last-committed copy even BEFORE that commit is pushed,
    merged to main, and re-indexed. Consulted as the restart-safe half of the
    duplicate-commit guard (the in-process memo covers the common case without
    a git call; this covers a fresh process whose memo is empty). Best-effort:
    any error reads as "nothing committed yet"."""
    try:
        wt = worktrees.get_or_create(
            source_session=_MAINTENANCE_SOURCE_SESSION,
            agent_identity=_MAINTENANCE_AGENT_IDENTITY,
        )
        return worktrees.git.file_at_commit(
            "HEAD", ledger_path, worktree_path=wt.path,
        )
    except Exception:  # noqa: BLE001 - guard read must never break the caller
        return None


def maybe_update_ledger(
    *,
    idx: Index,
    worktrees: WorktreeRegistry,
    push_queue: PushQueue,
    pending: PendingQueue,
    serializer: WriteSerializer,
    ledger_path: str,
    audit_log: AuditLog | None = None,
    now: float | None = None,
) -> str | None:
    """Best-effort: commit the maintenance ledger doc when the freshly computed
    state (``idx.maintenance_state``) differs from the state recorded in the
    last committed copy. Returns the new commit sha, or None when no commit was
    needed or attempted.

    Duplicate-commit guard: a freshly committed ledger is not visible in the
    INDEX until it is pushed, merged to main, and re-indexed, so comparing
    against the index alone would re-commit the same state on every pull-loop
    tick during that window. Three checks, cheapest first, all comparing the
    STRUCTURED state (never the rendered markdown, whose ``computed_at``
    timestamp changes every render):

    1. ``idx.maintenance_last_committed_state`` -- the in-process memo set
       after each successful commit.
    2. The state parsed from the ledger doc in the live index.
    3. The state parsed from the system worktree's HEAD copy -- the worktree
       survives process restarts (unpushed commits block its GC), so this
       closes the restart window the memo cannot.

    NEVER raises: a commit failure (gate rejection, lock contention, git
    error, ...) is logged (and audited, best-effort) and swallowed, so a
    maintenance-ledger hiccup can never break index refresh or serving
    (issue #113); the memo is NOT set on failure, so the next tick retries.
    Reuses the SAME serialized write/commit machinery every other write goes
    through (``tools_write.commit_multifile_in_worktree``) rather than forking
    a second git-writing path.
    """
    state = idx.maintenance_state
    if state is None:
        return None
    # Guard 1: in-process memo (no I/O).
    if idx.maintenance_last_committed_state == state:
        return None
    # Guard 2: the indexed ledger copy.
    existing = idx.get(LEDGER_ID)
    old_state = parse_ledger_state(existing.content_markdown if existing is not None else None)
    if old_state == state:
        idx.maintenance_last_committed_state = state
        return None
    # Structural path check: the ledger must land inside an indexed prefix or
    # it is committed but never served; a misconfigured
    # KB_MAINTENANCE_LEDGER_PATH is refused loudly instead.
    from data_olympus.auth import is_writable_path, path_rejection_reason
    if not is_writable_path(ledger_path):
        reason = path_rejection_reason(ledger_path)
        _log.warning(
            "maintenance ledger path %r is not a writable indexed path (%s); "
            "skipping the ledger commit -- fix KB_MAINTENANCE_LEDGER_PATH "
            "(and KB_INDEXED_PREFIXES if using a custom taxonomy)",
            ledger_path, reason,
        )
        if audit_log is not None:
            with contextlib.suppress(Exception):
                audit_log.append({
                    "ts": _time.time(), "event_type": "maintenance_ledger",
                    "status": "skipped_bad_path", "target_path": ledger_path,
                    "agent_identity": _MAINTENANCE_AGENT_IDENTITY,
                    "reason": reason,
                })
        return None
    # Guard 3: the system worktree's HEAD copy (restart-safe).
    wt_state = parse_ledger_state(_last_committed_in_worktree(worktrees, ledger_path))
    if wt_state == state:
        idx.maintenance_last_committed_state = state
        return None
    computed_at = datetime.datetime.fromtimestamp(
        now if now is not None else _time.time(), tz=datetime.UTC
    ).isoformat()
    postimage = render_ledger_markdown(state, computed_at_iso=computed_at)

    from data_olympus.index import _classify_by_path
    target_tier, _ = _classify_by_path(ledger_path)

    try:
        from data_olympus.tools_write import commit_multifile_in_worktree
        sha, push_state = commit_multifile_in_worktree(
            worktrees=worktrees, push_queue=push_queue, pending=pending,
            serializer=serializer, idx=idx,
            source_session=_MAINTENANCE_SOURCE_SESSION,
            agent_identity=_MAINTENANCE_AGENT_IDENTITY,
            files=[{"target_path": ledger_path, "postimage": postimage}],
            subject="chore(maintenance): update maintenance ledger",
            target_tier=target_tier,
            target_path_for_msg=ledger_path,
            confidence=1.0,
            push_meta={"source_session": _MAINTENANCE_SOURCE_SESSION,
                       "agent_identity": _MAINTENANCE_AGENT_IDENTITY},
        )
    except Exception as exc:  # noqa: BLE001 - best-effort; never break refresh/serving
        # The memo is deliberately NOT set here, so the next pull-loop tick
        # retries the commit while the state still differs from the last
        # committed copy.
        _log.warning("maintenance ledger commit failed (retried next tick): %s", exc)
        if audit_log is not None:
            with contextlib.suppress(Exception):
                audit_log.append({
                    "ts": _time.time(), "event_type": "maintenance_ledger",
                    "status": "commit_failed", "target_path": ledger_path,
                    "agent_identity": _MAINTENANCE_AGENT_IDENTITY,
                    "reason": str(exc)[:400],
                })
        return None
    # Commit landed: arm the in-process memo so subsequent ticks are no-ops
    # until the state genuinely changes again.
    idx.maintenance_last_committed_state = state
    if audit_log is not None:
        with contextlib.suppress(Exception):
            audit_log.append({
                "ts": _time.time(), "event_type": "maintenance_ledger",
                "status": "committed", "target_path": ledger_path,
                "agent_identity": _MAINTENANCE_AGENT_IDENTITY,
                "commit_sha": sha,
            })
    _log.info("maintenance ledger updated: %s (push_state=%s)", sha, push_state)
    return sha
