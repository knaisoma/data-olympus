"""Keep submitted MCP input out of validation errors and server logs.

What FastMCP 3.4.x and the MCP SDK did with caller input:

- A tool call whose arguments fail validation returned pydantic's error text,
  input values and unexpected argument names included, and logged it at WARNING.
- Their DEBUG traces record call arguments, tool and prompt names, and resource
  URIs as the caller sent them.
- A failing tool, prompt or resource logs a traceback whose exception messages
  can quote the input.
- The SDK logs malformed requests, notifications and unsolicited responses,
  often with the raw message, before any tool runs.
- An unknown tool name was echoed back to the caller.

Log records are handled by where they come from, not by what they say. Every
record emitted from the MCP SDK or from FastMCP's server code is rewritten by
default: a message template keeps its fixed text and every argument becomes
``<redacted>``; a message that was already formatted becomes a marker naming its
source file and line, which is enough to find the diagnostic that fired; any
traceback keeps its frames and exception types, exception groups included, but
not exception messages. Records from this application are never touched, even
when they reach the same logger. Matching on message text was tried first and
each review found a message shape it missed; an origin rule cannot miss one.

On the response side, argument-validation and unknown-tool errors are rebuilt
from fixed vocabulary. A tool's own error message still reaches the caller that
sent the request, because it tells them how to correct the call and goes
nowhere else.

Behaviour is tested through a real MCP client with fully formatted records
(``tests/test_mcp_validation_sanitization.py``), so a dependency upgrade that
changes an exception type or a module layout fails CI instead of leaking again.
"""
from __future__ import annotations

import logging
import os
import traceback
from typing import TYPE_CHECKING, Any

import fastmcp
import mcp
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


class _SignatureParameters:
    """Marker: location heads come from a tool's own call signature."""


SIGNATURE_PARAMETERS = _SignatureParameters()

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


def _trusted_head(first: object, params: object) -> bool:
    if not isinstance(first, str):
        return False
    if params is SIGNATURE_PARAMETERS:
        return True
    return isinstance(params, frozenset) and first in params


def _location(error: dict[str, Any], params: object) -> str:
    loc = error.get("loc")
    loc = tuple(loc) if isinstance(loc, (list, tuple)) else ()
    if not loc:
        return "arguments"
    first = loc[0]
    if error.get("type") in _UNEXPECTED_TYPES:
        head = UNEXPECTED_ARGUMENT
    elif _trusted_head(first, params):
        head = str(first)
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


def summarize_errors(errors: object, params: object = None) -> str:
    """One clause per distinct problem, from trusted names and allowlisted codes.

    ``params`` is a frozenset of declared parameter names, ``SIGNATURE_PARAMETERS``
    when the errors come from validating a call against the tool's signature,
    or ``None`` when no location head can be trusted.
    """
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


def summarize_validation_error(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        # FastMCP raises this only for a call that failed its signature check;
        # the pydantic error it wraps locates problems by parameter name.
        cause, params = exc.__cause__, SIGNATURE_PARAMETERS
    else:
        # A bare pydantic error comes from a tool body validating its own data,
        # whose locations can be caller-chosen keys.
        cause, params = exc, None
    if isinstance(cause, PydanticValidationError):
        return summarize_errors(
            [dict(e) for e in cause.errors(include_url=False, include_input=False)], params,
        )
    return "arguments are invalid"


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
        try:
            return await call_next(context)
        except (ValidationError, PydanticValidationError) as exc:
            name = context.message.name
            raise ToolError(
                f"Invalid arguments for tool {name!r}: {summarize_validation_error(exc)}"
            ) from None
        except NotFoundError:
            raise ToolError("Unknown tool") from None


def _package_root(module: Any, subdir: str = "") -> str:
    root = os.path.dirname(os.path.abspath(module.__file__))
    return os.path.join(root, subdir) if subdir else root


_SDK_ROOTS = (_package_root(mcp), _package_root(fastmcp, "server"))
_SITE_ROOTS = tuple(os.path.dirname(_package_root(m)) for m in (mcp, fastmcp))
_INVALID_ARGUMENTS = "Invalid arguments for tool %r: %s"
_SAFE_TYPE_NAME_TEMPLATES = frozenset({
    "Processing request of type %s", "Dispatching request of type %s",
})


def _from_sdk(record: logging.LogRecord) -> bool:
    path = os.path.abspath(record.pathname or "")
    return any(path.startswith(root + os.sep) for root in _SDK_ROOTS)


def _source(record: logging.LogRecord) -> str:
    path = os.path.abspath(record.pathname or "")
    for root in _SITE_ROOTS:
        if path.startswith(root + os.sep):
            path = os.path.relpath(path, root)
            break
    return f"{path.replace(os.sep, '/')}:{record.lineno}"


def _format_exception(te: traceback.TracebackException, lines: list[str], depth: int) -> None:
    if depth > 8:
        lines.append("<nested exceptions omitted>")
        return
    cause = te.__cause__ or (None if te.__suppress_context__ else te.__context__)
    if cause is not None:
        _format_exception(cause, lines, depth + 1)
        lines.append("\nThe above exception led to the following exception:\n")
    lines.append("Traceback (most recent call last):")
    lines.extend(line.rstrip("\n") for line in te.stack.format())
    lines.append(f"{te.exc_type_str}: {MESSAGE_REDACTED}")
    for index, child in enumerate(getattr(te, "exceptions", None) or (), start=1):
        lines.append(f"  +---------------- {index} ----------------")
        child_lines: list[str] = []
        _format_exception(child, child_lines, depth + 1)
        lines.extend("  | " + line for line in child_lines)


def _sanitized_traceback(exc_info: Any) -> str:
    value = exc_info[1] if isinstance(exc_info, tuple) and len(exc_info) == 3 else None
    if not isinstance(value, BaseException):
        return ""
    te = traceback.TracebackException(
        type(value), value, value.__traceback__, capture_locals=False,
    )
    lines: list[str] = []
    _format_exception(te, lines, 0)
    return "\n".join(lines)


def _redacted_args(args: object) -> tuple[object, ...] | dict[str, object]:
    if isinstance(args, dict):
        return dict.fromkeys(args, REDACTED)
    if isinstance(args, tuple):
        return tuple(REDACTED for _ in args)
    return ()


class ArgumentSanitizingLogFilter(logging.Filter):
    """Rewrite records emitted by the MCP SDK or FastMCP's server; never raise."""

    def filter(self, record: logging.LogRecord) -> bool:
        # The same record can pass this filter on a logger and again on a
        # handler; rewriting twice would redact the sanitized result.
        if getattr(record, "_mcp_input_sanitized", False):
            return True
        try:
            if _from_sdk(record):
                self._rewrite(record)
                record._mcp_input_sanitized = True
        except Exception:
            record.msg = "MCP diagnostic redacted: the input sanitizer could not parse it"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True

    @staticmethod
    def _rewrite(record: logging.LogRecord) -> None:
        msg, args = record.msg, record.args
        if msg == _INVALID_ARGUMENTS and isinstance(args, tuple) and len(args) == 2:
            name = args[0] if isinstance(args[0], str) else REDACTED
            # The tool resolved before its arguments were validated, so the name
            # is a registered one; the locations are not trusted in the log.
            record.args = (name, summarize_errors(args[1], None))
        elif msg in _SAFE_TYPE_NAME_TEMPLATES and isinstance(args, tuple) and all(
            isinstance(a, str) and a.isidentifier() for a in args
        ):
            pass
        elif isinstance(msg, str) and args:
            record.args = _redacted_args(args)
        else:
            record.msg = f"MCP diagnostic from {_source(record)} (content redacted)"
            record.args = ()
        try:
            record.getMessage()
        except Exception:
            record.msg = f"MCP diagnostic from {_source(record)} (content redacted)"
            record.args = ()
        if record.exc_info:
            record.exc_text = _sanitized_traceback(record.exc_info)
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = f"<exception text redacted from {_source(record)}>"
        record.stack_info = None


_FILTER = ArgumentSanitizingLogFilter()


def install_log_filter() -> None:
    """Attach the filter to every logger and handler MCP records can pass through.

    A logger's own filters see only records logged on that logger, so the root
    logger (the SDK session logs through it directly) and every existing ``mcp``
    and ``fastmcp`` logger get the filter. The handlers on the root and
    ``fastmcp`` loggers get it too, which covers SDK modules imported later.
    The filter ignores records from anywhere else, so attaching it broadly
    changes nothing for this application's own logging. Safe to call repeatedly.
    """
    loggers = [logging.getLogger(), logging.getLogger("fastmcp")]
    for name in list(logging.Logger.manager.loggerDict):
        if name == "mcp" or name.startswith(("mcp.", "fastmcp")):
            loggers.append(logging.getLogger(name))
    for logger in loggers:
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    for logger in (logging.getLogger(), logging.getLogger("fastmcp")):
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
