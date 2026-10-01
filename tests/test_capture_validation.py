"""Capture provenance envelope validation on kb_propose_memory (issue #141).

Covers the input contract: every malformed envelope is rejected as
``rejected_invalid_capture`` without echoing a submitted value and without
consuming a rate-limit token; a credential-shaped value anywhere in the
envelope is ``rejected_secret_detected`` with the pattern name only and leaves
nothing enqueued, committed or audited in the clear; and a hostile value can
never forge a top-level frontmatter key.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest
import yaml

from data_olympus.audit_log import AuditLog
from data_olympus.auth import PathBlocklist
from data_olympus.capture import project_capture, reattach_capture, validate_capture
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.tools_write import _render_memory, kb_propose_memory_fn
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    from pathlib import Path

TEXT = "Prefer the staging cluster for load tests."
TEXT_HASH = "sha256:" + hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
EVENT_HASH = "sha256:" + "a" * 64
GITHUB_TOKEN = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz"


def _envelope(**overrides: Any) -> dict[str, Any]:
    env: dict[str, Any] = {
        "capture_source": "claude_code.hook",
        "capture_event_id": "01J9ZK3Q7R8S9T0V1W2X3Y4Z5A",
        "source_event_hash": EVENT_HASH,
        "transformation": "automem.distill/1.4.2",
        "raw_retention": "discarded",
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not _DROP}


_DROP = object()


class _NoTokenLimiter:
    """A limiter that fails the test if a rejected envelope reaches it."""

    calls = 0

    def allow(self, **_kwargs: Any) -> bool:
        type(self).calls += 1
        raise AssertionError("rate limiter consulted for a rejected envelope")


def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _state(tmp_path: Path) -> tuple[WorktreeRegistry, PushQueue, PendingQueue, AuditLog]:
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, env=_env())
    reg = WorktreeRegistry(git=GitOps(repo), worktree_root=str(tmp_path / "wts"))
    pq = PushQueue(queue_root=str(tmp_path / "push-q"))
    pen = PendingQueue(pending_root=str(tmp_path / "pending"))
    audit = AuditLog(log_path=str(tmp_path / "audit.log"), hmac_key="")
    return reg, pq, pen, audit


def _propose(tmp_path: Path, capture: object, *, confidence: float = 0.4,
             limiter: Any = None):
    reg, pq, pen, audit = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text=TEXT, tags=[], source_session="s", agent_identity="claude",
        confidence=confidence, confidence_threshold=0.85,
        worktrees=reg, push_queue=pq, pending=pen,
        rate_limiter=limiter or _NoTokenLimiter(),
        blocklist=PathBlocklist(tier_blocks=[], path_blocks=[]),
        remote_addr="10.0.0.1", audit_log=audit, capture=capture,
    )
    return resp, pen, pq, tmp_path / "audit.log"


# 5 -------------------------------------------------------------------------

_INVALID: list[tuple[str, object]] = [
    ("empty_object", {}),
    ("list", [_envelope()]),
    ("string", "claude_code.hook"),
    ("number", 7),
    ("unknown_key", _envelope(privacy_scope="team")),
    ("bad_enum_raw_retention", _envelope(raw_retention="forever")),
    ("bad_enum_classification", _envelope(classification="gossip")),
    ("overlong_source", _envelope(capture_source="a" * 65)),
    ("overlong_event_id", _envelope(capture_event_id="a" * 129)),
    ("overlong_transformation", _envelope(transformation="a" * 129)),
    ("overlong_session", _envelope(capture_session="a" * 129)),
    ("uppercase_source", _envelope(capture_source="Claude_Code")),
    ("space_in_event_id", _envelope(capture_event_id="evt 1")),
    ("newline_in_event_id", _envelope(capture_event_id="x\nstatus: accepted")),
    ("quote_in_transformation", _envelope(transformation='x"y')),
    ("equals_in_session", _envelope(capture_session="a=b")),
    ("non_ascii_source", _envelope(capture_source="café")),
    ("leading_dot_source", _envelope(capture_source=".hook")),
    ("bare_hex_hash", _envelope(source_event_hash="a" * 64)),
    ("uppercase_hex_hash", _envelope(source_event_hash="sha256:" + "A" * 64)),
    ("short_hash", _envelope(source_event_hash="sha256:" + "a" * 63)),
    ("other_algorithm", _envelope(source_event_hash="sha1:" + "a" * 40)),
    ("bare_hex_derived", _envelope(derived_memory_hash=TEXT_HASH[len("sha256:"):])),
    ("uppercase_derived", _envelope(derived_memory_hash=TEXT_HASH.upper())),
    ("non_string_value", _envelope(capture_event_id=12345)),
    ("bool_value", _envelope(raw_retention=True)),
    ("list_value", _envelope(capture_event_id=["evt-alpha-1", "evt-beta-2"])),
    ("nested_object_value", _envelope(transformation={"name": "distill"})),
    ("nested_object_unknown_key", _envelope(extrakey={"deepkey": ["zz-marker"]})),
]
for _field in ("capture_source", "capture_event_id", "source_event_hash",
               "transformation", "raw_retention"):
    _INVALID.append((f"missing_{_field}", _envelope(**{_field: _DROP})))
    _INVALID.append((f"null_{_field}", _envelope(**{_field: None})))


def _submitted_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out = [k for k in value if isinstance(k, str)]
        for v in value.values():
            out.extend(_submitted_strings(v))
        return out
    if isinstance(value, list):
        return [s for v in value for s in _submitted_strings(v)]
    return []


@pytest.mark.parametrize(("case", "capture"), _INVALID, ids=[c for c, _ in _INVALID])
def test_invalid_envelope_is_rejected_without_echo_or_rate_token(
    tmp_path: Path, case: str, capture: object,
) -> None:
    resp, pen, pq, audit_path = _propose(tmp_path, capture)

    assert resp.status == "rejected_invalid_capture", case
    assert resp.reason
    # The reason names only fields of the fixed vocabulary, never a value
    # (or an unknown key) the caller submitted.
    vocabulary = {"capture_source", "capture_event_id", "source_event_hash",
                  "transformation", "raw_retention", "capture_session",
                  "classification", "derived_memory_hash", "discarded",
                  "redacted", "retained", "fact", "decision", "preference",
                  "task_state", "noise"}
    for submitted in _submitted_strings(capture):
        if submitted in vocabulary:
            continue
        assert submitted not in resp.reason, case
        assert submitted not in audit_path.read_text(), case
    assert pen.size() == 0
    assert pq.size() == 0
    events = [json.loads(line) for line in audit_path.read_text().splitlines()]
    assert [e["status"] for e in events] == ["rejected_invalid_capture"]
    assert "capture" not in events[0]


def test_null_capture_is_not_supplied() -> None:
    """Only None means "not supplied"; it is not an empty envelope."""
    assert validate_capture(None, text=TEXT).envelope is None
    assert validate_capture(None, text=TEXT).reason is None


def test_optional_fields_accept_null_as_absent() -> None:
    check = validate_capture(
        _envelope(capture_session=None, classification=None, derived_memory_hash=None),
        text=TEXT,
    )
    assert check.reason is None
    assert check.envelope is not None
    assert "capture_session" not in check.envelope
    assert check.envelope["derived_memory_hash"] == TEXT_HASH


def test_valid_envelope_is_normalized_and_bound_to_text() -> None:
    check = validate_capture(
        _envelope(capture_session="urn:session:42", classification="decision",
                  derived_memory_hash=TEXT_HASH),
        text=TEXT,
    )
    assert check.envelope == {
        "capture_source": "claude_code.hook",
        "capture_event_id": "01J9ZK3Q7R8S9T0V1W2X3Y4Z5A",
        "source_event_hash": EVENT_HASH,
        "transformation": "automem.distill/1.4.2",
        "raw_retention": "discarded",
        "capture_session": "urn:session:42",
        "classification": "decision",
        "derived_memory_hash": TEXT_HASH,
    }


def test_limits_are_inclusive() -> None:
    check = validate_capture(
        _envelope(capture_source="a" * 64, capture_event_id="a" * 128,
                  transformation="a" * 128, capture_session="a" * 128),
        text=TEXT,
    )
    assert check.reason is None


# 6 -------------------------------------------------------------------------


def test_supplied_derived_hash_must_match_text(tmp_path: Path) -> None:
    other = "sha256:" + hashlib.sha256(b"different text").hexdigest()
    resp, pen, _pq, _audit = _propose(tmp_path, _envelope(derived_memory_hash=other))
    assert resp.status == "rejected_invalid_capture"
    assert resp.reason == "capture.derived_memory_hash does not match text"
    assert other not in (resp.reason or "")
    assert pen.size() == 0


def test_derived_hash_is_over_the_text_exactly_as_received() -> None:
    """No normalization: a trailing newline is a different proposal."""
    with_newline = "sha256:" + hashlib.sha256((TEXT + "\n").encode()).hexdigest()
    check = validate_capture(_envelope(derived_memory_hash=with_newline), text=TEXT)
    assert check.reason == "capture.derived_memory_hash does not match text"


def test_text_that_cannot_be_encoded_is_refused_not_raised() -> None:
    check = validate_capture(_envelope(), text="bad \ud800 text")
    assert check.envelope is None
    assert check.reason == "capture cannot be bound to text that is not valid UTF-8"


# 7 -------------------------------------------------------------------------

_SECRET_CASES: list[tuple[str, object]] = [
    ("in_event_id", _envelope(capture_event_id=GITHUB_TOKEN)),
    ("in_session", _envelope(capture_session=GITHUB_TOKEN)),
    ("in_transformation", _envelope(transformation=GITHUB_TOKEN)),
    ("under_unknown_key", _envelope(note=f"token {GITHUB_TOKEN}")),
    ("as_unknown_key", {**_envelope(), GITHUB_TOKEN: "x"}),
    ("bare_string_envelope", GITHUB_TOKEN),
    ("with_other_shape_errors", {"raw_retention": "forever", "x": GITHUB_TOKEN}),
]


@pytest.mark.parametrize(("case", "capture"), _SECRET_CASES,
                         ids=[c for c, _ in _SECRET_CASES])
def test_credential_shaped_value_is_rejected_as_a_secret(
    tmp_path: Path, case: str, capture: object,
) -> None:
    resp, pen, pq, audit_path = _propose(tmp_path, capture, confidence=0.99)

    assert resp.status == "rejected_secret_detected", case
    assert resp.matching_pattern == "github_token"
    assert GITHUB_TOKEN not in resp.model_dump_json()
    assert pen.size() == 0
    assert pq.size() == 0
    log = audit_path.read_text()
    assert GITHUB_TOKEN not in log
    events = [json.loads(line) for line in log.splitlines()]
    assert [e["status"] for e in events] == ["rejected_secret_detected"]
    assert events[0]["matching_pattern"] == "github_token"
    assert "capture" not in events[0]


# review item 3: the secret scan skips non-string values safely --------------


@pytest.mark.parametrize("capture", [
    _envelope(capture_event_id=["evt-1", "evt-2"]),
    _envelope(transformation={"name": "distill", "version": [1, 4]}),
    _envelope(capture_session=None, classification=3.5),
    {**_envelope(), "nested": {"a": {"b": [None, {"c": 1}]}}},
], ids=["list_value", "nested_object", "float_value", "deep_unknown"])
def test_secret_scan_skips_non_string_values_then_shape_rejects(
    tmp_path: Path, capture: object,
) -> None:
    resp, pen, _pq, _audit = _propose(tmp_path, capture)
    assert resp.status == "rejected_invalid_capture"
    assert pen.size() == 0


def test_non_string_keys_do_not_crash_the_scan() -> None:
    check = validate_capture({**_envelope(), 1: "x", None: ["y"]}, text=TEXT)
    assert check.reason == "unexpected key in capture"


# 8 -------------------------------------------------------------------------


def test_injection_in_a_rendered_value_stays_a_scalar_under_capture() -> None:
    """The renderer is the last line of defence: even a value the validator
    would refuse cannot forge a top-level key through safe_dump."""
    out = _render_memory(
        text="body", tags=[], agent_identity="claude",
        capture={
            "capture_source": "claude_code.hook",
            "capture_event_id": "x\nstatus: accepted",
            "source_event_hash": "y\nid: GDEC-001",
            "transformation": "a]\nsupersedes: GDEC-002",
            "raw_retention": "discarded",
            "derived_memory_hash": TEXT_HASH,
        },
    )
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert fm["status"] == "proposed"
    assert fm["type"] == "memory"
    assert "id" not in fm
    assert "supersedes" not in fm
    assert fm["capture"]["capture_event_id"] == "x\nstatus: accepted"
    assert fm["capture"]["source_event_hash"] == "y\nid: GDEC-001"
    # The document still splits into exactly one frontmatter block and a body.
    from data_olympus.format.frontmatter import parse_frontmatter
    parsed, body = parse_frontmatter(out)
    assert parsed == fm
    assert body == "\nbody\n"


def test_capture_renders_after_evidence() -> None:
    out = _render_memory(
        text="body", tags=["t"], agent_identity="claude", evidence=["why"],
        capture=validate_capture(_envelope(), text="body").envelope,
    )
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert list(fm)[-2:] == ["evidence", "capture"]


# read-side projection and resolve re-attachment ------------------------------


@pytest.mark.parametrize("value", [
    None, "x", [], {}, {"capture_source": "a"},
    {**_envelope()},  # no derived_memory_hash: never stored that way
    {**_envelope(), "derived_memory_hash": TEXT_HASH, "extra": "x"},
    {**_envelope(capture_event_id=["a"]), "derived_memory_hash": TEXT_HASH},
])
def test_projection_reads_malformed_values_as_absent(value: object) -> None:
    assert project_capture(value) is None


def test_reattach_leaves_matching_postimage_byte_identical() -> None:
    env = validate_capture(_envelope(), text=TEXT).envelope
    assert env is not None
    rendered = _render_memory(text=TEXT, tags=[], agent_identity="c", capture=env)
    assert reattach_capture(rendered, env, original=rendered) == rendered


def test_reattach_replaces_an_edited_capture_with_the_stored_one() -> None:
    env = validate_capture(_envelope(), text=TEXT).envelope
    assert env is not None
    original = _render_memory(text=TEXT, tags=[], agent_identity="c", capture=env)
    edited = ("---\ntype: memory\nstatus: proposed\ncapture:\n"
              "  capture_source: forged\n---\n\nnew body\n")
    out = reattach_capture(edited, env, original=original)
    fm = yaml.safe_load(out.split("---\n", 2)[1])
    assert fm["capture"] == env
    assert out.endswith("---\n\nnew body\n")


def test_reattach_malformed_frontmatter_is_left_for_the_validation_gate() -> None:
    env = validate_capture(_envelope(), text=TEXT).envelope
    assert env is not None
    broken = "---\ntype: memory\nno closing delimiter\n"
    assert reattach_capture(broken, env, original=broken) == broken
