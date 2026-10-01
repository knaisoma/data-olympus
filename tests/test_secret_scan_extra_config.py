"""KB_SECRET_SCAN_EXTRA_PATTERNS is parsed once at startup, keeping quantifiers whole."""
from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

import data_olympus.server as server
from data_olympus import write_gate
from data_olympus.auth import PathBlocklist
from data_olympus.config import load_config
from data_olympus.git_ops import GitOps
from data_olympus.pending import PendingQueue
from data_olympus.push_queue import PushQueue
from data_olympus.rate_limit import SlidingWindowLimiter
from data_olympus.tools_write import kb_propose_memory_fn
from data_olympus.worktrees import WorktreeRegistry
from data_olympus.write_gate import load_extra_secret_patterns, scan_postimage_for_secrets

if TYPE_CHECKING:
    from pathlib import Path

ACME = r"ACME_[A-Z0-9]{20,40}"
ACME_VALUE = "ACME_" + "A1B2C3D4E5F6G7H8J9K0L1M2"


def _bundle_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kb = tmp_path / "kb"
    (kb / "decisions").mkdir(parents=True)
    (kb / "decisions" / "D-1.md").write_text(
        "---\nid: D-1\ntype: decision\nstatus: active\ntier: meta\n"
        "title: Seed\n---\n# Seed\n\nSeed document.\n", encoding="utf-8")
    monkeypatch.setenv("KB_MAIN_PATH", str(kb))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "kb.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_DISABLE_VERSION_CHECK", "1")


def _propose_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.com",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.com"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "seed.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    pq = PushQueue(queue_root=str(tmp_path / "pq"))
    resp = kb_propose_memory_fn(
        text=text, tags=[], source_session="s", agent_identity="agent",
        confidence=0.95, confidence_threshold=0.85,
        worktrees=WorktreeRegistry(git=GitOps(repo), worktree_root=str(tmp_path / "wts")),
        push_queue=pq, pending=PendingQueue(pending_root=str(tmp_path / "pending")),
        rate_limiter=SlidingWindowLimiter(max_per_hour=100),
        blocklist=PathBlocklist([], []), remote_addr="1.2.3.4",
    )
    return resp, pq


@pytest.mark.parametrize("raw", [f'["{ACME}"]'.replace("\\", "\\\\"), ACME])
def test_bounded_quantifier_pattern_rejects_a_matching_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str,
) -> None:
    _bundle_env(tmp_path, monkeypatch)
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", raw)
    cfg = load_config()
    assert cfg.secret_scan_extra_patterns == (ACME,)
    server.build_app_from_config(cfg, bootstrap_now=False)
    # The running gate uses the parsed setting, not a later environment value.
    monkeypatch.delenv("KB_SECRET_SCAN_EXTRA_PATTERNS")
    resp, pq = _propose_memory(
        tmp_path, monkeypatch, f"The staging deploy token is {ACME_VALUE} for reference.")
    assert resp.status == "rejected_secret_detected"
    assert resp.matching_pattern == "custom_1"
    assert pq.size() == 0


def test_two_comma_separated_patterns_load_as_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", "FOO-[0-9]{4}, BAR-[a-z,]{3,5}")
    assert load_config().secret_scan_extra_patterns == ("FOO-[0-9]{4}", "BAR-[a-z,]{3,5}")


def test_json_array_form_loads_each_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", '["FOO-[0-9]{4}", "a,b"]')
    assert load_config().secret_scan_extra_patterns == ("FOO-[0-9]{4}", "a,b")


def test_escaped_comma_and_brackets_stay_in_one_pattern(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", r"X\,Y,[]a,]{2},Z")
    assert load_config().secret_scan_extra_patterns == (r"X\,Y", "[]a,]{2}", "Z")


def test_unset_setting_loads_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KB_SECRET_SCAN_EXTRA_PATTERNS", raising=False)
    assert load_config().secret_scan_extra_patterns == ()


@pytest.mark.parametrize("raw,index", [
    ("FOO-[0-9]{4},(unclosed", 2),
    ('["FOO", "(unclosed"]', 2),
    ("(a+)+", 1),
    ('["FOO", 7]', 2),
    ('["FOO", ""]', 2),
])
def test_unusable_pattern_fails_startup_naming_its_index(
    monkeypatch: pytest.MonkeyPatch, raw: str, index: int,
) -> None:
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", raw)
    with pytest.raises(ValueError, match="KB_SECRET_SCAN_EXTRA_PATTERNS") as info:
        load_config()
    message = str(info.value)
    assert f"entry {index}" in message
    assert "unclosed" not in message
    assert "a+" not in message


def test_fallback_loader_keeps_a_bounded_quantifier_whole() -> None:
    extra = load_extra_secret_patterns(ACME)
    assert [p.pattern for _name, p in extra] == [ACME]
    assert not scan_postimage_for_secrets(
        postimage=f"key {ACME_VALUE}\n", extra_patterns=extra).ok


def test_unconfigured_gate_reads_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a loaded configuration (library use, tests) the gate falls back
    to the environment, parsed the same way."""
    write_gate.configure_extra_secret_patterns(None)
    monkeypatch.setenv("KB_SECRET_SCAN_EXTRA_PATTERNS", ACME)
    assert not scan_postimage_for_secrets(postimage=f"key {ACME_VALUE}\n").ok
