"""The guided onboarding script, authored once. Consumed by the MCP prompts
(prompts.py), the REST/CLI playbook endpoint, and any fallback skill, so all
surfaces render identical instructions."""
from __future__ import annotations

_INTERVIEW = """\
Interview the human for discovery knowledge the repo does not already state.
Ask only what imported files do not answer. Map answers to canonical files:
  - identity, purpose, repo URL -> README.md (+ git_remote_url front-matter)
  - architecture and rationale  -> README.md "Architecture"
  - conventions and patterns     -> AGENTS.md
  - integrations and deps        -> README.md "Integrations"
  - gotchas / operational notes  -> AGENTS.md "Gotchas & operational notes"
  - ownership and contacts       -> README.md "Ownership"
"""

_CLEANUP = """\
Before cleanup, wait for the bootstrap to converge. A committed bootstrap is not
visible to the index until it is pushed and re-pulled, so poll kb_onboarding_status
until it reports 'onboarded' (or watch kb_health advance past the bootstrap commit)
BEFORE calling kb_cleanup_plan. Running cleanup against a not-yet-converged index
classifies the just-bootstrapped files as unique and produces a wrong plan.

Then call kb_cleanup_plan with the local doc files. Present the plan per file
(default to the recommended action). For imported_duplicate items, apply the
supplied thin-pointer text to the project repo with your own edit tool and leave
the changes staged for the human to commit. Show partial_overlap items for manual
review; leave unique files untouched. The server never edits the project repo.

Do not re-run kb_bootstrap_project while a prior bootstrap for the same workspace
is still converging: the server holds a short-lived in-flight guard and rejects a
second call with rejected_already_in_progress until the window elapses.
"""

_DISPATCH = """\
# Onboarding: dispatch

1. Detect the current workspace (and component, if inside a nested repo).
2. Call kb_onboarding_status for it. If the state is 'onboarded', stop and say so.
3. Ask the human: is this repo part of a known project already in the KB?
   Show candidates with `kb_list` over projects.
   - If yes, run the component onboarding for that project.
   - If no, run the full project onboarding.
"""

_PROJECT = """\
# Onboarding project: {workspace}

1. Confirm kb_onboarding_status(workspace={workspace!r}) is 'absent' or 'partial'.
   'absent' bootstraps every canonical file; 'partial' completes an existing
   project by committing ONLY the missing_files the status reports (an already
   present file is never overwritten). If it is already 'onboarded', stop.
2. Inventory local docs (README.md, AGENTS.md, CLAUDE.md, .rules/, docs/).
{interview}
3. Assemble imported files + interview answers and call kb_bootstrap_project
   (workspace={workspace!r}, workspace_remote_url={workspace_remote_url!r}). One
   atomic commit on high confidence, else one pending bundle. On 'partial' you
   may supply the full set; the server narrows it to the missing files.
{cleanup}
"""

_COMPONENT = """\
# Onboarding component: {component} (project {workspace})

1. Read the parent project's AGENTS.md first (kb_get projects-{workspace}-AGENTS)
   to inherit its conventions and lessons; only ask for component-specific deltas.
2. Confirm kb_onboarding_status(workspace={workspace!r}, component={component!r})
   is 'absent' or 'partial'. 'partial' completes the component by committing ONLY
   the missing_files reported; existing files are never overwritten. Stop if it is
   already 'onboarded'.
{interview}
3. Call kb_bootstrap_project(workspace={workspace!r}, component={component!r},
   component_remote_url={component_remote_url!r}).
{cleanup}
"""


def render_playbook(
    kind: str,
    *,
    workspace: str | None = None,
    component: str | None = None,
    workspace_remote_url: str | None = None,
    component_remote_url: str | None = None,
) -> str:
    if kind == "dispatch":
        return _DISPATCH
    if kind == "project":
        return _PROJECT.format(
            workspace=workspace, workspace_remote_url=workspace_remote_url,
            interview=_INTERVIEW, cleanup=_CLEANUP,
        )
    if kind == "component":
        return _COMPONENT.format(
            workspace=workspace, component=component,
            component_remote_url=component_remote_url,
            interview=_INTERVIEW, cleanup=_CLEANUP,
        )
    raise ValueError(f"unknown playbook kind: {kind!r}")
