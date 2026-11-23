"""Build the Agora services for a running server."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from data_olympus.agora.access import Access
from data_olympus.agora.context import AgoraContext, AgoraSettings
from data_olympus.agora.decisions import DecisionService
from data_olympus.agora.highlights import HighlightService
from data_olympus.agora.service import AgoraService
from data_olympus.agora.sqlite_store import SQLiteAgoraStore
from data_olympus.agora.threads import ThreadService
from data_olympus.audit_log import AuditLog

if TYPE_CHECKING:
    from data_olympus.config import Config
    from data_olympus.index import Index
    from data_olympus.principals import PrincipalRegistry


@dataclass(frozen=True, slots=True)
class Agora:
    ctx: AgoraContext
    access: Access
    core: AgoraService
    threads: ThreadService
    decisions: DecisionService
    highlights: HighlightService


def build_agora(config: Config, *, idx: Index, registry: PrincipalRegistry) -> Agora:
    """Create the store (its folder and database on first start; nothing else
    on the state volume is touched) and wire every service together."""
    store = SQLiteAgoraStore(config.agora_state_path)
    store.initialize()
    audit = AuditLog(log_path=config.agora_audit_log_path,
                     hmac_key=config.audit_hmac_key)
    ctx = AgoraContext(
        store=store, audit=audit,
        settings=AgoraSettings.from_config(config, auth_on=registry.auth_configured),
        kb_categories=idx.list_categories,
        principal_lookup=registry.get,
        principal_list=registry.principals,
    )
    access = Access(ctx)
    highlights = HighlightService(ctx, access)
    core = AgoraService(ctx, access, highlights=highlights)
    decisions = DecisionService(ctx, access, highlights=highlights)
    threads = ThreadService(ctx, access, core, decisions=decisions, highlights=highlights)
    return Agora(ctx, access, core, threads, decisions, highlights)
