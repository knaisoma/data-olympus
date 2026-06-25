"""Shared pytest fixtures."""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(scope="session", autouse=True)
def _force_git_default_branch_main(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Pin `git init` to default to 'main' for the whole test session.

    GitHub runners leave init.defaultBranch unset and fall back to 'master',
    while local dev boxes usually set it to 'main'. That mismatch broke CI when a
    fixture ran `git push origin main` against a repo whose default branch was
    'master' ("src refspec main does not match any"). Pointing GIT_CONFIG_GLOBAL
    at a temp config pins the default to 'main' for any subprocess that inherits
    os.environ (the _git_env()/_env() helpers do), so a future unpinned
    `git init` cannot reintroduce the trap.
    """
    cfg = tmp_path_factory.mktemp("gitconfig") / "config"
    cfg.write_text("[init]\n\tdefaultBranch = main\n")
    prev = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = prev


@pytest.fixture
def tmp_kb(tmp_path: Path) -> Path:
    """A tiny KB with markdown files mimicking the v2 KB structure.

    Returns the root of the KB. Contains:
      - 3 T1 universal/foundation STDs
      - 1 T2 tech-stacks/backend-nestjs STD (for tier filter tests)
      - 1 T3 projects/example-project/README.md (with git_remote_url for rename detection)
      - 1 T4 projects/example-project/components/payment-service/AGENTS.md (also with remote url)
      - 1 GDEC, 1 workflow, operator-overrides + tooling files
    """
    kb = tmp_path / "kb"
    foundation = kb / "universal" / "foundation"
    foundation.mkdir(parents=True)
    nestjs = kb / "tech-stacks" / "backend-nestjs"
    nestjs.mkdir(parents=True)
    example_project = kb / "projects" / "example-project"
    example_project.mkdir(parents=True)
    payment_component = example_project / "components" / "payment-service"
    payment_component.mkdir(parents=True)
    decisions = kb / "decisions"
    decisions.mkdir(parents=True)

    (foundation / "STD-U-001-test-policy.md").write_text(
        "---\nid: STD-U-001\ntier: T1\ncategory: foundation\ntags: [policy, test]\n"
        "title: Test Policy\n---\n# STD-U-001: Test Policy\n\n"
        "Body content for testing search over the worktree word.\n"
    )
    (foundation / "STD-U-002-writing-style.md").write_text(
        "---\nid: STD-U-002\ntier: T1\ncategory: foundation\ntags: [style]\n"
        "title: Writing Style\n---\n# STD-U-002: Writing Style\n\n"
        "Use bulleted lists. Avoid em-dashes.\n"
    )
    (foundation / "STD-U-007-disagreement-format.md").write_text(
        "---\nid: STD-U-007\ntier: T1\ncategory: foundation\ntags: [collaboration]\n"
        "title: Disagreement Format\n---\n# STD-U-007: Disagreement Format\n\n"
        "Use PROBLEM, WHY, BETTER APPROACH, BENEFITS.\n"
    )
    (nestjs / "STD-BN-001-module-structure.md").write_text(
        "---\nid: STD-BN-001\ntier: T2\ncategory: stack:backend-nestjs\ntags: [nestjs]\n"
        "title: NestJS Module Structure\n---\n# STD-BN-001\n\n"
        "Module per feature.\n"
    )
    (example_project / "README.md").write_text(
        "---\nid: projects-example-project-README\ntier: T3\ncategory: project:example-project\n"
        "git_remote_url: git@github.com:example-org/example-project.git\n"
        "title: Example Project\n---\n# Example Project\n\nProject overview.\n"
    )
    (payment_component / "AGENTS.md").write_text(
        "---\nid: projects-example-project-components-payment-service-AGENTS\ntier: T4\n"
        "category: component:example-project/payment-service\n"
        "git_remote_url: git@github.com:example-org/payment-service.git\n"
        "title: payment-service AGENTS\n---\n# payment-service\n\n"
        "Component-scoped rules.\n"
    )
    (decisions / "GDEC-008-instruction-file-standard.md").write_text(
        "---\nid: GDEC-008\ntier: decisions\ntags: [agents]\n"
        "title: Instruction File Standard\n---\n# GDEC-008\n\n"
        "AGENTS.md is canonical.\n"
    )
    workflows = kb / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "WF-001-ship-something.md").write_text(
        "# WF-001: Ship Something\n\nA workflow without front matter.\n"
    )
    operator_overrides = kb / "operator" / "agent-overrides"
    operator_overrides.mkdir(parents=True)
    (operator_overrides / "claude.md").write_text(
        "# Claude override\n\nClaude-specific notes.\n"
    )
    tooling = kb / "tooling"
    tooling.mkdir(parents=True)
    (tooling / "worktrees.md").write_text(
        "# Worktrees\n\nWorktree conventions.\n"
    )
    return kb


@pytest.fixture
def tmp_git_kb(tmp_kb: Path) -> Path:
    """A tmp_kb that is also a git repo with one commit on main."""
    kb = tmp_kb
    env = {"GIT_AUTHOR_NAME": "tester", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "tester", "GIT_COMMITTER_EMAIL": "t@example.com",
           "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=kb, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=kb, check=True, env=env)
    return kb


@pytest.fixture
def tmp_index_path(tmp_path: Path) -> Path:
    """A path for a sqlite index that does not yet exist."""
    return tmp_path / "index.db"


@pytest.fixture
def status_kb(tmp_path: Path) -> Path:
    """A KB with status/type frontmatter and a supersession pair, all matching
    the word 'caching', so status/type filters can be exercised."""
    kb = tmp_path / "status-kb"
    d = kb / "universal" / "foundation"
    d.mkdir(parents=True)
    (d / "STD-OLD.md").write_text(
        "---\nid: STD-OLD\ntier: T1\ntype: standard\nstatus: superseded\n"
        "superseded_by: STD-NEW\n---\n# Caching rule\n\nOld caching guidance.\n"
    )
    (d / "STD-NEW.md").write_text(
        "---\nid: STD-NEW\ntier: T1\ntype: standard\nstatus: active\n"
        "supersedes: STD-OLD\n---\n# Caching rule\n\nCurrent caching guidance.\n"
    )
    decisions = kb / "decisions"
    decisions.mkdir(parents=True)
    (decisions / "DEC-1.md").write_text(
        "---\nid: DEC-1\ntier: decisions\ntype: decision\nstatus: accepted\n"
        "---\n# Caching decision\n\nWe chose caching.\n"
    )
    return kb
