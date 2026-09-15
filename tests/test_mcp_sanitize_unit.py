"""Unit checks for the sanitizer's vocabulary and its log filter's robustness."""
from __future__ import annotations

import logging
import os
import sys

import mcp
import pytest

from data_olympus import mcp_sanitize
from data_olympus.mcp_sanitize import ArgumentSanitizingLogFilter, summarize_errors

FAKE = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SDK_FILE = os.path.join(os.path.dirname(mcp.__file__), "shared", "session.py")
APP_FILE = os.path.join(os.path.dirname(mcp_sanitize.__file__), "server.py")


def _format(record: logging.LogRecord) -> str:
    return logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)


def _record(msg: object, args: object = (), exc_info: object = None, *,
            pathname: str = SDK_FILE, name: str = "root") -> logging.LogRecord:
    return logging.LogRecord(name, logging.WARNING, pathname, 430,
                             msg, args, exc_info)  # type: ignore[arg-type]


def _exc_info(message: str) -> object:
    try:
        try:
            int(message)
        except ValueError as inner:
            raise ValueError(f"validity_state {message!r}") from inner
    except ValueError:
        return sys.exc_info()
    raise AssertionError


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


def test_no_declared_parameters_hide_every_head() -> None:
    summary = summarize_errors([{"type": "string_type", "loc": (FAKE,)}], params=None)
    assert FAKE not in summary


@pytest.mark.parametrize("record", [
    _record("Invalid arguments for tool %r: %s", ("kb_search", [{"type": "x", "loc": 17}]),
            name="fastmcp.server.server"),
    _record("Invalid arguments for tool %r: %s", ({"a": FAKE},), name="fastmcp.server.server"),
    _record(f"Invalid arguments for tool 't': {FAKE}", name="fastmcp.server.server"),
    _record(f"Failed to validate notification: bad {FAKE}"),
    _record(f"Response ID {FAKE!r} cannot be normalized to match pending requests"),
    _record(f"Received exception from stream: SessionMessage(id={FAKE!r})",
            name="mcp.server.lowlevel.server"),
    _record("[srv] Handler called: get_prompt %s with %s", (FAKE, {}),
            name="fastmcp.server.mixins.mcp_operations"),
    _record("[srv] Handler called: call_tool %(n)s with %(a)s", ({"n": "kb", "a": FAKE},),
            name="fastmcp.server.mixins.mcp_operations"),
    _record("Tool cache miss for %s, refreshing cache", (FAKE,), name="mcp.server.lowlevel.server"),
    _record(object()),
])
def test_sdk_records_never_pass_input_and_filter_never_raises(record: logging.LogRecord) -> None:
    assert ArgumentSanitizingLogFilter().filter(record) is True
    assert FAKE not in _format(record)


def test_sdk_preformatted_record_keeps_its_source_location() -> None:
    record = _record(f"Failed to validate notification: bad {FAKE}")
    ArgumentSanitizingLogFilter().filter(record)
    assert "mcp/shared/session.py:430" in _format(record)


def test_sdk_cached_exception_text_and_stack_info_are_removed() -> None:
    record = _record("Error", ())
    record.exc_text = f"ValueError: {FAKE}"
    record.stack_info = f"Stack (most recent call last):\n  {FAKE}"
    ArgumentSanitizingLogFilter().filter(record)
    assert FAKE not in _format(record)


def test_sdk_traceback_keeps_types_and_frames_without_messages() -> None:
    record = _record("Error calling tool %r", ("kb_search",), _exc_info(FAKE),
                     name="fastmcp.server.server")
    ArgumentSanitizingLogFilter().filter(record)
    formatted = _format(record)
    assert FAKE not in formatted
    assert "Traceback" in formatted
    assert "ValueError" in formatted
    assert "_exc_info" in formatted


def test_exception_group_children_keep_types_and_frames() -> None:
    class ChildFailure(Exception):
        pass

    def inner_frame() -> None:
        raise ChildFailure(FAKE)

    try:
        try:
            inner_frame()
        except ChildFailure as child:
            raise ExceptionGroup("group", [child]) from None
    except ExceptionGroup:
        info = sys.exc_info()
    record = _record("Error", (), info, name="mcp.server.lowlevel.server")
    ArgumentSanitizingLogFilter().filter(record)
    formatted = _format(record)
    assert FAKE not in formatted
    assert "ChildFailure" in formatted
    assert "inner_frame" in formatted


def test_application_records_are_untouched_even_on_root() -> None:
    record = _record("startup database unavailable: %s", ("detail",), _exc_info("detail"),
                     pathname=APP_FILE)
    ArgumentSanitizingLogFilter().filter(record)
    formatted = _format(record)
    assert "startup database unavailable: detail" in formatted
    assert "validity_state 'detail'" in formatted
