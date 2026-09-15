"""Unit checks for the sanitizer's vocabulary and its log filter's robustness."""
from __future__ import annotations

import logging
import sys

import pytest

from data_olympus import mcp_sanitize
from data_olympus.mcp_sanitize import ArgumentSanitizingLogFilter, summarize_errors

FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _format(record: logging.LogRecord) -> str:
    return logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)


def _record(msg: object, args: object = (), exc_info: object = None) -> logging.LogRecord:
    return logging.LogRecord("fastmcp.server.server", logging.WARNING, __file__, 1,
                             msg, args, exc_info)  # type: ignore[arg-type]


def test_location_head_kept_only_for_declared_parameters() -> None:
    errors = [
        {"type": "extra_forbidden", "loc": (FAKE,)},
        {"type": "string_type", "loc": ("text",)},
    ]
    summary = summarize_errors(errors, params=frozenset({"text"}))
    assert FAKE not in summary
    assert "text must be a string (string_type)" in summary
    assert mcp_sanitize.PARAMETER_PLACEHOLDER in summary


def test_integer_location_head_is_not_trusted() -> None:
    summary = summarize_errors([{"type": "missing_positional_only_argument", "loc": (0,)}],
                               params=frozenset({"text"}))
    assert "is required" in summary


def test_unknown_error_code_is_not_emitted() -> None:
    summary = summarize_errors([{"type": FAKE.lower(), "loc": ("text",)}],
                               params=frozenset({"text"}))
    assert FAKE.lower() not in summary
    assert summary == "text is invalid"


def test_unknown_tool_parameters_hide_every_head() -> None:
    summary = summarize_errors([{"type": "string_type", "loc": (FAKE,)}], params=None)
    assert FAKE not in summary


@pytest.mark.parametrize("record", [
    _record("Invalid arguments for tool %r: %s", ("kb_search", [{"type": "x", "loc": 17}])),
    _record("Invalid arguments for tool %r: %s", ({"a": FAKE},)),
    _record(f"Failed to validate request: bad {FAKE}"),
    _record(f"Message that failed validation: {FAKE}"),
    _record(f"Error reading resource 'data://{FAKE}'"),
    _record("[srv] Handler called: read_resource %s", (f"data://{FAKE}",)),
    _record(object()),
])
def test_filter_never_raises_and_never_passes_input(record: logging.LogRecord) -> None:
    assert ArgumentSanitizingLogFilter().filter(record) is True
    assert FAKE not in _format(record)


def test_filter_strips_exception_messages_but_keeps_types_and_frames() -> None:
    try:
        try:
            int(FAKE)
        except ValueError as inner:
            raise ValueError(f"validity_state {FAKE!r}") from inner
    except ValueError:
        record = _record("Error calling tool %r", ("kb_search",), sys.exc_info())
    ArgumentSanitizingLogFilter().filter(record)
    formatted = _format(record)
    assert FAKE not in formatted
    assert "Traceback" in formatted
    assert "ValueError" in formatted
    assert "test_filter_strips_exception_messages_but_keeps_types_and_frames" in formatted
