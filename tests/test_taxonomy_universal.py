"""The default taxonomy classifies every path under ``universal/`` as T1.

The seven ``universal/<subdir>/`` rules keep their categories; any other path
under ``universal/`` (a loose file, or a subdirectory outside those seven)
falls to a catch-all T1 rule instead of the unmatched ``meta`` tier. The write
blocklist classifies a target by this path tier, so ``KB_WRITE_BLOCK_TIERS=T1``
covers the whole of ``universal/``.
"""
from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from data_olympus.auth import PathBlocklist
from data_olympus.git_ops import GitOps
from data_olympus.index import _DEFAULT_PATH_RULES, _classify_by_path
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import kb_propose_edit_fn, kb_propose_memory_fn
from data_olympus.worktrees import WorktreeRegistry

if TYPE_CHECKING:
    from pathlib import Path

SPECIFIC = (
    ("universal/foundation/x.md", "foundation"),
    ("universal/quality/x.md", "quality"),
    ("universal/security/x.md", "security"),
    ("universal/infrastructure/x.md", "infrastructure"),
    ("universal/database/x.md", "database"),
    ("universal/api/x.md", "api"),
    ("universal/services/x.md", "services"),
)

OTHER_UNIVERSAL = (
    "universal/README.md",
    "universal/process/x.md",
    "universal/testing/deep/nested.md",
)


@pytest.fixture(autouse=True)
def _default_taxonomy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_TAXONOMY_PATH", raising=False)


@pytest.mark.parametrize(("path", "category"), SPECIFIC)
def test_specific_universal_prefixes_keep_their_categories(path: str, category: str) -> None:
    assert _classify_by_path(path) == ("T1", category)


@pytest.mark.parametrize("path", OTHER_UNIVERSAL)
def test_other_universal_paths_classify_as_t1(path: str) -> None:
    assert _classify_by_path(path) == ("T1", "universal")


def test_catch_all_follows_the_specific_rules() -> None:
    prefixes = [p for p, _, _ in _DEFAULT_PATH_RULES]
    catch_all = prefixes.index("universal/")
    specific = [
        i for i, p in enumerate(prefixes)
        if p.startswith("universal/") and p != "universal/"
    ]
    assert len(specific) == 7
    assert all(i < catch_all for i in specific)


@pytest.mark.parametrize("path", ("universalx/a.md", "operator/universal/a.md", "README.md"))
def test_lookalike_paths_stay_unmatched(path: str) -> None:
    assert _classify_by_path(path) == ("meta", "meta")


# ---- the write blocklist follows the path tier ------------------------------

def _env() -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}


def _state(tmp_path: Path) -> dict[str, object]:
    repo = tmp_path / "main"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main"], cwd=repo, check=True, env=_env())
    (repo / "seed.md").write_text("seed")
    subprocess.run(["git", "add", "seed.md"], cwd=repo, check=True, env=_env())
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True, env=_env())
    return {
        "worktrees": WorktreeRegistry(git=GitOps(repo), worktree_root=str(tmp_path / "wts")),
        "push_queue": PushQueue(queue_root=str(tmp_path / "push-q")),
        "pending": PendingQueue(pending_root=str(tmp_path / "pending")),
        "rate_limiter": SlidingWindowLimiter(max_per_hour=10),
        "blocklist": PathBlocklist(tier_blocks=["T1"], path_blocks=[]),
        "remote_addr": "10.0.0.1",
    }


@pytest.mark.parametrize("path", ("universal/README.md", "universal/process/x.md",
                                  "universal/foundation/x.md"))
def test_t1_block_rejects_edits_anywhere_under_universal(tmp_path: Path, path: str) -> None:
    st = _state(tmp_path)
    resp = kb_propose_edit_fn(
        target_path=path, postimage="# x\n", base_commit="HEAD", base_blob_sha=None,
        target_file_hash=None, reason="r", source_session="s", agent_identity="claude",
        confidence=0.99, confidence_threshold=0.85, **st,  # type: ignore[arg-type]
    )
    assert resp.status == "rejected_path_blocked"
    assert resp.target_tier == "T1"


@pytest.mark.parametrize("prefix", ("universal/", "universal/process/"))
def test_t1_block_rejects_memories_under_universal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str,
) -> None:
    monkeypatch.setenv("KB_MEMORY_INBOX_PREFIX", prefix)
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@e.com")
    st = _state(tmp_path)
    resp = kb_propose_memory_fn(
        text="x", tags=[], source_session="s", agent_identity="claude",
        confidence=0.99, confidence_threshold=0.85, **st,  # type: ignore[arg-type]
    )
    assert resp.status == "rejected_path_blocked"
    assert resp.target_tier == "T1"
