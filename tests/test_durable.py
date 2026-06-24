"""Tests for atomic_write_json + atomic_remove (parent-dir fsync chain)."""
from __future__ import annotations

import json

from data_olympus.durable import atomic_remove, atomic_write_json


def test_atomic_write_json_writes_payload_and_returns(tmp_path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(str(target), {"a": 1, "b": "two"})
    assert target.exists()
    assert json.loads(target.read_text()) == {"a": 1, "b": "two"}


def test_atomic_write_json_overwrites_existing(tmp_path) -> None:
    target = tmp_path / "x.json"
    target.write_text('{"old": true}')
    atomic_write_json(str(target), {"new": True})
    assert json.loads(target.read_text()) == {"new": True}


def test_atomic_write_json_leaves_no_tmp_after_success(tmp_path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(str(target), {"k": "v"})
    survivors = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not any("x.json.tmp" in s for s in survivors), survivors


def test_atomic_remove_deletes_file(tmp_path) -> None:
    target = tmp_path / "x.json"
    target.write_text("{}")
    atomic_remove(str(target))
    assert not target.exists()


def test_atomic_remove_missing_is_noop(tmp_path) -> None:
    target = tmp_path / "never-existed.json"
    atomic_remove(str(target))  # must not raise
    assert not target.exists()


def test_atomic_write_creates_parent_if_needed_via_caller(tmp_path) -> None:
    """atomic_write_json does NOT create parent dirs; the caller is responsible.
    This test pins the contract: writing to a non-existent parent raises."""
    target = tmp_path / "subdir" / "x.json"
    import pytest
    with pytest.raises(FileNotFoundError):
        atomic_write_json(str(target), {"k": "v"})


def test_atomic_write_then_remove_cycle_leaves_clean_directory(tmp_path) -> None:
    target = tmp_path / "x.json"
    atomic_write_json(str(target), {"k": "v"})
    atomic_remove(str(target))
    assert list(tmp_path.iterdir()) == []
