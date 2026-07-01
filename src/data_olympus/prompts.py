"""MCP prompts for guided onboarding. Thin wrappers over the single-sourced
playbook (onboarding_playbook.py); no business logic here."""
from __future__ import annotations

from typing import TYPE_CHECKING

from data_olympus.onboarding_playbook import render_playbook

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_prompts(app: FastMCP) -> None:
    @app.prompt
    def onboard() -> str:
        """Guided entry point: detect the workspace, then route to project or
        component onboarding."""
        return render_playbook("dispatch")

    @app.prompt
    def onboard_project(workspace: str, workspace_remote_url: str | None = None) -> str:
        """Guided full-project (T3) onboarding: import, interview, bootstrap, clean up."""
        return render_playbook(
            "project", workspace=workspace, workspace_remote_url=workspace_remote_url,
        )

    @app.prompt
    def onboard_component(
        workspace: str, component: str, component_remote_url: str | None = None,
    ) -> str:
        """Guided component (T4) onboarding under a known project."""
        return render_playbook(
            "component", workspace=workspace, component=component,
            component_remote_url=component_remote_url,
        )
