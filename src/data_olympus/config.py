"""Configuration loading from environment variables."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from data_olympus.tool_discovery import ToolDiscoveryMode, load_tool_discovery_mode

_log = logging.getLogger("data_olympus.config")


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
    # Separate ceiling for the high-frequency /api/v1/gate/check route (the
    # enforcement hook's per-tool-action freshness probe). 0 (default) disables
    # throttling for that route: it is a mandatory, once-per-tool-call, read-only
    # probe, so any fixed hourly quota self-DoSes an active multi-agent fleet
    # (behind ingress all clients collapse to one limiter bucket). Reads are
    # already unthrottled; this keeps gate/check consistent with them. Set a
    # positive value only if you want an explicit backstop.
    gate_check_rate_limit_per_hour: int = 0
    max_text_bytes: int = 262144
    max_postimage_bytes: int = 1048576
    max_body_bytes: int = 2097152
    max_bootstrap_files: int = 50
    pending_timeout_sec: int = 86400
    pending_queue_cap: int = 100
    # Age (seconds) after which a crash-orphaned AUTO-COMMIT path lock is reclaimed
    # by pending_gc_loop (KB_AUTO_COMMIT_LOCK_TTL_SEC). An auto-commit critical
    # section is seconds long, so a lock older than this cannot have a live holder;
    # the default of 600s (10 min) is a wide safety margin. Pending-proposal locks
    # are NEVER reclaimed by this TTL (they live until resolve/expiry). Startup
    # reclaims every auto-commit lock unconditionally (a fresh process holds none).
    auto_commit_lock_ttl_sec: int = 600
    worktree_idle_sec: int = 3600
    git_key_path: str = "/tmp/git-key"
    audit_log_path: str = "/state/audit/events.log"
    audit_hmac_key: str = ""
    # Size-based audit-log rotation threshold in bytes (KB_AUDIT_MAX_BYTES). 0
    # (default) disables rotation: the log stays a single file (backward
    # compatible). When set, a fresh append rotates the live file once it exceeds
    # this size; the tamper-evident hash chain carries across the boundary.
    audit_max_bytes: int = 0
    auth_token: str = ""
    auth_principals: list[dict[str, Any]] = field(default_factory=list)
    consult_ttl_sec: int = 300
    ledger_path: str = "/state/ledger.json"
    # Maintenance ledger (issue #113): committed frontmatter-only markdown doc
    # recording corpus-state audit flags (missing `status`, recently-expired /
    # expiring-soon docs per #107 validity data), refreshed on every index
    # build when the computed state changes. The default lives under the
    # generic `tooling/` taxonomy prefix (index._DEFAULT_PATH_RULES maps it to
    # tier "tooling"); a deployment using a custom KB_TAXONOMY_PATH must make
    # sure this path still resolves inside an INDEXED prefix, or the ledger is
    # committed but never searchable/gettable. Not to be confused with
    # ``ledger_path`` above (the unrelated ConsultationLedger JSON state file).
    maintenance_ledger_path: str = "tooling/maintenance-ledger.md"
    # Window sizes (days) for the "recently expired" / "expiring soon" buckets.
    maintenance_recently_expired_days: int = 30
    maintenance_expiring_soon_days: int = 30
    # Virtual status autofill for legacy (pre-0.4.0) corpora (issue #147 / KNA-69).
    # Default ON. When on, the index build treats a doc missing `status` as
    # `active` IN MEMORY only (the SQLite `docs.status` column and the in-force
    # retrieval view), so a pre-0.4.0 corpus does not silently lose its in-force
    # docs on upgrade. The markdown source file is NEVER touched by the build:
    # Index.build stays a read-only parse. The maintenance ledger still reports
    # the PHYSICAL missing-status gap (so it keeps nagging until the operator runs
    # `data-olympus migrate status --apply`, which is the only lane that writes
    # `status` to disk). Set KB_STATUS_AUTOFILL=off to restore the conservative
    # pre-#147 behavior (a status-less doc is served but never in-force). This
    # intentionally reverses the #114 "never guess status" stance: for the narrow
    # legacy-upgrade case, a seamless default beats conservative flagging.
    status_autofill: bool = True
    # Streamable-http session reaping: terminate transports idle beyond this many
    # seconds to bound _server_instances (see session_metrics). 0 disables the
    # reaper (observability-only). The scan runs every session_reap_interval_sec.
    # An in-flight GET SSE stream is kept non-idle by the activity middleware, so
    # this window only clears sessions whose stream has actually closed (an
    # abandoned handshake); 5 minutes keeps those from piling up.
    session_idle_timeout_sec: int = 300
    session_reap_interval_sec: int = 60
    # How often the activity middleware re-stamps a session while its GET SSE
    # stream is open, so a quiet-but-connected client is never reaped. Clamped
    # below session_idle_timeout_sec at wiring time.
    session_touch_interval_sec: int = 30
    # Optional override for the status-aware reranker's status->weight map
    # (issue #37). None means "use the Index built-in default map". A negative
    # weight boosts an in-force status, a positive one penalizes a retired one.
    status_weights: dict[str, float] | None = None
    read_only: bool = False
    tool_discovery_mode: ToolDiscoveryMode = "search"
    # Corpus co-occurrence query expansion (issue #40). Default ON. The build-time
    # table is bounded by ``cooccurrence_k`` related terms per term above the
    # ``cooccurrence_min_count`` / ``cooccurrence_min_pmi`` thresholds. These are
    # surfaced here for discoverability; the build path in Index.build reads the
    # same env vars directly (mirroring the synonym expander), so the CLI indexer
    # honours them without threading Config through.
    cooccurrence_enabled: bool = True
    cooccurrence_k: int = 5
    cooccurrence_min_count: int = 3
    cooccurrence_min_pmi: float = 0.1
    # Corpus-size floor below which co-occurrence is auto-disabled, and per-doc
    # unique-token cap on O(n^2) pair counting (finding (b), WP2b).
    cooccurrence_min_docs: int = 50
    cooccurrence_max_doc_tokens: int = 400
    # Trigram fuzzy-match fallback (issue #41). Default OFF so existing search
    # behaviour is unchanged. When on, a primary FTS query returning at or below
    # ``trigram_fallback_threshold`` hits is backfilled from the trigram index so
    # a typo or partial identifier still reaches its document; the backfill only
    # ever appends after primary hits (as a RANK_CLASS_BACKFILL class). Surfaced
    # here for discoverability. The trigram table is now created and populated
    # ONLY when the fallback is enabled (KB_TRIGRAM_MODE=on) (finding (c), WP2b),
    # so a default deployment pays no trigram build/size cost; Index.search reads
    # these to gate the query-time fallback.
    trigram_fallback_enabled: bool = False
    trigram_fallback_threshold: int = 3
    # Optional local-embedding hybrid ranking (issue #42). Default OFF so the
    # zero-dependency lexical mode stays the default and no embedding library is
    # imported. When on, docs are embedded at build time (schema v8 doc_vectors)
    # and a hybrid reranker blends normalised bm25 with query-doc cosine.
    # ``embeddings_weight`` in [0, 1] is the cosine fraction of the blend;
    # ``embeddings_model`` names the local ONNX model. Surfaced here for
    # discoverability; the build path reads the same env vars directly (mirroring
    # the trigram/co-occurrence features), so the CLI indexer honours them.
    embeddings_enabled: bool = False
    embeddings_weight: float = 0.35
    embeddings_model: str = "BAAI/bge-small-en-v1.5"
    # Trusted reverse-proxy addresses for X-Forwarded-For handling
    # (KB_TRUSTED_PROXIES, comma-separated). Empty (default) means uvicorn runs
    # with proxy_headers OFF: X-Forwarded-For is ignored and the real remote_addr
    # is the immediate peer, so a client cannot spoof its address to dodge the
    # per-IP rate limiter. Set to the ingress/proxy IP(s) (or ``*`` to trust any
    # peer, ONLY safe when nothing untrusted can reach the port directly) to make
    # the rate limiter see the true client IP behind the proxy. See docs/serving.md.
    trusted_proxies: list[str] = field(default_factory=list)
    # Public hostnames the reverse proxy presents in the Host header
    # (KB_PUBLIC_HOSTNAMES, comma-separated). Mapped to fastmcp's Host-header
    # allowlist when the HTTP app is built (see server.main). Empty (default)
    # leaves fastmcp reading its own FASTMCP_HTTP_ALLOWED_HOSTS env knob, so
    # behaviour is unchanged for existing deployments. This exists so operators
    # configure data-olympus directly (`KB_*`) rather than reaching for the
    # fastmcp dependency's env var. Getting it wrong is the silent-breakage shape
    # that caused the 2026-07-09 kn-dev 421 outage: host-protection on + a public
    # bind + no allowed host -> every proxied request 421s while readiness stays
    # green (direct pod probes pass). See docs/serving.md and server._resolve_allowed_hosts.
    public_hostnames: list[str] = field(default_factory=list)
    # Periodic "a newer version is published" check (issue #146 / KNA-68).
    # ``disable_version_check`` (KB_DISABLE_VERSION_CHECK, default off) makes an
    # air-gapped deployment do ZERO outbound calls: the background task is not
    # spawned at all. When enabled, the task runs once every
    # ``version_check_interval_sec`` (default 24h) on a worker thread and caches
    # the result on ServerState; the /api/v1/health route only READS the cached
    # value, never the network (a blocking urllib lookup on the async request
    # path would freeze the event loop for the timeout and stall readiness).
    disable_version_check: bool = False
    version_check_interval_sec: int = 86400


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
    gate_check_rate_limit_per_hour = int(os.getenv("KB_GATE_CHECK_RATE_LIMIT_PER_HOUR", "0"))
    max_text_bytes = int(os.getenv("KB_MAX_TEXT_BYTES", "262144"))
    max_postimage_bytes = int(os.getenv("KB_MAX_POSTIMAGE_BYTES", "1048576"))
    max_body_bytes = int(os.getenv("KB_MAX_BODY_BYTES", "2097152"))
    max_bootstrap_files = int(os.getenv("KB_MAX_BOOTSTRAP_FILES", "50"))
    pending_timeout_sec = int(os.getenv("KB_PENDING_TIMEOUT_SEC", "86400"))
    pending_queue_cap = int(os.getenv("KB_PENDING_QUEUE_CAP", "100"))
    auto_commit_lock_ttl_sec = int(os.getenv("KB_AUTO_COMMIT_LOCK_TTL_SEC", "600"))
    if auto_commit_lock_ttl_sec <= 0:
        # A non-positive periodic TTL is unsafe: reclaim treats max_age_sec=0 as the
        # unconditional (startup) sweep sentinel, so the running loop would reclaim
        # FRESH auto-commit locks and could free a live one. Clamp to the default.
        _log.warning(
            "KB_AUTO_COMMIT_LOCK_TTL_SEC=%s is non-positive; clamping to 600s "
            "(a non-positive periodic TTL would reclaim live auto-commit locks)",
            auto_commit_lock_ttl_sec,
        )
        auto_commit_lock_ttl_sec = 600
    worktree_idle_sec = int(os.getenv("KB_WORKTREE_IDLE_SEC", "3600"))
    git_key_path = os.getenv("KB_GIT_KEY_PATH", "/tmp/git-key")
    audit_log_path = os.getenv("KB_AUDIT_LOG_PATH", "/state/audit/events.log")
    audit_hmac_key = os.getenv("KB_AUDIT_HMAC_KEY", "")
    audit_max_bytes = int(os.getenv("KB_AUDIT_MAX_BYTES", "0"))
    auth_token = os.getenv("KB_AUTH_TOKEN", "")
    from data_olympus.principals import parse_principals_env
    auth_principals = parse_principals_env(os.getenv("KB_AUTH_PRINCIPALS", ""))
    consult_ttl_sec = int(os.getenv("KB_CONSULT_TTL_SEC", "300"))
    ledger_path = os.getenv("KB_LEDGER_PATH", "/state/ledger.json")
    maintenance_ledger_path = os.getenv(
        "KB_MAINTENANCE_LEDGER_PATH", "tooling/maintenance-ledger.md"
    )
    maintenance_recently_expired_days = int(
        os.getenv("KB_MAINTENANCE_RECENTLY_EXPIRED_DAYS", "30")
    )
    maintenance_expiring_soon_days = int(
        os.getenv("KB_MAINTENANCE_EXPIRING_SOON_DAYS", "30")
    )
    # KB_STATUS_AUTOFILL defaults to on (issue #147 / KNA-69): a legacy corpus
    # missing `status` keeps its in-force docs after upgrade. Any explicit
    # non-truthy value (off/0/false/no) restores the conservative behavior.
    status_autofill = _env_bool(os.getenv("KB_STATUS_AUTOFILL", "on"))
    session_idle_timeout_sec = int(os.getenv("KB_SESSION_IDLE_TIMEOUT_SEC", "300"))
    session_reap_interval_sec = int(os.getenv("KB_SESSION_REAP_INTERVAL_SEC", "60"))
    session_touch_interval_sec = int(os.getenv("KB_SESSION_TOUCH_INTERVAL_SEC", "30"))
    status_weights = _load_status_weights(os.getenv("KB_STATUS_WEIGHTS", ""))
    read_only = _env_bool(os.getenv("KB_READ_ONLY", ""))
    tool_discovery_mode = load_tool_discovery_mode(
        os.getenv("KB_TOOL_DISCOVERY_MODE", "search")
    )
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
    from data_olympus.embeddings import (
        embeddings_config as _embeddings_config,
    )
    from data_olympus.embeddings import (
        embeddings_enabled as _embeddings_enabled,
    )
    emb_enabled = _embeddings_enabled()
    emb_cfg = _embeddings_config()
    trusted_proxies = _split_csv(os.getenv("KB_TRUSTED_PROXIES", ""))
    public_hostnames = _split_csv(os.getenv("KB_PUBLIC_HOSTNAMES", ""))
    disable_version_check = _env_bool(os.getenv("KB_DISABLE_VERSION_CHECK", ""))
    version_check_interval_sec = int(
        os.getenv("KB_VERSION_CHECK_INTERVAL_SEC", "86400")
    )
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
        gate_check_rate_limit_per_hour=gate_check_rate_limit_per_hour,
        max_text_bytes=max_text_bytes,
        max_postimage_bytes=max_postimage_bytes,
        max_body_bytes=max_body_bytes,
        max_bootstrap_files=max_bootstrap_files,
        pending_timeout_sec=pending_timeout_sec,
        pending_queue_cap=pending_queue_cap,
        auto_commit_lock_ttl_sec=auto_commit_lock_ttl_sec,
        worktree_idle_sec=worktree_idle_sec,
        git_key_path=git_key_path,
        audit_log_path=audit_log_path,
        audit_hmac_key=audit_hmac_key,
        audit_max_bytes=audit_max_bytes,
        auth_token=auth_token,
        auth_principals=auth_principals,
        consult_ttl_sec=consult_ttl_sec,
        ledger_path=ledger_path,
        maintenance_ledger_path=maintenance_ledger_path,
        maintenance_recently_expired_days=maintenance_recently_expired_days,
        maintenance_expiring_soon_days=maintenance_expiring_soon_days,
        status_autofill=status_autofill,
        session_idle_timeout_sec=session_idle_timeout_sec,
        session_reap_interval_sec=session_reap_interval_sec,
        session_touch_interval_sec=session_touch_interval_sec,
        status_weights=status_weights,
        read_only=read_only,
        tool_discovery_mode=tool_discovery_mode,
        cooccurrence_enabled=cooc_enabled,
        cooccurrence_k=int(cooc_params["k"]),
        cooccurrence_min_count=int(cooc_params["min_count"]),
        cooccurrence_min_pmi=float(cooc_params["min_pmi"]),
        cooccurrence_min_docs=int(cooc_params["min_docs"]),
        cooccurrence_max_doc_tokens=int(cooc_params["max_doc_tokens"]),
        trigram_fallback_enabled=trigram_enabled,
        trigram_fallback_threshold=trigram_threshold,
        embeddings_enabled=emb_enabled,
        embeddings_weight=emb_cfg.weight,
        embeddings_model=emb_cfg.model_name,
        trusted_proxies=trusted_proxies,
        public_hostnames=public_hostnames,
        disable_version_check=disable_version_check,
        version_check_interval_sec=version_check_interval_sec,
    )
