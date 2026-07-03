"""Unit tests for the REST status -> HTTP code maps (write-pipeline core +
onboarding seam statuses)."""
from __future__ import annotations

from data_olympus.rest_api import _propose_status, _resolve_status


def test_propose_status_write_pipeline_codes() -> None:
    assert _propose_status("committed") == 201
    assert _propose_status("pending_confirmation") == 202
    assert _propose_status("rejected_payload_too_large") == 413
    assert _propose_status("rejected_rate_limited") == 429
    assert _propose_status("rejected_pending_queue_full") == 429
    assert _propose_status("rejected_stale_base") == 409
    assert _propose_status("rejected_path_lock_busy") == 409
    assert _propose_status("rejected_invalid_document") == 422
    assert _propose_status("rejected_something_else") == 400


def test_propose_status_onboarding_seam_codes() -> None:
    # Seams folded in from the onboarding package.
    assert _propose_status("rejected_already_in_progress") == 409
    assert _propose_status("rejected_path_locked") == 423


def test_resolve_status_codes() -> None:
    assert _resolve_status("committed") == 200
    assert _resolve_status("rejected_edited_text_too_large") == 413
    assert _resolve_status("already_resolved") == 409
    assert _resolve_status("rejected_stale_base") == 409
    assert _resolve_status("rejected_invalid_document") == 422
    assert _resolve_status("rejected") == 200
