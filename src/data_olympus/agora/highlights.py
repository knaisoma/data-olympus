"""Agora headlines and the catch-up board (design 6.11).

A headline is recorded when a thread changes state, never for a plain reply.
Its text is written by the participant who caused the event: the thread title
for an opening event, a required one-line outcome for a closing one. Only
fallbacks (accept and withdraw without an outcome, a timeout) and membership
lines are built by the server, from titles, names and states. The server never
generates a summary.

"Since my last catch-up" is a position in a store-wide sequence, not a time.
Every query, count, topic tally and pin checks that the reader can see the
headline's thread before any limit applies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import INSTANCE, Highlight, Message, new_id
from data_olympus.agora.safety import validate_text

if TYPE_CHECKING:
    from collections.abc import Sequence

    from data_olympus.agora.access import Access
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.decisions import DecisionService
    from data_olympus.agora.models import Membership, Participant, Thread
    from data_olympus.agora.threads import InboxItem, ThreadService

HEADLINE_MAX_CHARS = 200
MAX_SNAPSHOT = 500
ACTION_WEIGHTS: dict[str, int] = {
    "decided": 100,
    "unresolved": 90,
    "decision_opened": 80,
    "pinned": 70,
    "handover_accepted": 60,
    "handover_returned": 60,
    "handover_offered": 50,
    "answered": 40,
    "closed": 30,
    "withdrawn": 20,
    "membership": 10,
}
# Grouping by action (design 6.11: decided, handed over, answered, escalated,
# membership); the remaining actions keep their own name.
ACTION_GROUPS: dict[str, str] = {
    "decided": "decided",
    "decision_opened": "escalated",
    "unresolved": "escalated",
    "handover_offered": "handed over",
    "handover_accepted": "handed over",
    "handover_returned": "handed over",
    "answered": "answered",
    "closed": "closed",
    "withdrawn": "withdrawn",
    "pinned": "pinned",
    "membership": "membership",
}
TIMEOUT_SUFFIX = ": no decision before the timeout"
MEMBERSHIP_VERBS = {"joined": "joined", "blocked": "blocked from", "removed": "removed from"}


def fit_headline(title: str, suffix: str) -> str:
    """``title`` + ``suffix`` in at most 200 characters, keeping the suffix
    whole and shortening the title with ``...``."""
    room = HEADLINE_MAX_CHARS - len(suffix)
    if len(title) <= room:
        return title + suffix
    keep = max(0, room - 3)
    return title[:keep] + "..." + suffix


@dataclass(frozen=True, slots=True)
class HighlightGroup:
    key: str
    items: list[Highlight]


@dataclass(frozen=True, slots=True)
class CatchUp:
    after_seq: int
    last_seq: int
    complete: bool
    ack: int | None
    total: int
    needs_you: list[InboxItem]
    groups: list[HighlightGroup]
    topics: dict[str, int] = field(default_factory=dict)
    # True when the inbox scan behind needs_you reached its bound, so the
    # oldest outstanding items may be missing from needs_you.
    needs_you_truncated: bool = False


class HighlightService:
    def __init__(self, ctx: AgoraContext, access: Access) -> None:
        self.ctx = ctx
        self.access = access

    # --- building ----------------------------------------------------------
    @staticmethod
    def fallback(title: str, suffix: str) -> str:
        return fit_headline(title, suffix)

    def _headline(self, text: str) -> str:
        value = validate_text(text, field="headline", max_bytes=HEADLINE_MAX_CHARS * 4,
                              required=True)
        if len(value) > HEADLINE_MAX_CHARS:
            raise AgoraError("too_large", "a headline is at most 200 characters")
        return value

    def make(self, actor: Participant | None, thread: Thread, action: str, headline: str,
             *, message_id: str | None) -> Highlight:
        if action not in ACTION_WEIGHTS:
            raise AgoraError("invalid", f"unknown headline action {action!r}")
        return Highlight(
            id=new_id("hl"), seq=0, scope=thread.scope, thread_id=thread.id,
            message_id=message_id, action=action, headline=self._headline(headline),
            actor=actor.name if actor else "system", topics=tuple(thread.topics),
            weight=ACTION_WEIGHTS[action], pinned_by=None, created_at=self.ctx.clock(),
        )

    def make_membership(self, actor: Participant, m: Membership,
                        verb: Literal["joined", "blocked", "removed"]) -> Highlight:
        line = f"{m.participant} {MEMBERSHIP_VERBS[verb]} {m.project}"
        return Highlight(
            id=new_id("hl"), seq=0, scope=m.project, thread_id=None, message_id=None,
            action="membership", headline=self._headline(line), actor=actor.name,
            topics=(), weight=ACTION_WEIGHTS["membership"], pinned_by=None,
            created_at=self.ctx.clock(),
        )

    # --- committing ---------------------------------------------------------
    def _event_message(self, actor: Participant | None, thread_id: str,
                       body: str) -> Message:
        return Message(new_id("msg"), thread_id, actor.name if actor else "system",
                       actor.via if actor else "", body, (), self.ctx.clock())

    def commit_new_thread(self, actor: Participant, thread: Thread,
                          first: Message | None) -> Thread:
        """Create a thread; a decision or hand-over also gets its opening
        headline (the title), anchored on the first message or, without a
        body, on an event message carrying the title."""
        action = {"decision": "decision_opened", "handover": "handover_offered"}.get(
            thread.kind)
        if action is None:
            self.ctx.store.create_thread(thread, first)
            return thread
        anchor = first or self._event_message(actor, thread.id, thread.title)
        highlight = self.make(actor, thread, action, thread.title, message_id=anchor.id)
        self.ctx.store.create_thread(thread, anchor, highlight=highlight)
        return thread

    def commit_thread_event(
        self, actor: Participant | None, before: Thread, after: Thread, *,
        message: Message | None = None, action: str | None = None,
        headline: str | None = None,
    ) -> Thread:
        """Store a thread change as a compare-and-swap on ``before.version``,
        with its event message and headline, in one transaction. A headline
        anchors on the event message, which carries the headline text."""
        highlight = None
        if action is not None:
            line = headline
            if line is None and action == "handover_accepted":
                line = fit_headline(before.title, f": accepted by "
                                    f"{actor.name if actor else 'system'}")
            if line is None:
                raise AgoraError("invalid", "this change needs a one-line outcome")
            if message is None or message.body != line:
                message = self._event_message(actor, before.id, line)
            highlight = self.make(actor, after, action, line, message_id=message.id)
        result = self.ctx.store.commit_event(thread=after, expected_version=before.version,
                                             message=message, highlight=highlight)
        if not result.ok or result.thread is None:
            raise AgoraError("conflict", "the thread changed meanwhile; read it again "
                             "and retry")
        return result.thread

    def commit_membership(self, actor: Participant, m: Membership,
                          verb: Literal["joined", "blocked", "removed"], *,
                          expected: Membership | None) -> bool:
        highlight = None if m.project == INSTANCE else self.make_membership(actor, m, verb)
        return self.ctx.store.commit_event(membership=m, highlight=highlight,
                                           expected_membership=expected).ok

    def expire(self, thread_id: str) -> Thread | None:
        """Expire an overdue decision through the store's transactional
        read-check-write; racing callers produce one headline."""
        now = self.ctx.clock()

        def build(fresh: Thread) -> tuple[Message, Highlight | None]:
            line = fit_headline(fresh.title, TIMEOUT_SUFFIX)
            msg = self._event_message(None, fresh.id, line)
            return msg, self.make(None, fresh, "unresolved", line, message_id=msg.id)

        expired = self.ctx.store.expire_if_due(thread_id, now, build)
        if expired is not None:
            self.ctx.record("decision.timeout", None, thread=thread_id)
        return expired

    # --- visibility -----------------------------------------------------------
    def visible(self, actor: Participant, h: Highlight) -> bool:
        if h.thread_id is not None:
            thread = self.ctx.store.get_thread(h.thread_id)
            return thread is not None and self.access.can_read_thread(actor, thread)
        try:
            scope = self.access.scope_record(h.scope)
        except AgoraError:
            return False
        return self.access.can_read_scope(actor, scope)

    # --- pins -------------------------------------------------------------
    def pin(self, actor: Participant, *, message_id: str, headline: str) -> Highlight:
        self.access.require(actor.kind == "person", "only persons pin headlines")
        line = self._headline(headline)
        message = self.ctx.store.get_message(message_id) if isinstance(message_id, str) \
            else None
        if message is None:
            raise AgoraError("not_found", "no such message")
        thread = self.ctx.store.get_thread(message.thread_id)
        if thread is None:
            raise AgoraError("not_found", "no such message")
        self.access.require(self.access.can_read_thread(actor, thread),
                            "you cannot read this thread")
        h = Highlight(new_id("hl"), 0, thread.scope, thread.id, message.id, "pinned", line,
                      actor.name, tuple(thread.topics), ACTION_WEIGHTS["pinned"],
                      actor.name, self.ctx.clock())
        self.ctx.store.commit_event(highlight=h)
        stored = self.ctx.store.get_highlight(h.id)
        self.ctx.record("highlight.pin", actor, highlight=h.id, thread=thread.id)
        return stored or h

    def unpin(self, actor: Participant, highlight_id: str) -> None:
        self.access.require(actor.kind == "person", "only persons unpin headlines")
        h = self.ctx.store.get_highlight(highlight_id) if isinstance(highlight_id, str) \
            else None
        if h is None or not self.visible(actor, h):
            raise AgoraError("not_found", "no such headline")
        if h.pinned_by is None:
            raise AgoraError("conflict", "the headline is not pinned")
        from data_olympus.agora.access import project_of

        project = project_of(h.scope) if h.scope != INSTANCE else None
        allowed = (h.pinned_by == actor.name or actor.admin
                   or (project is not None and self.access.is_project_owner(actor, project)))
        self.access.require(allowed, "only whoever pinned it, a project owner or an "
                            "admin may unpin it")
        from dataclasses import replace

        self.ctx.store.update_highlight(replace(h, pinned_by=None))
        self.ctx.record("highlight.unpin", actor, highlight=h.id)

    # --- catch-up ---------------------------------------------------------
    def _readable_scopes(self, actor: Participant, scope: str | None) -> set[str]:
        if scope is not None:
            record = self.access.resolve_scope(actor, scope, claim=False)
            self.access.require(self.access.can_read_scope(actor, record),
                                "you cannot read this scope")
            return {record.id}
        out = {"universal"}
        for s in self.ctx.store.list_scopes():
            if self.access.can_read_scope(actor, s):
                out.add(s.id)
        return out

    def _query(self, actor: Participant, *, after_seq: int, bound: int,
               scope: str | None = None, topics: Sequence[str] = (),
               actions: Sequence[str] = (), pinned_only: bool = False,
               since_ts: float | None = None) -> dict[str, Any]:
        """The store filter for what ``actor`` may see: readable scopes and the
        direct-conversation rule, applied in SQL before any limit or count."""
        return {
            "after_seq": after_seq, "upto_seq": bound,
            "scopes": self._readable_scopes(actor, scope),
            "actions": set(actions) or None, "topics": set(topics) or None,
            "pinned_only": pinned_only, "since_ts": since_ts,
            "direct_for": self.access.direct_for(actor),
        }

    @staticmethod
    def _order_key(h: Highlight) -> tuple[int, int, int]:
        return (0 if h.pinned_by else 1, -h.weight, -h.seq)

    def _group(self, items: list[Highlight], group_by: str) -> list[HighlightGroup]:
        buckets: dict[str, list[Highlight]] = {}
        for h in items:
            if group_by == "topic":
                keys = list(h.topics) or ["untagged"]
            elif group_by == "action":
                keys = [ACTION_GROUPS.get(h.action, h.action)]
            else:
                keys = [h.scope]
            for key in keys:
                buckets.setdefault(key, []).append(h)
        groups = [HighlightGroup(k, sorted(v, key=self._order_key))
                  for k, v in buckets.items()]
        groups.sort(key=lambda g: (self._order_key(g.items[0]), g.key))
        return groups

    def catch_up(
        self, actor: Participant, *, threads: ThreadService, decisions: DecisionService,
        after_seq: int | None = None, since_ts: float | None = None,
        scope: str | None = None, topics: Sequence[str] = (),
        actions: Sequence[str] = (), pinned_only: bool = False,
        group_by: Literal["topic", "action", "scope"] = "scope", limit: int = 200,
    ) -> CatchUp:
        if group_by not in ("topic", "action", "scope"):
            raise AgoraError("invalid", "group_by must be topic, action or scope")
        unknown = [a for a in actions if a not in ACTION_WEIGHTS]
        if unknown:
            raise AgoraError("invalid", f"unknown actions: {', '.join(unknown[:5])}")
        if after_seq is not None and (not isinstance(after_seq, int) or after_seq < 0):
            raise AgoraError("invalid", "after_seq must be a non-negative integer")
        decisions.expire_due()
        bound = self.ctx.store.max_seq()
        marker = self.ctx.store.get_catchup(actor.name)
        start = marker if after_seq is None else after_seq
        query = self._query(actor, after_seq=start, bound=bound, scope=scope,
                            topics=topics, actions=actions, pinned_only=pinned_only,
                            since_ts=since_ts)
        cap = max(1, min(int(limit), MAX_SNAPSHOT))
        total = self.ctx.store.count_highlights(**query)
        # Only the page is read; each row is checked again as a last guard.
        page = self.ctx.store.list_highlights(**query, limit=cap)
        taken = [h for h in page if self.visible(actor, h)]
        complete = total <= cap
        last_seq = bound if complete or not page else page[-1].seq
        filtered = bool(scope or topics or actions or pinned_only or since_ts is not None)
        from_marker = after_seq is None or after_seq == marker
        ack = last_seq if (not filtered and from_marker) else None
        tally: dict[str, int] = {}
        for h in taken:
            for t in h.topics:
                tally[t] = tally.get(t, 0) + 1
        inbox, inbox_truncated = threads.inbox_page(actor)
        needs_you = [i for i in inbox if i.reason != "activity"]
        return CatchUp(start, last_seq, complete, ack, total, needs_you,
                       self._group(taken, group_by), tally,
                       needs_you_truncated=inbox_truncated)

    def mark_caught_up(self, actor: Participant, ack: int) -> int:
        """Move the caller's own marker forward to ``ack``, clamped to the
        latest position; it never moves back. Only the caller's marker moves,
        so a made-up value can only hide headlines from the caller."""
        if not isinstance(ack, int) or isinstance(ack, bool) or ack < 0:
            raise AgoraError("invalid", "ack must be a non-negative integer")
        store = self.ctx.store
        target = min(ack, store.max_seq())
        store.set_catchup(actor.name, target)
        marker = store.get_catchup(actor.name)
        self.ctx.record("catchup.mark", actor, seq=marker)
        return marker

    def count_since(self, actor: Participant, decisions: DecisionService) -> int:
        decisions.expire_due()
        bound = self.ctx.store.max_seq()
        marker = self.ctx.store.get_catchup(actor.name)
        return self.ctx.store.count_highlights(
            **self._query(actor, after_seq=marker, bound=bound))
