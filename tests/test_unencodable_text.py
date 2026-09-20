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

import json

import pytest

from data_olympus.pending import PendingQueue
from data_olympus.tools_write import _safe_advisory_text, _validate_evidence

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
