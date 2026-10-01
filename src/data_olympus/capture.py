"""Capture provenance envelope on proposed memories (issue #141, first slice).

A memory distilled from a passive capture (a hook event, an external session
transcript) carries an optional ``capture`` object that labels it as
evidence-derived, binds it to the exact source event and proposal bytes, and
names the transformation that produced it. Only identifiers, hashes and enums
enter the store: the captured raw event stays outside data-olympus.

The envelope is provenance, not authority. It adds no status vocabulary and does
not change what ``in_force`` returns: a capture-derived memory is still a
``status: proposed`` document under the memory inbox, which is never in force.

This module holds the validation, the tolerant read-side projection and the
resolve-time re-attachment. The propose path in ``tools_write`` turns a
validation failure into a response, so the status literals live there.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from data_olympus.format.frontmatter import parse_frontmatter
from data_olympus.write_gate import scan_postimage_for_secrets

# Token character classes. ASCII only and matched with ``fullmatch``, so no
# value can carry whitespace, a newline, a quote, ``=`` or any prose: a token
# can never produce a writing-rule finding and never reads as an instruction.
_SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_EVENT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_HASH_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TRANSFORMATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,127}")

RAW_RETENTION = ("discarded", "redacted", "retained")
CLASSIFICATION = ("fact", "decision", "preference", "task_state", "noise")

# Field order is the rendering order, in frontmatter and in responses.
REQUIRED_FIELDS = (
    "capture_source",
    "capture_event_id",
    "source_event_hash",
    "transformation",
    "raw_retention",
)
OPTIONAL_FIELDS = ("capture_session", "classification", "derived_memory_hash")
FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS

_PATTERNS: dict[str, re.Pattern[str]] = {
    "capture_source": _SOURCE_RE,
    "capture_event_id": _EVENT_ID_RE,
    "source_event_hash": _HASH_RE,
    "transformation": _TRANSFORMATION_RE,
    "capture_session": _EVENT_ID_RE,
    "derived_memory_hash": _HASH_RE,
}
_ENUMS: dict[str, tuple[str, ...]] = {
    "raw_retention": RAW_RETENTION,
    "classification": CLASSIFICATION,
}
_EXPECTED: dict[str, str] = {
    "capture_source": "lowercase ASCII letters, digits, '_', '.', '-' (1 to 64)",
    "capture_event_id": "ASCII letters, digits, '.', '_', ':', '-' (1 to 128)",
    "capture_session": "ASCII letters, digits, '.', '_', ':', '-' (1 to 128)",
    "source_event_hash": "'sha256:' followed by 64 lowercase hex characters",
    "derived_memory_hash": "'sha256:' followed by 64 lowercase hex characters",
    "transformation": "ASCII letters, digits, '.', '_', ':', '@', '/', '+', '-' (1 to 128)",
}


def text_hash(text: str) -> str:
    """``sha256:`` of the exact UTF-8 bytes of the submitted memory text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CaptureCheck:
    """Outcome of :func:`validate_capture`.

    Exactly one of ``envelope`` (accepted), ``secret_pattern`` (a value matched
    a credential pattern) or ``reason`` (a shape error) is set, except that all
    are None when no envelope was supplied. ``reason`` never carries a
    submitted value.
    """

    envelope: dict[str, str] | None = None
    reason: str | None = None
    secret_pattern: str | None = None


def _secret_in(capture: object) -> str | None:
    """Pattern name of the first credential-shaped string in the envelope.

    Runs before any shape check, so a credential sent in the wrong place is
    reported as a secret rather than as a shape error. Covers a bare string
    envelope, and every top-level key and value of a mapping, including keys
    the shape check would reject. A non-string value (a list, a nested object,
    a number) is skipped here rather than walked: it is rejected by the shape
    check below without ever being echoed, persisted or logged.
    """
    candidates: list[str] = []
    if isinstance(capture, str):
        candidates.append(capture)
    elif isinstance(capture, Mapping):
        for key, value in capture.items():
            if isinstance(key, str):
                candidates.append(key)
            if isinstance(value, str):
                candidates.append(value)
    for candidate in candidates:
        result = scan_postimage_for_secrets(postimage=candidate)
        if result.match is not None:
            return result.match.pattern_name
    return None


def _shape_error(capture: Mapping[Any, Any]) -> str | None:
    """Rejection reason for a mapping that is not a valid envelope, else None.

    Order: unknown keys, missing required fields, then each field's type and
    pattern or enum. Names only fields from the fixed vocabulary, never a
    submitted key or value.
    """
    if not capture:
        return "capture must be a non-empty object"
    if any(key not in FIELDS for key in capture):
        return "unexpected key in capture"
    for field in REQUIRED_FIELDS:
        if capture.get(field) is None:
            return f"missing required field 'capture.{field}'"
    for field in FIELDS:
        value = capture.get(field)
        if value is None:
            # Only an OPTIONAL field can still be None here: an explicit JSON
            # null on an optional field reads as absent.
            continue
        if not isinstance(value, str):
            return f"capture.{field} must be a string"
        if field in _ENUMS:
            if value not in _ENUMS[field]:
                allowed = ", ".join(_ENUMS[field])
                return f"capture.{field} must be one of: {allowed}"
        elif not _PATTERNS[field].fullmatch(value):
            return f"capture.{field} must be {_EXPECTED[field]}"
    return None


def _ordered(capture: Mapping[Any, Any]) -> dict[str, str]:
    return {f: str(capture[f]) for f in FIELDS if capture.get(f) is not None}


def validate_capture(capture: object, *, text: str) -> CaptureCheck:
    """Validate a caller-supplied envelope and bind it to ``text``.

    ``None`` (absent or JSON null) means not supplied. Anything else must be a
    valid envelope: ``{}``, a list or a string are rejected, never coerced.
    Order: secret, object type, unknown keys, missing required, per-field
    type/pattern/enum, then the derived hash. The server computes
    ``derived_memory_hash`` over ``text`` exactly as received; a supplied value
    must equal it.
    """
    if capture is None:
        return CaptureCheck()
    secret = _secret_in(capture)
    if secret is not None:
        return CaptureCheck(secret_pattern=secret)
    if not isinstance(capture, Mapping):
        return CaptureCheck(reason="capture must be an object")
    error = _shape_error(capture)
    if error is not None:
        return CaptureCheck(reason=error)
    try:
        computed = text_hash(text)
    except (AttributeError, UnicodeEncodeError):
        # Text the encoder cannot render has no byte form to bind to. Refused
        # here rather than raising out of the propose path.
        return CaptureCheck(reason="capture cannot be bound to text that is not valid UTF-8")
    supplied = capture.get("derived_memory_hash")
    if supplied is not None and supplied != computed:
        return CaptureCheck(reason="capture.derived_memory_hash does not match text")
    envelope = _ordered(capture)
    envelope["derived_memory_hash"] = computed
    return CaptureCheck(envelope=envelope)


def project_capture(value: object) -> dict[str, str] | None:
    """Tolerant read-side projection of a stored envelope.

    Returns the envelope in field order when ``value`` is a well-formed one,
    and None for anything else, so a malformed or tampered record in pending
    meta can never raise inside a listing or a readback and take every other
    entry down with it. A stored envelope always carries
    ``derived_memory_hash``, so one without it is treated as malformed.
    """
    if not isinstance(value, Mapping):
        return None
    try:
        if _shape_error(value) is not None:
            return None
    except Exception:  # noqa: BLE001 - a read projection never raises
        return None
    if value.get("derived_memory_hash") is None:
        return None
    return _ordered(value)


def _dump(frontmatter: Mapping[str, Any]) -> str:
    import yaml

    dumped: str = yaml.safe_dump(
        dict(frontmatter), sort_keys=False, default_flow_style=False,
        allow_unicode=True,
    )
    return dumped


def reattach_capture(postimage: str, envelope: Mapping[str, str], *, original: str) -> str:
    """Return ``postimage`` with ``envelope`` as its frontmatter ``capture`` key.

    Used on resolve, where an operator's ``edited_text`` replaces the whole
    proposed document and would otherwise drop the label. The envelope comes
    from pending meta, as validated at propose time, so its
    ``derived_memory_hash`` still describes the SUBMITTED text: a mismatch
    against the committed body is the record that a human edited it.

    - Already carrying exactly this envelope: returned unchanged, byte for byte.
    - With a frontmatter block: the block is re-serialized through
      ``yaml.safe_dump`` with ``capture`` set to the stored envelope, replacing
      any ``capture`` the edit supplied. The body is kept as is.
    - Body only (no frontmatter block, the shape the interactive edit flow
      produces): the ORIGINAL proposal's frontmatter is kept, with the stored
      envelope, and the edited text becomes the body.
    - Malformed frontmatter: returned unchanged, and the content-validation
      gate rejects it before anything is written.
    """
    capture = dict(envelope)
    try:
        frontmatter, body = parse_frontmatter(postimage)
    except ValueError:
        return postimage
    if postimage.split("\n", 1)[0].strip() == "---":
        if frontmatter.get("capture") == capture:
            return postimage
        frontmatter = dict(frontmatter)
        frontmatter["capture"] = capture
        return "---\n" + _dump(frontmatter) + "---\n" + body
    try:
        base, _ = parse_frontmatter(original)
    except ValueError:
        base = {}
    base = dict(base)
    base["capture"] = capture
    text = postimage if postimage.endswith("\n") else postimage + "\n"
    return "---\n" + _dump(base) + "---\n\n" + text
