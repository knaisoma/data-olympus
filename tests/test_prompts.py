"""The three onboarding MCP prompts register and render the playbook."""
from __future__ import annotations

import asyncio

from fastmcp import FastMCP

from data_olympus.prompts import register_prompts


def _names(app: FastMCP) -> set[str]:
    prompts = asyncio.run(app.list_prompts())
    return {p.name for p in prompts}


def test_all_three_prompts_register() -> None:
    app = FastMCP("test")
    register_prompts(app)
    assert {"onboard", "onboard_project", "onboard_component"} <= _names(app)


def test_project_prompt_renders_with_workspace_argument() -> None:
    # NOTE: adapted from the brief's `app.get_prompt(name, arguments)` call.
    # In fastmcp 3.4.2, FastMCP.get_prompt(name) takes only a name and
    # returns a Prompt (or None); rendering with arguments is a second step
    # via Prompt.render(arguments) -> PromptResult. Confirmed via a throwaway
    # probe against this repo's installed fastmcp. The two assertions the
    # brief intends (names registered; "foo" appears in the rendered text)
    # are unchanged.
    app = FastMCP("test")
    register_prompts(app)
    prompt = asyncio.run(app.get_prompt("onboard_project"))
    result = asyncio.run(prompt.render({"workspace": "foo"}))
    text = " ".join(
        m.content.text for m in result.messages if hasattr(m.content, "text")
    )
    assert "foo" in text
