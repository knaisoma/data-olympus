"""Tests for collision-safe session id derivation."""
from __future__ import annotations

import re

from data_olympus.safe_id import make_safe_id


def test_make_safe_id_collision_safety_distinct_long_sessions_with_same_prefix() -> None:
    """Two long session ids that collapse to the same 48-char sanitized prefix
    MUST produce different safe_ids (the hash suffix is what guarantees this)."""
    a = "session-abcdefghijklmnopqrstuvwxyz-0123456789abcdef-extra-1"
    b = "session-abcdefghijklmnopqrstuvwxyz-0123456789abcdef-extra-2"
    sa = make_safe_id(a)
    sb = make_safe_id(b)
    assert sa != sb
    # Both safe_ids start with the same sanitized prefix; only the hash suffix differs.
    assert sa[:48] == sb[:48]


def test_make_safe_id_empty_input_produces_stable_nonempty_id() -> None:
    sid = make_safe_id("")
    assert sid
    assert sid == make_safe_id("")


def test_make_safe_id_leading_dot_does_not_collide_with_path_traversal() -> None:
    sid = make_safe_id("...")
    assert not sid.startswith(".")


def test_make_safe_id_is_git_ref_safe() -> None:
    """The safe_id is used as a branch name (kb-session/<safe_id>) so it must
    satisfy git ref naming rules: no leading dash, no '..', no control chars."""
    sid = make_safe_id("019e7eec/8b2e/4100/badness\x00\nwith control chars")
    assert not sid.startswith("-")
    assert ".." not in sid
    assert re.match(r"^[a-z0-9_-]+$", sid), f"unexpected chars in {sid!r}"


def test_make_safe_id_unicode_input_is_handled() -> None:
    sid = make_safe_id("session-日本語-ñoño-emoji-😀")
    assert sid
    assert re.match(r"^[a-z0-9_-]+$", sid)
