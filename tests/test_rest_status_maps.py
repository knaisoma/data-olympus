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


def test_resolve_refusals_are_client_errors_not_200() -> None:
    """A resolve refused for an unencodable identity or edited text must not
    read as success. The map used to fall through to 200 for any status it
    did not name, so rejected_invalid_encoding came back 200 and a client
    checking only the status code would conclude the decision was applied."""
    assert _resolve_status("rejected_invalid_encoding") == 400


def test_propose_encoding_refusal_is_a_client_error() -> None:
    assert _propose_status("rejected_invalid_encoding") == 400
