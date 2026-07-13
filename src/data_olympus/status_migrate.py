"""Explicit status-autofill persistence lane (issue #147 / KNA-69).

The index-build autofill (see ``Index.build`` / ``Config.status_autofill``) is
VIRTUAL: it treats a legacy doc missing ``status`` as ``active`` IN MEMORY so a
pre-0.4.0 corpus keeps its in-force docs after upgrade, but never writes to the
markdown source. This module is the SECOND, EXPLICIT lane: an operator invokes
``data-olympus migrate status --apply`` to write the ``status`` field into the
physical markdown files so the corpus goes lint-clean and the maintenance ledger
stops nagging.

Design notes:

- It is a SYSTEM write, not an agent write: it rewrites files on disk directly
  (via the same ``render_document`` serializer the importer uses) rather than
  going through the governed-lane pending route. An operator ran it on purpose.
- Idempotent: a doc that already has a non-empty ``status`` is left untouched, so
  a second ``--apply`` run rewrites nothing. Re-running is a no-op.
- Audited: every applied change is recorded (one event per file) through the
  same tamper-evident :class:`~data_olympus.audit_log.AuditLog` chain the server
  uses, so the migration is attributable.
- Targets exactly the files the maintenance ledger flags: it reuses
  :func:`data_olympus.format.discover_bundle_files` (the single source of truth
  for concept files, which already excludes reserved filenames), so autofill and
  the ledger's missing-status audit never drift.

Default value ``active`` preserves the pre-0.4.0 in-force behavior and is kept in
sync with ``index.DEFAULT_AUTOFILL_STATUS`` (asserted in the tests).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from data_olympus.format import discover_bundle_files
from data_olympus.format.document import Document
from data_olympus.importer.stamp import render_document

if TYPE_CHECKING:
    from pathlib import Path

    from data_olympus.audit_log import AuditLog

# The status written to disk for a legacy doc missing ``status``. Kept in sync
# with ``index.DEFAULT_AUTOFILL_STATUS`` (the VIRTUAL lane's value) so the two
# lanes converge on the SAME final state: whether the operator relies on virtual
# autofill or persists it, a status-less legacy doc ends up ``active``.
DEFAULT_STATUS = "active"

# Position for the injected ``status`` key in the rewritten frontmatter. The
# schema order (importer.stamp.build_frontmatter) is id, type, status, tier, ...
# so a doc that has id/type but no status gets status re-inserted right after
# ``type``; if neither is present it lands first. This keeps a migrated doc's
# frontmatter in the same field order as a freshly authored one.
_STATUS_AFTER = ("id", "type")


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Outcome of a status-autofill migration over a corpus."""

    scanned: int
    changed_paths: tuple[str, ...] = ()
    already_present: int = 0
    skipped_malformed: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        return len(self.changed_paths)


def _insert_status(frontmatter: dict[str, object], status: str) -> dict[str, object]:
    """Return a new frontmatter mapping with ``status`` inserted in schema order.

    Rebuilds the dict so ``status`` lands right after ``type`` (or ``id`` if no
    ``type``), matching ``importer.stamp.build_frontmatter``'s field order, rather
    than appending it at the end. Every other key keeps its original relative
    order, so a migrated doc reads like a freshly authored one.
    """
    keys = list(frontmatter)
    anchor_positions = [keys.index(k) for k in _STATUS_AFTER if k in keys]
    if not anchor_positions:
        # No id/type anchor at all: status leads the frontmatter.
        return {"status": status, **frontmatter}
    insert_after = max(anchor_positions)
    out: dict[str, object] = {}
    for i, key in enumerate(keys):
        out[key] = frontmatter[key]
        if i == insert_after:
            out["status"] = status
    return out


def apply_status_autofill(
    corpus_root: Path,
    *,
    default_status: str = DEFAULT_STATUS,
    audit_log: AuditLog | None = None,
    agent_identity: str = "data-olympus-system",
    source_session: str = "system:status-migrate",
) -> MigrationResult:
    """Write ``default_status`` into every concept doc missing a ``status`` field.

    Read-modify-write per file over the concept files under ``corpus_root`` (the
    same set :func:`discover_bundle_files` returns, which excludes reserved
    filenames). A file that already has a non-empty ``status`` is left byte-for-
    byte untouched (idempotence), as is a file whose frontmatter cannot be parsed
    (a lint concern, surfaced in ``skipped_malformed``, never silently rewritten).

    When ``audit_log`` is given, one ``status_migrate`` event is appended per
    changed file so the migration is attributable through the tamper-evident
    chain. Returns a :class:`MigrationResult` summarising the run.
    """
    scanned = 0
    changed: list[str] = []
    already_present = 0
    skipped_malformed: list[str] = []

    for md in discover_bundle_files(corpus_root):
        scanned += 1
        rel = str(md.relative_to(corpus_root))
        try:
            doc = Document.load(md)
        except ValueError:
            # Malformed frontmatter: a `kb lint` concern. Never rewrite blindly.
            skipped_malformed.append(rel)
            continue
        status = doc.frontmatter.get("status")
        if isinstance(status, str) and status.strip():
            already_present += 1
            continue
        new_fm = _insert_status(doc.frontmatter, default_status)
        md.write_text(render_document(new_fm, doc.body), encoding="utf-8")
        changed.append(rel)
        if audit_log is not None:
            audit_log.append({
                "ts": time.time(),
                "event_type": "status_migrate",
                "status": "committed",
                "target_path": rel,
                "agent_identity": agent_identity,
                "source_session": source_session,
                "value": default_status,
            })

    return MigrationResult(
        scanned=scanned,
        changed_paths=tuple(changed),
        already_present=already_present,
        skipped_malformed=tuple(skipped_malformed),
    )
