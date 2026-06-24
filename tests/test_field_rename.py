"""Regression tests for the doc_id -> id field rename in slice 2B."""
from __future__ import annotations

from data_olympus.models import SearchHitModel


def test_search_hit_model_has_id_field() -> None:
    fields = set(SearchHitModel.model_fields.keys())
    assert "id" in fields, f"SearchHitModel must have 'id' field; got {fields}"
    assert "doc_id" not in fields, (
        f"SearchHitModel must NOT have 'doc_id' field (renamed to 'id'); got {fields}"
    )


def test_search_hit_model_id_is_required_string() -> None:
    info = SearchHitModel.model_fields["id"]
    assert info.is_required(), "SearchHitModel.id must be required"
    # The annotation is `str` (no default). Just constructing should require it.
