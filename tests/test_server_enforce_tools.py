# tests/test_server_enforce_tools.py
"""The enforce tools are registered and reachable on the app."""
from __future__ import annotations

import asyncio

from data_olympus.server import build_app


def test_enforce_tools_registered(tmp_kb, tmp_index_path) -> None:
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        tool_discovery_mode="all",
    )
    # NOTE (plan adaptation): the plan's draft used `app.get_tools()`, which does
    # not exist in this fastmcp version. `app.list_tools()` is the supported
    # coroutine that returns FunctionTool objects exposing `.name`; we use that.
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {"kb_consult", "kb_gate_check", "kb_compliance"} <= names


def test_kb_consult_tool_exposes_optional_trigger(tmp_kb, tmp_index_path) -> None:
    """The MCP kb_consult tool mirrors the REST /consult contract: an optional
    trigger parameter defaulting to explicit, so MCP callers (and future MCP-based
    installers) can mark prompt_hook auto-consults. It must be OPTIONAL so
    existing agent callers that omit it keep working (and count as explicit)."""
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
        tool_discovery_mode="all",
    )
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    schema = tools["kb_consult"].parameters
    props = schema.get("properties", {})
    assert "trigger" in props
    assert "trigger" not in schema.get("required", [])
    assert props["trigger"].get("default") == "explicit"
