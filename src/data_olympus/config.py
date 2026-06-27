"""Configuration loading from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable runtime configuration."""

    kb_main_path: Path
    kb_index_path: Path
    kb_remote_url: str
    sync_interval_sec: int
    staleness_degraded_sec: int
    confidence_threshold: float
    http_port: int
    worktree_root: str = "/kb-worktrees"
    pending_root: str = "/state/pending"
    push_queue_root: str = "/state/push-queue"
    write_block_tiers: list[str] = field(default_factory=list)
    write_block_paths: list[str] = field(default_factory=list)
    rate_limit_per_hour: int = 100
    pending_timeout_sec: int = 86400
    pending_queue_cap: int = 100
    worktree_idle_sec: int = 3600
    git_key_path: str = "/tmp/git-key"
    audit_log_path: str = "/state/audit/events.log"
    auth_token: str = ""
    auth_principals: list[dict] = field(default_factory=list)
    consult_ttl_sec: int = 300
    ledger_path: str = "/state/ledger.json"


def _split_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def load_config() -> Config:
    """Load configuration from environment, applying defaults."""
    threshold = float(os.environ.get("KB_CONFIDENCE_THRESHOLD", "0.85"))
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(
            f"KB_CONFIDENCE_THRESHOLD must be in [0, 1]; got {threshold}"
        )
    worktree_root = os.getenv("KB_WORKTREE_ROOT", "/kb-worktrees")
    pending_root = os.getenv("KB_PENDING_ROOT", "/state/pending")
    push_queue_root = os.getenv("KB_PUSH_QUEUE_ROOT", "/state/push-queue")
    write_block_tiers = _split_csv(os.getenv("KB_WRITE_BLOCK_TIERS", ""))
    write_block_paths = _split_csv(os.getenv("KB_WRITE_BLOCK_PATHS", ""))
    rate_limit_per_hour = int(os.getenv("KB_RATE_LIMIT_PER_HOUR", "100"))
    pending_timeout_sec = int(os.getenv("KB_PENDING_TIMEOUT_SEC", "86400"))
    pending_queue_cap = int(os.getenv("KB_PENDING_QUEUE_CAP", "100"))
    worktree_idle_sec = int(os.getenv("KB_WORKTREE_IDLE_SEC", "3600"))
    git_key_path = os.getenv("KB_GIT_KEY_PATH", "/tmp/git-key")
    audit_log_path = os.getenv("KB_AUDIT_LOG_PATH", "/state/audit/events.log")
    auth_token = os.getenv("KB_AUTH_TOKEN", "")
    from data_olympus.principals import parse_principals_env
    auth_principals = parse_principals_env(os.getenv("KB_AUTH_PRINCIPALS", ""))
    consult_ttl_sec = int(os.getenv("KB_CONSULT_TTL_SEC", "300"))
    ledger_path = os.getenv("KB_LEDGER_PATH", "/state/ledger.json")
    return Config(
        kb_main_path=Path(os.environ.get("KB_MAIN_PATH", "/kb-main")),
        kb_index_path=Path(os.environ.get("KB_INDEX_PATH", "/index/kb.db")),
        kb_remote_url=os.environ.get("KB_REMOTE_URL", ""),
        sync_interval_sec=int(os.environ.get("KB_SYNC_INTERVAL_SEC", "60")),
        staleness_degraded_sec=int(os.environ.get("KB_STALENESS_DEGRADED_SEC", "600")),
        confidence_threshold=threshold,
        http_port=int(os.environ.get("KB_HTTP_PORT", "8080")),
        worktree_root=worktree_root,
        pending_root=pending_root,
        push_queue_root=push_queue_root,
        write_block_tiers=write_block_tiers,
        write_block_paths=write_block_paths,
        rate_limit_per_hour=rate_limit_per_hour,
        pending_timeout_sec=pending_timeout_sec,
        pending_queue_cap=pending_queue_cap,
        worktree_idle_sec=worktree_idle_sec,
        git_key_path=git_key_path,
        audit_log_path=audit_log_path,
        auth_token=auth_token,
        auth_principals=auth_principals,
        consult_ttl_sec=consult_ttl_sec,
        ledger_path=ledger_path,
    )
