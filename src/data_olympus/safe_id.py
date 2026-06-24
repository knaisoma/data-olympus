"""Derive a collision-safe filesystem + git-ref-safe id from an opaque session
identifier.

The id is used as:
  - a subdirectory name under /kb-worktrees/<safe_id>/
  - a git branch name kb-session/<safe_id>

Therefore it MUST satisfy: lowercase, alphanumeric + dash + underscore only,
no leading dash, no '..', no control chars, no whitespace, no NUL.

Codex blocker 1 fix: always append a hash suffix so two long inputs that
collapse to the same sanitized prefix still produce DIFFERENT safe_ids.
"""
from __future__ import annotations

import hashlib
import re

_SANITIZE_RE = re.compile(r"[^a-z0-9_-]+")
_PREFIX_LEN = 48
_HASH_LEN = 12


def make_safe_id(source_session: str) -> str:
    """Return a stable, collision-safe safe_id for `source_session`.

    Two distinct inputs ALWAYS produce distinct safe_ids (the hash suffix is
    the guarantee). Empty / whitespace-only / control-char-only inputs produce
    a non-empty fallback safe_id.
    """
    raw = source_session or ""
    lowered = raw.lower()
    sanitized = _SANITIZE_RE.sub("-", lowered).strip("-_") or "session"
    prefix = sanitized[:_PREFIX_LEN]
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:_HASH_LEN]
    return f"{prefix}-{digest}"
