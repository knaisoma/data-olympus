"""Unencodable (lone-surrogate) advisory text must never reach the pending listing.

A caller-supplied string carrying an unpaired surrogate survives every existing
gate on the propose path: ``json.loads`` accepts the ``\\udXXX`` escape, the
secret scanner is a regex match that does not reject it, and
``durable.atomic_write_json`` serializes with the default ``ensure_ascii=True``
so it persists durably. The failure surfaces later and somewhere else: Starlette
renders ``GET /api/v1/pending`` with ``ensure_ascii=False`` followed by a strict
UTF-8 encode, which raises ``UnicodeEncodeError``. Because the listing is
serialized in one pass, a single poisoned entry hides every other pending
proposal, and the bytes are on disk so it survives a restart.

Two independent defences are asserted here, because either alone is
insufficient:

1. WRITE side: the advisory strings are made safe before they are persisted.
   ``reason`` is sanitized rather than rejected, matching the existing
   secret-scan treatment (a flagged reason is replaced with a note, because
   reason is advisory metadata rather than committed content). ``evidence``
   is rejected, matching its existing validator.
2. READ side: an entry that is ALREADY on disk from before this fix, or
   written by anything else, must degrade to a placeholder instead of taking
   the whole listing down with it.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

from data_olympus.pending import PendingQueue
from data_olympus.tools_write import (
    _reject_unencodable_identity,
    _safe_advisory_text,
    _validate_evidence,
)

LONE_SURROGATE = "\ud800"


def _renders_like_starlette(payload: object) -> bytes:
    """Exactly what starlette's JSONResponse.render does."""
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False, indent=None,
        separators=(",", ":"),
    ).encode("utf-8")


def test_lone_surrogate_breaks_the_response_encoder_but_not_persistence():
    """Pin the underlying asymmetry this whole module exists for.

    If this ever stops holding, the defences below are protecting against
    something that no longer happens and should be re-justified rather than
    silently kept.
    """
    # Accepted on input, and indistinguishable from ordinary text by length.
    assert json.loads('{"r": "\\ud800"}')["r"] == LONE_SURROGATE
    assert len(LONE_SURROGATE) == 1
    assert LONE_SURROGATE.strip()

    # Persists happily: this is what atomic_write_json does.
    assert json.dumps({"r": LONE_SURROGATE}) == '{"r": "\\ud800"}'

    # Blows up only on the response path.
    with pytest.raises(UnicodeEncodeError):
        _renders_like_starlette({"r": LONE_SURROGATE})


class TestSafeAdvisoryText:
    def test_ordinary_text_is_returned_unchanged(self):
        for value in ("plain", "accented éàü", "emoji 🙂", "cjk 知識", ""):
            assert _safe_advisory_text(value) == value

    def test_paired_surrogates_are_not_mangled(self):
        """A correctly-paired astral character encodes fine and must survive.

        Python stores it as a single code point, so this is really a guard
        against a naive implementation that rejects anything in the surrogate
        RANGE rather than testing encodability.
        """
        astral = "\U0001F600"
        assert _safe_advisory_text(astral) == astral

    def test_lone_surrogate_is_replaced_with_a_safe_note(self):
        out = _safe_advisory_text(LONE_SURROGATE)
        assert out != LONE_SURROGATE
        _renders_like_starlette({"r": out})  # must not raise
        assert "not encodable" in out

    def test_lone_surrogate_embedded_in_otherwise_valid_text(self):
        out = _safe_advisory_text("before" + LONE_SURROGATE + "after")
        _renders_like_starlette({"r": out})
        # Replaced wholesale rather than partially, so no caller-controlled
        # fragment is carried forward at all.
        assert "before" not in out


class TestEvidenceRejectsUnencodable:
    def test_unencodable_evidence_item_is_rejected(self):
        err = _validate_evidence(["fine", LONE_SURROGATE])
        assert err is not None
        assert "encodable" in err
        # The rejection reason must not itself carry the bad character, or the
        # error response becomes the new poisoned payload.
        _renders_like_starlette({"reason": err})

    def test_valid_evidence_still_accepted(self):
        assert _validate_evidence(["fine", "also fine 🙂"]) is None


class TestListingSurvivesAPoisonedRecordAlreadyOnDisk:
    def test_one_bad_entry_does_not_hide_the_others(self, tmp_path):
        """The read-side defence, which is the one that matters for records
        written before this fix shipped."""
        root = tmp_path / "pending"
        root.mkdir()
        q = PendingQueue(pending_root=str(root))

        good = {
            "pending_id": "20260920T120000Z-aaaaaaaa",
            "proposal_type": "edit", "target_path": "a.md", "postimage": "x",
            "meta": {"reason": "ordinary"}, "enqueued_at": 1.0,
        }
        poisoned = {
            "pending_id": "20260920T120001Z-bbbbbbbb",
            "proposal_type": "edit", "target_path": "b.md", "postimage": "x",
            "meta": {"reason": LONE_SURROGATE}, "enqueued_at": 2.0,
        }
        for entry in (good, poisoned):
            path = root / f"{entry['pending_id']}.json"
            # ensure_ascii=True, exactly as atomic_write_json persists.
            path.write_text(json.dumps(entry), encoding="utf-8")

        listed = q.list()
        assert len(listed) == 2, "the poisoned record must not be dropped"

        # The whole listing must render, which is the actual failure being fixed.
        _renders_like_starlette(listed)

        by_path = {e["target_path"]: e for e in listed}
        assert by_path["a.md"]["reason"] == "ordinary", "untouched neighbour"
        assert by_path["b.md"]["reason"] != LONE_SURROGATE


class TestIdentityFieldsAreRejectedNotSanitized:
    """The audit-suppression hole, which is the serious one.

    _emit_audit wraps its append in contextlib.suppress(Exception) so audit
    trouble never fails a write, and AuditLog._canonical serializes with
    ensure_ascii=False before _digest encodes strictly. An unpaired surrogate
    in agent_identity therefore raised INSIDE the suppressed block: the write
    went through and no audit record was written for it. That is audit
    evasion by a caller who merely controls their own identity string, so
    these fields are rejected outright rather than replaced.
    """

    def test_the_audit_digest_really_does_raise_on_a_surrogate(self):
        """Pin the mechanism, so the rejection below is not cargo-culted."""
        body = {"agent_identity": LONE_SURROGATE, "event_type": "propose_edit"}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False)
        with pytest.raises(UnicodeEncodeError):
            hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def test_unencodable_agent_identity_is_named_but_not_echoed(self):
        err = _reject_unencodable_identity(agent_identity=LONE_SURROGATE)
        assert err is not None
        assert "agent_identity" in err
        assert LONE_SURROGATE not in err
        _renders_like_starlette({"reason": err})

    def test_each_identity_field_is_checked(self):
        for field in ("agent_identity", "source_session", "target_path", "pending_id"):
            err = _reject_unencodable_identity(**{field: LONE_SURROGATE})
            assert err is not None and field in err, field

    def test_ordinary_and_astral_identities_pass(self):
        assert _reject_unencodable_identity(
            agent_identity="agent-1", source_session="sess-1",
            target_path="decisions/D-1.md", edited_text="fine \U0001F600",
        ) is None

    def test_none_fields_pass(self):
        assert _reject_unencodable_identity(agent_identity=None, edited_text=None) is None


class TestDetailResponseSurvivesALegacyPoisonedRecord:
    def test_reason_is_placeheld_rather_than_breaking_the_route(self):
        """GET /api/v1/pending/{id} reads meta["reason"] directly, so the
        listing fix alone did not cover it."""
        from data_olympus.pending import _render_safe
        assert _render_safe({"reason": LONE_SURROGATE})["reason"] != LONE_SURROGATE
        _renders_like_starlette(_render_safe({"reason": LONE_SURROGATE}))


class TestHealthLockProjectionSurvivesAPoisonedLock:
    def test_auto_commit_owner_does_not_break_health(self, tmp_path):
        """An auto-commit lock records "auto-commit:<source_session>" as its
        pending_id, so caller text reaches health through held_locks()."""
        root = tmp_path / "pending"
        root.mkdir()
        q = PendingQueue(pending_root=str(root))
        locks = os.path.join(str(root), "locks")
        os.makedirs(locks, exist_ok=True)
        with open(os.path.join(locks, "deadbeef.lock"), "w", encoding="utf-8") as f:
            json.dump({
                "pending_id": "auto-commit:" + LONE_SURROGATE,
                "target_path": "a.md", "owner_kind": "auto_commit",
                "acquired_at": 1.0,
            }, f)

        records = q.held_locks()
        assert len(records) == 1
        _renders_like_starlette(records)
        assert LONE_SURROGATE not in records[0]["pending_id"]


class TestGuardPlacementThroughTheRealEntryPoints:
    """Regression cover for WHERE the guard sits, not just that it exists.

    The previous version of these tests called the helper directly, so
    deleting every production call site left them green. These drive the real
    functions with recording collaborators and assert that a rejection touches
    NOTHING: no audit attempt, no queue write, no lock, no worktree.
    """

    def _recorders(self):
        calls: list[str] = []

        class _Audit:
            def append(self, event):  # noqa: ARG002
                calls.append("audit")

        class _Pending:
            def enqueue(self, **kw):  # noqa: ARG002
                calls.append("enqueue")
                raise AssertionError("must not be reached")

            def size(self):
                return 0

        return calls, _Audit(), _Pending()

    def test_edit_rejects_before_touching_anything(self):
        from data_olympus.tools_write import kb_propose_edit_fn
        calls, audit, pending = self._recorders()
        resp = kb_propose_edit_fn(
            target_path="decisions/D-1.md", postimage="x",
            base_commit="abc", base_blob_sha=None, target_file_hash=None,
            reason="r", source_session=LONE_SURROGATE, agent_identity="a",
            confidence=0.1, confidence_threshold=0.85,
            worktrees=None, push_queue=None, pending=pending,  # type: ignore[arg-type]
            rate_limiter=None, blocklist=None, remote_addr="127.0.0.1",  # type: ignore[arg-type]
            audit_log=audit,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_encoding"
        assert calls == [], f"rejection must touch nothing, saw {calls}"
        _renders_like_starlette({"reason": resp.reason})

    def test_edit_rejects_a_nested_non_string_identity(self):
        """The bypass: REST checks presence, not type, so a nested object
        would skip an isinstance-guarded check and still reach the digest."""
        from data_olympus.tools_write import kb_propose_edit_fn
        calls, audit, pending = self._recorders()
        resp = kb_propose_edit_fn(
            target_path="decisions/D-1.md", postimage="x",
            base_commit="abc", base_blob_sha=None, target_file_hash=None,
            reason="r", source_session={"nested": LONE_SURROGATE},  # type: ignore[arg-type]
            agent_identity="a", confidence=0.1, confidence_threshold=0.85,
            worktrees=None, push_queue=None, pending=pending,  # type: ignore[arg-type]
            rate_limiter=None, blocklist=None, remote_addr="127.0.0.1",  # type: ignore[arg-type]
            audit_log=audit,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_encoding"
        assert "must be a string" in (resp.reason or "")
        assert calls == []

    def test_edit_rejects_an_unencodable_base_commit(self):
        """write_gate embeds base_commit verbatim in the CAS rejection reason,
        which is audited."""
        from data_olympus.tools_write import kb_propose_edit_fn
        calls, audit, pending = self._recorders()
        resp = kb_propose_edit_fn(
            target_path="decisions/D-1.md", postimage="x",
            base_commit=LONE_SURROGATE, base_blob_sha=None, target_file_hash=None,
            reason="r", source_session="s", agent_identity="a",
            confidence=0.1, confidence_threshold=0.85,
            worktrees=None, push_queue=None, pending=pending,  # type: ignore[arg-type]
            rate_limiter=None, blocklist=None, remote_addr="127.0.0.1",  # type: ignore[arg-type]
            audit_log=audit,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_encoding"
        assert calls == []

    def test_memory_rejects_before_touching_anything(self):
        from data_olympus.tools_write import kb_propose_memory_fn
        calls, audit, pending = self._recorders()
        resp = kb_propose_memory_fn(
            text="hello", tags=["t"], source_session="s",
            agent_identity=LONE_SURROGATE, confidence=0.1,
            confidence_threshold=0.85,
            worktrees=None, push_queue=None, pending=pending,  # type: ignore[arg-type]
            rate_limiter=None, blocklist=None, remote_addr="127.0.0.1",  # type: ignore[arg-type]
            audit_log=audit,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_encoding"
        assert calls == []

    def test_resolve_rejects_before_touching_anything(self):
        from data_olympus.tools_write import kb_resolve_pending_fn
        calls, audit, pending = self._recorders()
        resp = kb_resolve_pending_fn(
            pending_id="0123456789abcdef0123456789abcdef", decision="reject",
            edited_text=None,
            source_session="s", agent_identity=LONE_SURROGATE,
            worktrees=None, push_queue=None, pending=pending,  # type: ignore[arg-type]
            audit_log=audit,  # type: ignore[arg-type]
        )
        assert resp.status == "rejected_invalid_encoding"
        assert calls == []


class TestDetailRouteThroughTheRealFunction:
    def test_kb_get_pending_fn_renders_a_legacy_poisoned_record(self, tmp_path):
        from data_olympus.tools_write import kb_get_pending_fn
        root = tmp_path / "pending"
        root.mkdir()
        q = PendingQueue(pending_root=str(root))
        pid = "0123456789abcdef0123456789abcdef"
        (root / f"{pid}.json").write_text(json.dumps({
            "pending_id": pid, "proposal_type": "edit",
            "target_path": "a.md", "postimage": "body",
            "enqueued_at": 1.0,
            "meta": {"reason": LONE_SURROGATE, "proposer_principal": "p"},
        }), encoding="utf-8")

        resp = kb_get_pending_fn(
            pending_id=pid, pending=q, principal_name="p", can_resolve=True,
        )
        assert resp.status == "ok"
        assert resp.reason != LONE_SURROGATE
        _renders_like_starlette(resp.model_dump())
