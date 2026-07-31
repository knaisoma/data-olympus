"""MCP tool catalog discovery modes."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from fastmcp.server.transforms.search import BM25SearchTransform

if TYPE_CHECKING:
    from fastmcp import FastMCP

ToolDiscoveryMode = Literal["search", "all"]

DEFAULT_VISIBLE_TOOLS = (
    "kb_consult",
    "kb_search",
    "kb_get",
    "kb_health",
    "kb_gate_check",
    "kb_record_event",
    "kb_session_recap",
)


def load_tool_discovery_mode(raw: str) -> ToolDiscoveryMode:
    """Parse the catalog mode and fail closed on configuration mistakes."""
    mode = raw.strip().lower() or "search"
    if mode not in {"search", "all"}:
        raise ValueError("KB_TOOL_DISCOVERY_MODE must be 'search' or 'all'")
    return cast("ToolDiscoveryMode", mode)


def configure_tool_discovery(app: FastMCP, mode: ToolDiscoveryMode) -> None:
    """Filter tool listings in search mode without unregistering native tools."""
    resolved_mode = load_tool_discovery_mode(mode)
    if resolved_mode == "search":
        app.add_transform(
            BM25SearchTransform(
                always_visible=list(DEFAULT_VISIBLE_TOOLS),
                search_tool_name="tool_search",
                call_tool_name="call_tool",
            )
        )
