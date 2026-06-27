# tests/test_server_enforce_tools.py
"""The enforce tools are registered and reachable on the app."""
from __future__ import annotations

import asyncio

from data_olympus.server import build_app


def test_enforce_tools_registered(tmp_kb, tmp_index_path) -> None:
    app = build_app(
        kb_main_path=tmp_kb, kb_index_path=tmp_index_path,
        sync_interval_sec=60, staleness_degraded_sec=600, bootstrap_now=True,
    )
    # NOTE (plan adaptation): the plan's draft used `app.get_tools()`, which does
    # not exist in this fastmcp version. `app.list_tools()` is the supported
    # coroutine that returns FunctionTool objects exposing `.name`; we use that.
    names = {t.name for t in asyncio.run(app.list_tools())}
    assert {"kb_consult", "kb_gate_check", "kb_compliance"} <= names
