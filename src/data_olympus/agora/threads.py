"""Agora threads: discussions, questions, hand-overs (and decisions through
DecisionService), the inbox and search.

Every call returns at once; nothing waits on another participant. Visibility
is checked with ``Access.can_read_thread`` before any limit is applied.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from data_olympus.agora.context import body_fields
from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import Message, Thread, TicketRef, new_id
from data_olympus.agora.safety import fts_query, validate_text
from data_olympus.agora.service import (
    NOTE_MAX_CHARS,
    SHORT_TEXT_MAX_CHARS,
    TAG_PATTERN,
    THREAD_KINDS,
    short_text,
)

if TYPE_CHECKING:
    import builtins
    from collections.abc import Iterable, Sequence

    from data_olympus.agora.access import Access
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.models import Participant, Vote
    from data_olympus.agora.service import AgoraService

TITLE_MAX_CHARS = 200
MAX_REFS = 20
REF_MAX_CHARS = 500
MAX_TICKETS = 10
TICKET_SYSTEMS = ("jira", "github", "gitlab", "other")
MAX_MESSAGES = 200
_FETCH = 200
_FETCH_CAP = 5000

INBOX_PRIORITY = (
    "decision_to_decide",
    "tentative_vote_to_review",
    "handover_offered",
    "question_to_you",
    "decision_vote_requested",
    "contact_request",
    "membership_invite",
    "activity",
)
_ACTION_REASONS = frozenset(INBOX_PRIORITY) - {"activity"}


@dataclass(frozen=True, slots=True)
class HandoverInput:
    summary: str
    done: str = ""
    left: str = ""
    verify: str = ""
    repo: str = ""
    branch: str = ""


@dataclass(frozen=True, slots=True)
class OptionInput:
    title: str
    description: str = ""
    pros: str = ""
    cons: str = ""


@dataclass(frozen=True, slots=True)
class RecommendationInput:
    option_index: int
    rationale: str


@dataclass(frozen=True, slots=True)
class DecisionInput:
    question: str
    context: str = ""
    options: Sequence[OptionInput] = ()
    recommendation: RecommendationInput | None = None
    voting: bool = True
    agent_votes: bool | None = None
    timeout_sec: int | None = None


@dataclass(frozen=True, slots=True)
class ThreadView:
    thread: Thread
    messages: list[Message]
    votes: list[Vote] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class InboxItem:
    reason: str
    thread_id: str | None
    kind: str | None
    scope: str | None
    title: str
    state: str | None
    updated_at: float
    unseen: bool
    for_participant: str
    subject: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    thread_id: str
    scope: str
    title: str
    kind: str
    state: str
    snippet: str


def parse_audience(raw: str) -> tuple[Literal["everyone", "capability", "participant"], str]:
    from data_olympus.agora.identity import valid_name

    if raw == "everyone":
        return "everyone", ""
    kind, sep, value = (raw or "").partition(":")
    if sep and kind == "capability" and TAG_PATTERN.fullmatch(value):
        return "capability", value
    if sep and kind == "participant" and valid_name(value):
        return "participant", value
    raise AgoraError("invalid", "audience must be everyone, capability:<tag> or "
                     "participant:<name>")


def _tickets(tickets: Sequence[TicketRef | dict[str, Any]]) -> tuple[TicketRef, ...]:
    if len(tickets) > MAX_TICKETS:
        raise AgoraError("invalid", f"at most {MAX_TICKETS} tickets")
    out = []
    for t in tickets:
        data = (t if isinstance(t, dict) else
                {"system": t.system, "key": t.key, "url": t.url, "title": t.title})
        system = str(data.get("system", ""))
        if system not in TICKET_SYSTEMS:
            raise AgoraError("invalid", f"ticket system must be one of "
                             f"{', '.join(TICKET_SYSTEMS)}")
        out.append(TicketRef(
            system,  # type: ignore[arg-type]
            short_text(str(data.get("key", "")), field="ticket key", max_chars=100,
                       required=True),
            short_text(str(data.get("url", "")), field="ticket url", max_chars=REF_MAX_CHARS),
            short_text(str(data.get("title", "")), field="ticket title",
                       max_chars=SHORT_TEXT_MAX_CHARS),
        ))
    return tuple(out)


def _refs(refs: Sequence[str]) -> tuple[str, ...]:
    if isinstance(refs, str) or len(refs) > MAX_REFS:
        raise AgoraError("invalid", f"refs must be a list of at most {MAX_REFS}")
    return tuple(short_text(str(r), field="ref", max_chars=REF_MAX_CHARS, required=True)
                 for r in refs)


class ThreadService:
    def __init__(self, ctx: AgoraContext, access: Access, core: AgoraService,
                 decisions: Any = None, highlights: Any = None) -> None:
        self.ctx = ctx
        self.access = access
        self.core = core
        self.decisions = decisions
        self.highlights = highlights

    # --- helpers ------------------------------------------------------------
    def _body(self, text: str, *, required: bool, field: str = "body") -> str:
        return validate_text(text, field=field,
                             max_bytes=self.ctx.settings.max_body_bytes, required=required)

    def load(self, actor: Participant, thread_id: str) -> Thread:
        """The thread, after any due timeout, if ``actor`` may read it."""
        thread = self.ctx.store.get_thread(thread_id) if isinstance(thread_id, str) else None
        if thread is None:
            raise AgoraError("not_found", "no such thread")
        if self.decisions is not None:
            thread = self.decisions.apply_timeout(thread)
        self.access.require(self.access.can_read_thread(actor, thread),
                            "you cannot read this thread")
        return thread

    def _message(self, actor: Participant | None, thread_id: str, body: str,
                 refs: tuple[str, ...] = ()) -> Message:
        return Message(new_id("msg"), thread_id, actor.name if actor else "system",
                       actor.via if actor else "", body, refs, self.ctx.clock())

    def _commit(self, actor: Participant | None, before: Thread, after: Thread, *,
                message: Message | None = None, action: str | None = None,
                headline: str | None = None) -> Thread:
        """Store a thread change with its event message (and, from Task 9, its
        headline) as one unit."""
        if self.highlights is not None:
            result: Thread = self.highlights.commit_thread_event(
                actor, before, after, message=message, action=action, headline=headline)
            return result
        if message is not None:
            self.ctx.store.add_message(message)
            after = replace(after, updated_at=message.created_at)
        self.ctx.store.update_thread(after)
        return after

    def _addressed_to(self, thread: Thread, name: str, skills: Iterable[str]) -> bool:
        if thread.audience_type == "participant":
            return thread.audience_value == name
        if thread.audience_type == "capability":
            return thread.audience_value in set(skills)
        return False

    def _skills(self, name: str) -> tuple[str, ...]:
        profile = self.ctx.store.get_profile(name)
        return profile.skills if profile else ()

    def _visible(self, actor: Participant, limit: int, **query: Any) -> builtins.list[Thread]:
        """Threads matching ``query`` that ``actor`` may read, newest first;
        visibility is applied before the limit."""
        fetch = max(_FETCH, limit * 4)
        while True:
            rows = self.ctx.store.list_threads(limit=fetch, **query)
            visible = [t for t in rows if self.access.can_read_thread(actor, t)]
            if len(visible) >= limit or len(rows) < fetch or fetch >= _FETCH_CAP:
                return visible[:limit]
            fetch = min(fetch * 4, _FETCH_CAP)

    # --- posting ----------------------------------------------------------
    def post(
        self, actor: Participant, *, scope: str, audience: str, kind: str, title: str,
        body: str = "", refs: Sequence[str] = (),
        tickets: Sequence[TicketRef | dict[str, Any]] = (),
        decision: DecisionInput | None = None, handover: HandoverInput | None = None,
        topics: Sequence[str] = (),
    ) -> Thread:
        if kind not in THREAD_KINDS:
            raise AgoraError("invalid", f"kind must be one of {', '.join(THREAD_KINDS)}")
        audience_type, audience_value = parse_audience(audience)
        clean_title = short_text(title, field="title", max_chars=TITLE_MAX_CHARS,
                                 required=True)
        text = self._body(body, required=kind in ("discussion", "question"))
        clean_refs = _refs(refs)
        clean_tickets = _tickets(tickets)
        clean_topics = self._topics(topics)
        record = self.access.resolve_scope(actor, scope, claim=True)
        self.access.require(self.access.can_read_scope(actor, record),
                            "you cannot post in this scope")
        if audience_type == "participant":
            if self.ctx.known(audience_value) is None:
                raise AgoraError("not_found", f"no participant {audience_value!r}")
            if not self.core.contact_ok(actor, audience_value):
                raise AgoraError("contact_required",
                                 f"{audience_value} has not accepted you as a contact; "
                                 "send a contact request first")
        now = self.ctx.clock()
        payload: dict[str, Any] = {}
        timeout_at: float | None = None
        state = "open"
        if kind == "handover":
            if handover is None:
                raise AgoraError("invalid", "a hand-over needs its summary")
            if audience_type == "everyone":
                raise AgoraError("invalid", "a hand-over goes to a participant or a "
                                 "capability")
            payload = self._handover_payload(handover)
            state = "offered"
        elif kind == "decision":
            if self.decisions is None or decision is None:
                raise AgoraError("invalid", "a decision request needs its question")
            payload, timeout_at = self.decisions.build_payload(actor, decision, now)
        thread = Thread(
            id=new_id("th"), scope=record.id, audience_type=audience_type,
            audience_value=audience_value, kind=kind,  # type: ignore[arg-type]
            title=clean_title, originator=actor.name, state=state,
            tickets=clean_tickets, payload=payload, created_at=now, updated_at=now,
            timeout_at=timeout_at,
        )
        first = Message(new_id("msg"), thread.id, actor.name, actor.via, text,
                        clean_refs, now) if text else None
        if self.highlights is not None:
            thread = self.highlights.commit_new_thread(actor, thread, first, clean_topics)
        else:
            self.ctx.store.create_thread(thread, first)
        self.ctx.store.mark_seen(actor.name, thread.id, self.ctx.clock())
        self.ctx.record("thread.post", actor, thread=thread.id, scope=record.id,
                        kind=kind, audience=audience, **body_fields(text))
        return thread

    def _topics(self, topics: Sequence[str]) -> tuple[str, ...]:
        from data_olympus.agora.service import validate_tags

        return validate_tags(topics, field="topics", limit=5)

    def _handover_payload(self, h: HandoverInput) -> dict[str, Any]:
        def part(value: str, name: str, required: bool = False) -> str:
            return self._body(value, required=required, field=name)

        return {
            "summary": part(h.summary, "summary", required=True),
            "done": part(h.done, "done"),
            "left": part(h.left, "left"),
            "verify": part(h.verify, "verify"),
            "repo": short_text(h.repo, field="repo", max_chars=REF_MAX_CHARS),
            "branch": short_text(h.branch, field="branch", max_chars=SHORT_TEXT_MAX_CHARS),
            "accepted_by": None,
            "declined_by": [],
        }

    def reply(self, actor: Participant, thread_id: str, body: str,
              refs: Sequence[str] = ()) -> Message:
        text = self._body(body, required=True)
        clean_refs = _refs(refs)
        thread = self.load(actor, thread_id)
        if thread.state in ("closed", "withdrawn"):
            raise AgoraError("conflict", f"the thread is {thread.state}")
        msg = Message(new_id("msg"), thread.id, actor.name, actor.via, text, clean_refs,
                      self.ctx.clock())
        self.ctx.store.add_message(msg)
        self.ctx.store.mark_seen(actor.name, thread.id, self.ctx.clock())
        self.ctx.record("thread.reply", actor, thread=thread.id, message=msg.id,
                        **body_fields(text))
        return msg

    def _can_manage(self, actor: Participant, thread: Thread) -> bool:
        """The originator, the originator's owner, or the project owner."""
        if actor.name == thread.originator:
            return True
        rec = self.ctx.known(thread.originator)
        if rec is not None and rec.owner == actor.name:
            return True
        from data_olympus.agora.access import project_of

        project = project_of(thread.scope)
        if project is not None:
            return self.access.is_project_owner(actor, project)
        return actor.admin

    def _outcome(self, outcome: str, *, required: bool) -> str:
        return short_text(outcome, field="outcome", max_chars=TITLE_MAX_CHARS,
                          required=required)

    def set_state(self, actor: Participant, thread_id: str,
                  state: Literal["answered", "closed"], outcome: str = "") -> Thread:
        if state not in ("answered", "closed"):
            raise AgoraError("invalid", "state must be answered or closed")
        line = self._outcome(outcome, required=self.highlights is not None)
        thread = self.load(actor, thread_id)
        if thread.kind not in ("discussion", "question"):
            raise AgoraError("invalid", "only discussions and questions change state here")
        if thread.kind == "discussion" and state == "answered":
            raise AgoraError("invalid", "a discussion is closed, not answered")
        self.access.require(self._can_manage(actor, thread),
                            "only the originator, its owner or the project owner may "
                            "change the state")
        allowed = {"answered": ("open",), "closed": ("open", "answered")}[state]
        if thread.state not in allowed:
            raise AgoraError("conflict", f"the thread is {thread.state}")
        after = replace(thread, state=state, updated_at=self.ctx.clock())
        msg = self._message(actor, thread.id, line) if line else None
        result = self._commit(actor, thread, after, message=msg, action=state,
                              headline=line)
        self.ctx.record("thread.state", actor, thread=thread.id, state=state)
        return result

    def handover_respond(
        self, actor: Participant, thread_id: str,
        action: Literal["accept", "decline", "return"], note: str = "",
        outcome: str = "",
    ) -> Thread:
        if action not in ("accept", "decline", "return"):
            raise AgoraError("invalid", "action must be accept, decline or return")
        text = self._body(note, required=False, field="note")
        if len(text) > NOTE_MAX_CHARS * 8:
            raise AgoraError("too_large", "note is too long")
        line = self._outcome(outcome, required=(action == "return"
                                                and self.highlights is not None))
        thread = self.load(actor, thread_id)
        if thread.kind != "handover":
            raise AgoraError("invalid", "not a hand-over")
        payload = dict(thread.payload)
        now = self.ctx.clock()
        headline: str | None = None
        event = None
        if action in ("accept", "decline"):
            if thread.state != "offered":
                raise AgoraError("conflict", f"the hand-over is {thread.state}")
            if thread.audience_type == "participant":
                rec = self.ctx.known(thread.audience_value)
                self.access.require(
                    actor.name == thread.audience_value
                    or (rec is not None and rec.owner == actor.name),
                    "this hand-over is addressed to someone else")
            else:
                self.access.require(actor.name != thread.originator,
                                    "you cannot respond to your own hand-over")
                self.access.require(
                    thread.audience_value in self._skills(actor.name),
                    f"your profile does not list the {thread.audience_value!r} skill")
            if action == "accept":
                payload["accepted_by"] = actor.name
                after = replace(thread, state="accepted", payload=payload, updated_at=now)
                event = "handover_accepted"
                headline = line or None
            elif thread.audience_type == "capability":
                declined = list(payload.get("declined_by") or [])
                if actor.name not in declined:
                    declined.append(actor.name)
                payload["declined_by"] = declined
                after = replace(thread, payload=payload, updated_at=now)
            else:
                after = replace(thread, state="declined", payload=payload, updated_at=now)
        else:
            if thread.state != "accepted":
                raise AgoraError("conflict", f"the hand-over is {thread.state}")
            acceptor = str(payload.get("accepted_by") or "")
            rec = self.ctx.known(acceptor) if acceptor else None
            self.access.require(
                actor.name == acceptor or (rec is not None and rec.owner == actor.name),
                "only whoever accepted the hand-over may return it")
            after = replace(thread, state="returned", payload=payload, updated_at=now)
            event = "handover_returned"
            headline = line
        message = self._message(actor, thread.id, text) if text else None
        result = self._commit(actor, thread, after, message=message, action=event,
                              headline=headline)
        if line and self.highlights is None:
            self.ctx.store.add_message(self._message(actor, thread.id, line))
        self.ctx.record(f"handover.{action}", actor, thread=thread.id,
                        **(body_fields(text) if text else {}))
        return result

    # --- reading ------------------------------------------------------------
    def get(self, actor: Participant, thread_id: str, *, around: str | None = None,
            before: int = 10, after: int = 10) -> ThreadView:
        thread = self.load(actor, thread_id)
        store = self.ctx.store
        if around is not None:
            messages = store.list_messages_around(
                thread.id, around, before=max(0, min(before, 100)),
                after=max(0, min(after, 100)))
        else:
            messages = store.list_messages(thread.id, limit=MAX_MESSAGES)
        votes = store.list_votes(thread.id)
        store.mark_seen(actor.name, thread.id, self.ctx.clock())
        return ThreadView(thread, messages, votes)

    def list(self, actor: Participant, *, scope: str, kinds: Sequence[str] | None = None,
             states: Sequence[str] | None = None, limit: int = 50) -> builtins.list[Thread]:
        record = self.access.resolve_scope(actor, scope, claim=False)
        self.access.require(self.access.can_read_scope(actor, record),
                            "you cannot read this scope")
        limit = max(1, min(int(limit), 200))
        return self._visible(actor, limit, scopes={record.id},
                             kinds=set(kinds) if kinds else None,
                             states=set(states) if states else None)

    def search(self, actor: Participant, query: str, *, scope: str | None = None,
               limit: int = 20) -> builtins.list[SearchResult]:
        q = fts_query(query)
        limit = max(1, min(int(limit), 100))
        if scope is not None:
            record = self.access.resolve_scope(actor, scope, claim=False)
            self.access.require(self.access.can_read_scope(actor, record),
                                "you cannot read this scope")
            scopes = {record.id}
        else:
            scopes = {s.id for s in self.core.scopes(actor)}
        out: builtins.list[SearchResult] = []
        for thread_id, snippet in self.ctx.store.search(q, scopes=scopes, limit=limit * 5):
            thread = self.ctx.store.get_thread(thread_id)
            if thread is None or not self.access.can_read_thread(actor, thread):
                continue
            out.append(SearchResult(thread.id, thread.scope, thread.title, thread.kind,
                                    thread.state, snippet))
            if len(out) >= limit:
                break
        return out

    def mark_seen(self, actor: Participant, thread_ids: Sequence[str]) -> int:
        count = 0
        now = self.ctx.clock()
        for tid in list(thread_ids)[:200]:
            thread = self.ctx.store.get_thread(tid) if isinstance(tid, str) else None
            if thread is None or not self.access.can_read_thread(actor, thread):
                continue
            self.ctx.store.mark_seen(actor.name, thread.id, now)
            count += 1
        return count

    # --- inbox --------------------------------------------------------------
    def inbox(self, actor: Participant, *, limit: int = 50) -> builtins.list[InboxItem]:
        limit = max(1, min(int(limit), 200))
        names = [actor.name]
        if actor.kind == "person":
            names += [n for n in self.ctx.agents_of(actor.name) if n != actor.name]
        best: dict[tuple[str, str], InboxItem] = {}

        def offer(item: InboxItem) -> None:
            key = (item.thread_id or f"{item.reason}:{item.subject}", item.for_participant)
            current = best.get(key)
            if current is None or (INBOX_PRIORITY.index(item.reason)
                                   < INBOX_PRIORITY.index(current.reason)):
                best[key] = item

        person = actor.name if actor.kind == "person" else None
        for name in names:
            who = actor if name == actor.name else self.ctx.participant_for(name)
            if who is None:
                continue
            for item in self._inbox_for(actor, who, person):
                offer(item)
        items = sorted(best.values(),
                       key=lambda i: (INBOX_PRIORITY.index(i.reason), -i.updated_at))
        return items[:limit]

    def _inbox_for(self, actor: Participant, who: Participant,
                   person: str | None) -> builtins.list[InboxItem]:
        store = self.ctx.store
        name = who.name
        skills = self._skills(name)
        threads: dict[str, Thread] = {}
        queries: builtins.list[dict[str, Any]] = [
            {"audiences": {("participant", name)}},
            {"originators": {name}},
        ]
        if skills:
            queries.append({"audiences": {("capability", s) for s in skills}})
        subs = {s.scope: s for s in store.list_subscriptions(name)}
        scopes = set(subs) | self.core.implicit_scopes(name)
        if scopes:
            queries.append({"scopes": scopes, "audiences": {("everyone", "")}})
        for q in queries:
            for t in store.list_threads(limit=_FETCH, **q):
                threads.setdefault(t.id, t)
        if self.decisions is not None:
            threads = {tid: self.decisions.apply_timeout(t) for tid, t in threads.items()}
        visible = [t for t in threads.values()
                   if self.access.can_read_thread(who, t)
                   and self.access.can_read_thread(actor, t)]
        seen = store.seen_map(name, [t.id for t in visible])
        if name != actor.name:
            mine = store.seen_map(actor.name, [t.id for t in visible])
            seen = {k: max(seen.get(k, 0.0), mine.get(k, 0.0)) for k in set(seen) | set(mine)}
        out: builtins.list[InboxItem] = []

        def item(t: Thread, reason: str) -> InboxItem:
            return InboxItem(reason, t.id, t.kind, t.scope, t.title, t.state, t.updated_at,
                             t.updated_at > seen.get(t.id, 0.0), name)

        action_ids: set[str] = set()
        for t in visible:
            addressed = self._addressed_to(t, name, skills)
            if (t.kind == "handover" and t.state == "offered" and addressed
                    and t.originator != name
                    and name not in (t.payload.get("declined_by") or [])):
                out.append(item(t, "handover_offered"))
                action_ids.add(t.id)
            elif t.kind == "question" and t.state == "open" and addressed \
                    and t.originator != name:
                out.append(item(t, "question_to_you"))
                action_ids.add(t.id)
        if self.decisions is not None:
            for t, reason, for_participant in self.decisions.inbox_reasons(
                    {name}, person if name == actor.name else None, visible):
                # An agent's vote request does not need its owner; the agent
                # sees it in its own inbox.
                if for_participant == name and not (
                        reason == "decision_vote_requested" and name != actor.name):
                    out.append(item(t, reason))
                    action_ids.add(t.id)
        # A person's inbox carries their agents' action items, not their
        # agents' general activity, which the person already sees as their own.
        for t in visible:
            if t.id in action_ids or name != actor.name:
                continue
            kinds = subs[t.scope].kinds if t.scope in subs else ()
            if kinds and t.kind not in kinds and t.originator != name:
                continue
            if t.updated_at > seen.get(t.id, 0.0):
                out.append(item(t, "activity"))
        for c in store.list_contacts(recipient=name, state="requested"):
            out.append(InboxItem("contact_request", None, None, None,
                                 f"contact request from {c.sender}", c.state,
                                 c.updated_at, True, name, subject=c.sender))
        for m in store.list_memberships(participant=name):
            if m.state == "invited":
                out.append(InboxItem("membership_invite", None, None, m.project,
                                     f"invitation to {m.project}", m.state,
                                     m.updated_at, True, name, subject=m.project))
        return out
