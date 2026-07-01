"""Configuration loading from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    rate_limit_per_ip_per_hour: int = 0
    max_text_bytes: int = 262144
    max_postimage_bytes: int = 1048576
    max_body_bytes: int = 2097152
    max_bootstrap_files: int = 50
    pending_timeout_sec: int = 86400
    pending_queue_cap: int = 100
    worktree_idle_sec: int = 3600
    git_key_path: str = "/tmp/git-key"
    audit_log_path: str = "/state/audit/events.log"
    audit_hmac_key: str = ""
    auth_token: str = ""
    auth_principals: list[dict[str, Any]] = field(default_factory=list)
    consult_ttl_sec: int = 300
    ledger_path: str = "/state/ledger.json"
    # Streamable-http session reaping: terminate transports idle beyond this many
    # seconds to bound _server_instances (see session_metrics). 0 disables the
    # reaper (observability-only). The scan runs every session_reap_interval_sec.
    session_idle_timeout_sec: int = 1800
    session_reap_interval_sec: int = 60
    # Optional override for the status-aware reranker's status->weight map
    # (issue #37). None means "use the Index built-in default map". A negative
    # weight boosts an in-force status, a positive one penalizes a retired one.
    status_weights: dict[str, float] | None = None
    read_only: bool = False
    # Corpus co-occurrence query expansion (issue #40). Default ON. The build-time
    # table is bounded by ``cooccurrence_k`` related terms per term above the
    # ``cooccurrence_min_count`` / ``cooccurrence_min_pmi`` thresholds. These are
    # surfaced here for discoverability; the build path in Index.build reads the
    # same env vars directly (mirroring the synonym expander), so the CLI indexer
    # honours them without threading Config through.
    cooccurrence_enabled: bool = True
    cooccurrence_k: int = 5
    cooccurrence_min_count: int = 2
    cooccurrence_min_pmi: float = 0.0
    # Trigram fuzzy-match fallback (issue #41). Default OFF so existing search
    # behaviour is unchanged. When on, a primary FTS query returning at or below
    # ``trigram_fallback_threshold`` hits is backfilled from the trigram index so
    # a typo or partial identifier still reaches its document; the backfill only
    # ever appends after primary hits. Surfaced here for discoverability; the
    # build always creates the trigram table (cost is one extra insert/doc), and
    # Index.search reads these to gate the query-time fallback.
    trigram_fallback_enabled: bool = False
    trigram_fallback_threshold: int = 3


def _split_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _load_status_weights(raw: str) -> dict[str, float] | None:
    """Parse KB_STATUS_WEIGHTS: a JSON object of ``{status: weight}``.

    Empty/unset returns None (the Index applies its built-in default map). A
    malformed value raises ValueError rather than silently falling back, so a
    misconfigured deployment fails loudly instead of shipping default ranking.
    """
    raw = raw.strip()
    if not raw:
        return None
    import json
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"KB_STATUS_WEIGHTS must be a JSON object; {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            "KB_STATUS_WEIGHTS must be a JSON object of {status: weight}; "
            f"got {type(data).__name__}"
        )
    return {str(k): float(v) for k, v in data.items()}
def _env_bool(raw: str) -> bool:
    """Parse a truthy env string. True for 1/true/yes/on (case-insensitive);
    everything else (including empty) is False."""
    return raw.strip().lower() in ("1", "true", "yes", "on")


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
    rate_limit_per_ip_per_hour = int(os.getenv("KB_RATE_LIMIT_PER_IP_PER_HOUR", "0"))
    max_text_bytes = int(os.getenv("KB_MAX_TEXT_BYTES", "262144"))
    max_postimage_bytes = int(os.getenv("KB_MAX_POSTIMAGE_BYTES", "1048576"))
    max_body_bytes = int(os.getenv("KB_MAX_BODY_BYTES", "2097152"))
    max_bootstrap_files = int(os.getenv("KB_MAX_BOOTSTRAP_FILES", "50"))
    pending_timeout_sec = int(os.getenv("KB_PENDING_TIMEOUT_SEC", "86400"))
    pending_queue_cap = int(os.getenv("KB_PENDING_QUEUE_CAP", "100"))
    worktree_idle_sec = int(os.getenv("KB_WORKTREE_IDLE_SEC", "3600"))
    git_key_path = os.getenv("KB_GIT_KEY_PATH", "/tmp/git-key")
    audit_log_path = os.getenv("KB_AUDIT_LOG_PATH", "/state/audit/events.log")
    audit_hmac_key = os.getenv("KB_AUDIT_HMAC_KEY", "")
    auth_token = os.getenv("KB_AUTH_TOKEN", "")
    from data_olympus.principals import parse_principals_env
    auth_principals = parse_principals_env(os.getenv("KB_AUTH_PRINCIPALS", ""))
    consult_ttl_sec = int(os.getenv("KB_CONSULT_TTL_SEC", "300"))
    ledger_path = os.getenv("KB_LEDGER_PATH", "/state/ledger.json")
    session_idle_timeout_sec = int(os.getenv("KB_SESSION_IDLE_TIMEOUT_SEC", "1800"))
    session_reap_interval_sec = int(os.getenv("KB_SESSION_REAP_INTERVAL_SEC", "60"))
    status_weights = _load_status_weights(os.getenv("KB_STATUS_WEIGHTS", ""))
    read_only = _env_bool(os.getenv("KB_READ_ONLY", ""))
    from data_olympus.cooccurrence import (
        cooccurrence_build_params,
    )
    from data_olympus.cooccurrence import (
        cooccurrence_enabled as _cooc_enabled,
    )
    cooc_enabled = _cooc_enabled()
    cooc_params = cooccurrence_build_params()
    from data_olympus.trigram import (
        trigram_fallback_enabled as _trigram_enabled,
    )
    from data_olympus.trigram import (
        trigram_fallback_threshold as _trigram_threshold,
    )
    trigram_enabled = _trigram_enabled()
    trigram_threshold = _trigram_threshold()
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
        rate_limit_per_ip_per_hour=rate_limit_per_ip_per_hour,
        max_text_bytes=max_text_bytes,
        max_postimage_bytes=max_postimage_bytes,
        max_body_bytes=max_body_bytes,
        max_bootstrap_files=max_bootstrap_files,
        pending_timeout_sec=pending_timeout_sec,
        pending_queue_cap=pending_queue_cap,
        worktree_idle_sec=worktree_idle_sec,
        git_key_path=git_key_path,
        audit_log_path=audit_log_path,
        audit_hmac_key=audit_hmac_key,
        auth_token=auth_token,
        auth_principals=auth_principals,
        consult_ttl_sec=consult_ttl_sec,
        ledger_path=ledger_path,
        session_idle_timeout_sec=session_idle_timeout_sec,
        session_reap_interval_sec=session_reap_interval_sec,
        status_weights=status_weights,
        read_only=read_only,
        cooccurrence_enabled=cooc_enabled,
        cooccurrence_k=int(cooc_params["k"]),
        cooccurrence_min_count=int(cooc_params["min_count"]),
        cooccurrence_min_pmi=float(cooc_params["min_pmi"]),
        trigram_fallback_enabled=trigram_enabled,
        trigram_fallback_threshold=trigram_threshold,
    )
