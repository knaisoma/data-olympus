"""Onboarding logic: compute status + rename detection."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

CANONICAL_T3_FILES = ("README.md", "AGENTS.md")
CANONICAL_T4_FILES = ("AGENTS.md",)


@dataclass(frozen=True, slots=True)
class RenameCandidate:
    target_tier: Literal["T3", "T4"]
    target_workspace: str
    target_component: str | None
    confidence: float
    matched_via: Literal["git_remote_url"]


@dataclass(frozen=True, slots=True)
class OnboardingStatus:
    state: Literal["absent", "partial", "onboarded", "rename_candidate"]
    workspace: str
    component: str | None
    missing_files: list[str] = field(default_factory=list)
    rename_candidates: list[RenameCandidate] = field(default_factory=list)


def _normalize_remote_url(url: str) -> str:
    """Collapse ssh and https variants of the same repo to a single canonical
    form. git@github.com:org/repo.git == https://github.com/org/repo == ...etc."""
    s = url.strip().lower()
    s = re.sub(r"^git@([^:]+):", r"https://\1/", s)
    s = re.sub(r"^ssh://git@", "https://", s)
    s = re.sub(r"\.git/?$", "", s)
    return re.sub(r"/$", "", s)


def _has(entries: list[dict[str, Any]], filename: str) -> bool:
    return any(e["path"].endswith("/" + filename) for e in entries)


def compute_status(
    *,
    workspace: str,
    component: str | None,
    workspace_remote_url: str | None,
    component_remote_url: str | None,
    idx: Any,
) -> OnboardingStatus:
    if component is None:
        t3_root = f"projects/{workspace}/"
        t3_entries = idx.list_by_prefix(t3_root, exclude_under="components/")
        if t3_entries:
            missing = [f for f in CANONICAL_T3_FILES if not _has(t3_entries, f)]
            return OnboardingStatus(
                state="onboarded" if not missing else "partial",
                workspace=workspace, component=None,
                missing_files=missing, rename_candidates=[],
            )
        cands = (
            detect_rename_candidate(workspace_remote_url, idx, target_tier="T3")
            if workspace_remote_url else []
        )
        if cands:
            return OnboardingStatus(
                state="rename_candidate", workspace=workspace, component=None,
                missing_files=list(CANONICAL_T3_FILES), rename_candidates=cands,
            )
        return OnboardingStatus(
            state="absent", workspace=workspace, component=None,
            missing_files=list(CANONICAL_T3_FILES), rename_candidates=[],
        )

    t4_root = f"projects/{workspace}/components/{component}/"
    t4_entries = idx.list_by_prefix(t4_root)
    if t4_entries:
        missing = [f for f in CANONICAL_T4_FILES if not _has(t4_entries, f)]
        return OnboardingStatus(
            state="onboarded" if not missing else "partial",
            workspace=workspace, component=component,
            missing_files=missing, rename_candidates=[],
        )
    cands = (
        detect_rename_candidate(component_remote_url, idx, target_tier="T4")
        if component_remote_url else []
    )
    if cands:
        return OnboardingStatus(
            state="rename_candidate", workspace=workspace, component=component,
            missing_files=list(CANONICAL_T4_FILES), rename_candidates=cands,
        )
    return OnboardingStatus(
        state="absent", workspace=workspace, component=component,
        missing_files=list(CANONICAL_T4_FILES), rename_candidates=[],
    )


def detect_rename_candidate(
    remote_url: str | None,
    idx: Any,
    *,
    target_tier: Literal["T3", "T4"],
) -> list[RenameCandidate]:
    if not remote_url:
        return []
    norm = _normalize_remote_url(remote_url)
    out: list[RenameCandidate] = []
    for entry in idx.list_with_remote_url():
        if not entry.get("git_remote_url"):
            continue
        if _normalize_remote_url(entry["git_remote_url"]) != norm:
            continue
        if entry.get("tier") != target_tier:
            continue
        path = entry["path"]
        parts = path.split("/")
        if target_tier == "T3" and len(parts) >= 2:
            out.append(RenameCandidate(
                target_tier="T3", target_workspace=parts[1],
                target_component=None,
                confidence=1.0, matched_via="git_remote_url",
            ))
        elif target_tier == "T4" and len(parts) >= 4:
            out.append(RenameCandidate(
                target_tier="T4", target_workspace=parts[1],
                target_component=parts[3],
                confidence=1.0, matched_via="git_remote_url",
            ))
    return out
