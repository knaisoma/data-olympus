"""Keep submitted MCP input out of validation errors and server logs.

What FastMCP 3.4.x and the MCP SDK did with caller input:

- A tool call whose arguments fail validation returned pydantic's error text,
  input values and unexpected argument names included, and logged it at WARNING.
- FastMCP's DEBUG trace recorded every call's arguments and every resource URI.
- A tool that raises logs ``Error calling tool`` with a traceback, and the
  exception messages in that traceback can quote the input (``kb_search``'s
  ``validity_state`` parser does).
- The SDK logs a malformed request envelope, and at DEBUG the raw message,
  through the root logger before any tool runs.
- An unknown tool name was echoed back, and an unknown resource URI logged.

The guarantee this module gives is about logs: no record it filters carries
caller input, and tracebacks keep their frames and exception types while
dropping exception messages, so a failure stays diagnosable. On the response
side, argument-validation and unknown-tool errors are rebuilt from fixed
vocabulary. A tool's own error message still reaches the caller that sent the
request, because it is guidance for correcting the call and goes nowhere else.

Everything is behaviour-tested through a real MCP client with formatted records
(``tests/test_mcp_validation_sanitization.py``), so a dependency upgrade that
changes a message or an exception type fails CI instead of leaking again.
"""
from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError as PydanticValidationError

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.base import ToolResult

UNEXPECTED_ARGUMENT = "<unexpected argument>"
PARAMETER_PLACEHOLDER = "<parameter>"
KEY_PLACEHOLDER = "<key>"
REDACTED = "<redacted>"
MESSAGE_REDACTED = "<message redacted>"

_RULES: dict[str, str] = {
    "missing": "is required",
    "missing_argument": "is required",
    "missing_keyword_only_argument": "is required",
    "missing_positional_only_argument": "is required",
    "unexpected_keyword_argument": "is not a parameter of this tool",
    "unexpected_positional_argument": "is not a parameter of this tool",
    "multiple_argument_values": "was given more than once",
    "extra_forbidden": "is not allowed",
    "string_type": "must be a string",
    "float_type": "must be a number",
    "float_parsing": "must be a number",
    "int_type": "must be an integer",
    "int_parsing": "must be an integer",
    "int_from_float": "must be an integer",
    "bool_type": "must be a boolean",
    "bool_parsing": "must be a boolean",
    "list_type": "must be a list",
    "dict_type": "must be an object",
    "none_required": "must be null",
    "greater_than": "is out of range",
    "greater_than_equal": "is out of range",
    "less_than": "is out of range",
    "less_than_equal": "is out of range",
    "too_long": "is too long",
    "too_short": "is too short",
    "string_too_long": "is too long",
    "string_too_short": "is too short",
    "literal_error": "is not an allowed value",
    "enum": "is not an allowed value",
}
_UNEXPECTED_TYPES = frozenset({"unexpected_keyword_argument", "unexpected_positional_argument"})

# Declared parameters of tools that have been called, by tool name. Filled only
# for names that resolved to a real tool, so caller-chosen names never grow it.
_TOOL_PARAMETERS: dict[str, frozenset[str]] = {}


def _location(error: dict[str, Any], params: frozenset[str] | None) -> str:
    loc = error.get("loc")
    loc = tuple(loc) if isinstance(loc, (list, tuple)) else ()
    if not loc:
        return "arguments"
    first = loc[0]
    if error.get("type") in _UNEXPECTED_TYPES:
        head = UNEXPECTED_ARGUMENT
    elif isinstance(first, str) and params is not None and first in params:
        head = first
    elif isinstance(first, int):
        head = f"argument {first}"
    else:
        head = PARAMETER_PLACEHOLDER
    parts = [head]
    for component in loc[1:]:
        if isinstance(component, int):
            parts[-1] += f"[{component}]"
        else:
            parts.append(KEY_PLACEHOLDER)
    return ".".join(parts)


def _rule(error: dict[str, Any]) -> str:
    code = error.get("type")
    if isinstance(code, str) and code in _RULES:
        return f"{_RULES[code]} ({code})"
    return "is invalid"


def summarize_errors(
    errors: object, params: frozenset[str] | None = None,
) -> str:
    """One clause per distinct problem, from trusted names and allowlisted codes."""
    if not isinstance(errors, list):
        return "arguments are invalid"
    seen: list[str] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        line = f"{_location(error, params)} {_rule(error)}"
        if line not in seen:
            seen.append(line)
    return "; ".join(seen) if seen else "arguments are invalid"


def summarize_validation_error(exc: BaseException, params: frozenset[str] | None) -> str:
    cause = exc.__cause__ if isinstance(exc, ValidationError) else exc
    if isinstance(cause, PydanticValidationError):
        return summarize_errors(
            [dict(e) for e in cause.errors(include_url=False, include_input=False)], params,
        )
    return "arguments are invalid"


def _sanitized_traceback(exc_info: Any) -> str:
    """Frames and exception types, for every exception in the chain, no messages."""
    _type, value, tb = exc_info
    if value is None:
        return ""
    lines: list[str] = []
    te: traceback.TracebackException | None = traceback.TracebackException(
        type(value), value, tb, capture_locals=False,
    )
    chain: list[traceback.TracebackException] = []
    while te is not None and len(chain) < 16:
        chain.append(te)
        te = te.__cause__ or (None if te.__suppress_context__ else te.__context__)
    for index, item in enumerate(reversed(chain)):
        if index:
            lines.append("\nThe above exception led to the following exception:\n")
        lines.append("Traceback (most recent call last):")
        lines.extend(line.rstrip("\n") for line in item.stack.format())
        lines.append(f"{item.exc_type_str}: {MESSAGE_REDACTED}")
    return "\n".join(lines)


class ArgumentSanitizingMiddleware(Middleware):
    """Rebuild argument-validation and unknown-tool errors from fixed vocabulary.

    Registered as the outermost middleware so it also covers calls through the
    search-mode ``call_tool`` proxy, which re-enters the chain for the inner tool.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        params = await self._parameters(context, name)
        try:
            return await call_next(context)
        except (ValidationError, PydanticValidationError) as exc:
            raise ToolError(
                f"Invalid arguments for tool {name!r}: {summarize_validation_error(exc, params)}"
            ) from None
        except NotFoundError:
            raise ToolError("Unknown tool") from None

    @staticmethod
    async def _parameters(
        context: MiddlewareContext[mt.CallToolRequestParams], name: str,
    ) -> frozenset[str] | None:
        if name in _TOOL_PARAMETERS:
            return _TOOL_PARAMETERS[name]
        server = getattr(context.fastmcp_context, "fastmcp", None)
        if server is None:
            return None
        try:
            tool = await server.get_tool(name)
        except Exception:
            return None
        if tool is None:
            return None
        properties = (tool.parameters or {}).get("properties") or {}
        params = frozenset(k for k in properties if isinstance(k, str))
        _TOOL_PARAMETERS[name] = params
        return params


_INVALID_ARGUMENTS = "Invalid arguments for tool %r: %s"
_PREFORMATTED_PREFIXES = (
    "Failed to validate request:",
    "Message that failed validation:",
    "Unhandled exception in receive loop:",
    "Error reading resource ",
)


class ArgumentSanitizingLogFilter(logging.Filter):
    """Rewrite log records that would carry MCP input; never drop or raise.

    Records are rewritten rather than dropped, so an operator still sees which
    tool or request failed and why, just not what the caller submitted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            self._rewrite(record)
        except Exception:
            record.msg = "log record redacted: MCP input sanitizer could not parse it"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True

    def _rewrite(self, record: logging.LogRecord) -> None:
        msg = record.msg
        args = record.args
        if isinstance(msg, str):
            if msg == _INVALID_ARGUMENTS:
                if isinstance(args, tuple) and len(args) == 2:
                    name = args[0]
                    params = _TOOL_PARAMETERS.get(name) if isinstance(name, str) else None
                    tool = name if params is not None else "<tool>"
                    record.args = (tool, summarize_errors(args[1], params))
                else:
                    record.msg = "Invalid arguments for tool: %s"
                    record.args = (REDACTED,)
            elif msg.endswith(("Handler called: call_tool %s with %s",
                               "Handler called: get_prompt %s with %s")):
                if isinstance(args, tuple) and len(args) == 2:
                    record.args = (args[0], REDACTED)
                else:
                    record.msg, record.args = "Handler called", ()
            elif msg.endswith("Handler called: read_resource %s"):
                record.args = (REDACTED,)
            else:
                for prefix in _PREFORMATTED_PREFIXES:
                    if msg.startswith(prefix):
                        record.msg = f"{prefix.rstrip()} {REDACTED}"
                        record.args = ()
                        break
        if record.exc_info:
            record.exc_text = _sanitized_traceback(record.exc_info)
            record.exc_info = None


# Loggers whose records can carry MCP input. The SDK's session logs through the
# root logger directly, and a logger's own filters do not see records that
# propagate up from children, so each emitting logger is named.
_FILTERED_LOGGERS = (
    "",
    "fastmcp.server.server",
    "fastmcp.server.mixins.mcp_operations",
    "mcp.server.lowlevel.server",
    "mcp.server.streamable_http",
)


def install_log_filter() -> None:
    """Attach the filter once per logger; safe to call for every app built."""
    for name in _FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, ArgumentSanitizingLogFilter) for f in logger.filters):
            logger.addFilter(ArgumentSanitizingLogFilter())
