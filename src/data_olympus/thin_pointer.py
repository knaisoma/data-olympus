"""Canonical thin-pointer text. data-olympus owns this so every deployment
emits identical pointers; the marker's first-line placement lets a project-repo
lint validate drift unambiguously."""
from __future__ import annotations

THIN_POINTER_MARKER = "<!-- KB-THIN-POINTER v1 -->"


def render_thin_pointer(*, kb_path: str, kb_id: str, kind: str = "project") -> str:
    """Render the replacement body for a project-repo doc whose content now
    lives in the knowledge base. `kb_path` is the KB-relative git path;
    `kb_id` is the retrievable document id; `kind` is 'project' or 'component'."""
    return (
        f"{THIN_POINTER_MARKER}\n"
        f"The canonical documentation for this {kind} lives in the knowledge base\n"
        f"managed by data-olympus. This file is intentionally minimal.\n\n"
        f"Source of truth (git path in the knowledge base):\n\n"
        f"  {kb_path}\n\n"
        f"Agents: retrieve it with `kb get {kb_id}` or the data-olympus MCP `kb_get` tool.\n"
    )
