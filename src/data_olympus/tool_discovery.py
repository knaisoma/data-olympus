"""MCP tool catalog discovery modes."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from fastmcp.server.transforms.search import BM25SearchTransform

if TYPE_CHECKING:
    from collections.abc import Sequence

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


def configure_tool_discovery(
    app: FastMCP, mode: ToolDiscoveryMode, extra_visible: Sequence[str] = (),
) -> None:
    """Filter tool listings in search mode without unregistering native tools.

    ``extra_visible`` adds tools to the always-visible set (Agora adds
    ``agora_inbox`` so a session can always check its inbox)."""
    resolved_mode = load_tool_discovery_mode(mode)
    if resolved_mode == "search":
        visible = list(DEFAULT_VISIBLE_TOOLS)
        visible += [name for name in extra_visible if name not in visible]
        app.add_transform(
            BM25SearchTransform(
                always_visible=visible,
                search_tool_name="tool_search",
                call_tool_name="call_tool",
            )
        )
