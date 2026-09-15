"""Keep submitted MCP tool input out of error responses and logs.

FastMCP reports a tool call whose arguments fail validation by returning the
pydantic error text to the caller and logging it at WARNING. Both carry the
submitted input and, for an unexpected argument, its name, so a credential an
agent puts in the wrong field reaches the server log. FastMCP also logs every
call's arguments at DEBUG, and an unknown tool name is echoed back verbatim.

This module rebuilds those messages from fixed vocabulary: a parameter name is
kept only where it is a real parameter of the tool, any other location component
becomes a placeholder, and the rule is a fixed phrase. No input value, repr or
caller-chosen name survives. Both halves are behaviour-tested through a real MCP
client (``tests/test_mcp_validation_sanitization.py``), so a FastMCP upgrade that
changes the exception type or the log message fails CI instead of leaking again.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from fastmcp.exceptions import NotFoundError, ToolError, ValidationError
from fastmcp.server.middleware import Middleware
from pydantic import ValidationError as PydanticValidationError

if TYPE_CHECKING:
    import mcp.types as mt
    from fastmcp.server.middleware import CallNext, MiddlewareContext
    from fastmcp.tools.base import ToolResult

UNEXPECTED_ARGUMENT = "<unexpected argument>"
KEY_PLACEHOLDER = "<key>"
REDACTED_ARGUMENTS = "<arguments redacted>"

_RULES: dict[str, str] = {
    "missing_argument": "is required",
    "missing": "is required",
    "unexpected_keyword_argument": "is not a parameter of this tool",
    "unexpected_positional_argument": "is not a parameter of this tool",
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
_TYPE_CODE = re.compile(r"[a-z_]{1,64}")
_INVALID_ARGUMENTS_LOG = "Invalid arguments for tool %r: %s"


def _location(error: dict[str, Any]) -> str:
    loc = tuple(error.get("loc") or ())
    if not loc:
        return "arguments"
    if error.get("type") in {"unexpected_keyword_argument", "unexpected_positional_argument"}:
        head = UNEXPECTED_ARGUMENT
    else:
        # For every other argument-validation error the first component is a
        # declared parameter of the tool, not caller input.
        head = str(loc[0])
    parts = [head]
    for component in loc[1:]:
        if isinstance(component, int):
            parts[-1] += f"[{component}]"
        else:
            parts.append(KEY_PLACEHOLDER)
    return ".".join(parts)


def _rule(error: dict[str, Any]) -> str:
    code = str(error.get("type") or "")
    phrase = _RULES.get(code, "is invalid")
    return f"{phrase} ({code})" if _TYPE_CODE.fullmatch(code) else phrase


def summarize_errors(errors: list[dict[str, Any]]) -> str:
    """One line per distinct problem, built only from locations and rule codes."""
    seen: list[str] = []
    for error in errors:
        line = f"{_location(error)} {_rule(error)}"
        if line not in seen:
            seen.append(line)
    return "; ".join(seen) if seen else "arguments are invalid"


def _summarize_detail(detail: object) -> str:
    if isinstance(detail, list) and all(isinstance(e, dict) for e in detail):
        return summarize_errors(detail)
    return "arguments are invalid"


def summarize_validation_error(exc: BaseException) -> str:
    cause = exc.__cause__ if isinstance(exc, ValidationError) else exc
    if isinstance(cause, PydanticValidationError):
        return summarize_errors(
            [dict(e) for e in cause.errors(include_url=False, include_input=False)]
        )
    return "arguments are invalid"


class ArgumentSanitizingMiddleware(Middleware):
    """Replace argument-validation and unknown-tool errors with sanitized ones.

    Registered as the outermost middleware so it also covers calls that arrive
    through the search-mode ``call_tool`` proxy, which re-enters the middleware
    chain for the inner tool.
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


class ArgumentSanitizingLogFilter(logging.Filter):
    """Rewrite FastMCP log records that would carry tool input.

    Records are rewritten rather than dropped, so an operator still sees which
    tool and parameter failed and which rule, just not the submitted value.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 2 or not isinstance(record.msg, str):
            return True
        if record.msg == _INVALID_ARGUMENTS_LOG:
            record.args = (args[0], _summarize_detail(args[1]))
        elif record.msg.endswith(("Handler called: call_tool %s with %s",
                                  "Handler called: get_prompt %s with %s")):
            record.args = (args[0], REDACTED_ARGUMENTS)
        return True


_FILTERED_LOGGERS = ("fastmcp.server.server", "fastmcp.server.mixins.mcp_operations")


def install_log_filter() -> None:
    """Attach the filter once per logger; safe to call for every app built."""
    for name in _FILTERED_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, ArgumentSanitizingLogFilter) for f in logger.filters):
            logger.addFilter(ArgumentSanitizingLogFilter())
