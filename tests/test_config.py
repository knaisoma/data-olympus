"""Tests for config module."""
from pathlib import Path

import pytest

from data_olympus.config import Config, load_config


def test_load_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults apply when env vars are unset."""
    for key in [
        "KB_MAIN_PATH",
        "KB_INDEX_PATH",
        "KB_REMOTE_URL",
        "KB_SYNC_INTERVAL_SEC",
        "KB_STALENESS_DEGRADED_SEC",
        "KB_CONFIDENCE_THRESHOLD",
        "KB_HTTP_PORT",
    ]:
        monkeypatch.delenv(key, raising=False)
    cfg = load_config()
    assert cfg.kb_main_path == Path("/kb-main")
    assert cfg.kb_index_path == Path("/index/kb.db")
    assert cfg.kb_remote_url == ""
    assert cfg.sync_interval_sec == 60
    assert cfg.staleness_degraded_sec == 600
    assert cfg.confidence_threshold == 0.85
    assert cfg.http_port == 8080


def test_load_config_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Env vars override defaults."""
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "main"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_SYNC_INTERVAL_SEC", "30")
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.9")
    monkeypatch.setenv("KB_HTTP_PORT", "9090")
    cfg = load_config()
    assert cfg.kb_main_path == tmp_path / "main"
    assert cfg.kb_index_path == tmp_path / "idx.db"
    assert cfg.sync_interval_sec == 30
    assert cfg.confidence_threshold == 0.9
    assert cfg.http_port == 9090


def test_load_config_rejects_bad_threshold(monkeypatch: pytest.MonkeyPatch) -> None:
    """Threshold outside [0, 1] must raise."""
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "1.5")
    with pytest.raises(ValueError, match="KB_CONFIDENCE_THRESHOLD"):
        load_config()


def test_config_is_frozen() -> None:
    """Config instances are immutable."""
    cfg = Config(
        kb_main_path=Path("/tmp/a"),
        kb_index_path=Path("/tmp/b.db"),
        kb_remote_url="git@example.com:x/y.git",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        confidence_threshold=0.85,
        http_port=8080,
    )
    with pytest.raises((AttributeError, TypeError)):
        cfg.http_port = 9090  # type: ignore[misc]


def test_config_loads_new_write_env_vars(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "git@example.com:x/y.git")
    monkeypatch.setenv("KB_SYNC_INTERVAL_SEC", "60")
    monkeypatch.setenv("KB_STALENESS_DEGRADED_SEC", "600")
    monkeypatch.setenv("KB_CONFIDENCE_THRESHOLD", "0.85")
    monkeypatch.setenv("KB_HTTP_PORT", "8080")
    monkeypatch.setenv("KB_WORKTREE_ROOT", "/wts")
    monkeypatch.setenv("KB_PENDING_ROOT", "/state/pending")
    monkeypatch.setenv("KB_PUSH_QUEUE_ROOT", "/state/push-queue")
    monkeypatch.setenv("KB_WRITE_BLOCK_TIERS", "T1,T2")
    monkeypatch.setenv("KB_WRITE_BLOCK_PATHS", "decisions/DEC-008-*.md")
    monkeypatch.setenv("KB_RATE_LIMIT_PER_HOUR", "50")
    monkeypatch.setenv("KB_PENDING_TIMEOUT_SEC", "86400")
    monkeypatch.setenv("KB_PENDING_QUEUE_CAP", "100")
    monkeypatch.setenv("KB_WORKTREE_IDLE_SEC", "1800")
    monkeypatch.setenv("KB_GIT_KEY_PATH", "/tmp/git-key")

    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.worktree_root == "/wts"
    assert cfg.pending_root == "/state/pending"
    assert cfg.push_queue_root == "/state/push-queue"
    assert cfg.write_block_tiers == ["T1", "T2"]
    assert cfg.write_block_paths == ["decisions/DEC-008-*.md"]
    assert cfg.rate_limit_per_hour == 50
    assert cfg.pending_timeout_sec == 86400
    assert cfg.pending_queue_cap == 100
    assert cfg.worktree_idle_sec == 1800
    assert cfg.git_key_path == "/tmp/git-key"


def test_config_defaults_for_new_write_vars(monkeypatch, tmp_path) -> None:
    """Empty defaults for the policy blocklist; sensible defaults for the rest."""
    for var in ["KB_WORKTREE_ROOT", "KB_PENDING_ROOT", "KB_PUSH_QUEUE_ROOT",
                "KB_WRITE_BLOCK_TIERS", "KB_WRITE_BLOCK_PATHS", "KB_RATE_LIMIT_PER_HOUR",
                "KB_PENDING_TIMEOUT_SEC", "KB_PENDING_QUEUE_CAP", "KB_WORKTREE_IDLE_SEC",
                "KB_GIT_KEY_PATH"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")

    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.worktree_root == "/kb-worktrees"
    assert cfg.pending_root == "/state/pending"
    assert cfg.push_queue_root == "/state/push-queue"
    assert cfg.write_block_tiers == []
    assert cfg.write_block_paths == []
    assert cfg.rate_limit_per_hour == 100
    assert cfg.pending_timeout_sec == 86400
    assert cfg.pending_queue_cap == 100
    assert cfg.worktree_idle_sec == 3600
    assert cfg.git_key_path == "/tmp/git-key"


def test_config_loads_audit_log_path(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KB_MAIN_PATH", str(tmp_path / "kb"))
    monkeypatch.setenv("KB_INDEX_PATH", str(tmp_path / "idx.db"))
    monkeypatch.setenv("KB_REMOTE_URL", "")
    monkeypatch.setenv("KB_AUDIT_LOG_PATH", "/custom/audit.log")
    from data_olympus.config import load_config
    cfg = load_config()
    assert cfg.audit_log_path == "/custom/audit.log"
