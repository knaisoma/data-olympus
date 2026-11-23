"""Agora decision requests (design 6.4, kind 3).

A decision request is always closed by a person: its originator, or the owner
of the agent that originated it. Votes are advisory. An agent's vote carries a
rationale and a self-asserted confidence; below the threshold, or when the
agent asks for a person, it stays tentative until its owner reviews it. The
confidence is not a control, only a signal. Tallies keep people and agents
apart so a person running several agents does not outweigh other people.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from data_olympus.agora.context import body_fields
from data_olympus.agora.errors import AgoraError
from data_olympus.agora.highlights import TIMEOUT_SUFFIX, fit_headline
from data_olympus.agora.models import Message, Vote, new_id
from data_olympus.agora.safety import validate_text
from data_olympus.agora.service import short_text
from data_olympus.config import (
    AGORA_MAX_DECISION_TIMEOUT_SEC,
    AGORA_MIN_DECISION_TIMEOUT_SEC,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from data_olympus.agora.access import Access
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.models import Participant, Thread
    from data_olympus.agora.threads import DecisionInput, OptionInput

MAX_OPTIONS = 10
OPTION_TITLE_MAX_CHARS = 200
OPEN_STATES = ("open", "unresolved_advisory")
COUNTED = ("cast", "confirmed")


@dataclass(frozen=True, slots=True)
class Tally:
    people: dict[str, int]
    agents: dict[str, int]
    agent_votes: list[dict[str, Any]]
    tentative: list[dict[str, Any]]


class DecisionService:
    def __init__(self, ctx: AgoraContext, access: Access, highlights: Any = None) -> None:
        self.ctx = ctx
        self.access = access
        self.highlights = highlights

    # --- helpers ------------------------------------------------------------
    def _text(self, value: str, field: str, *, required: bool = False) -> str:
        return validate_text(value, field=field,
                             max_bytes=self.ctx.settings.max_body_bytes, required=required)

    def _option(self, actor: Participant, o: OptionInput) -> dict[str, Any]:
        return {
            "id": new_id("opt"),
            "title": short_text(o.title, field="option title",
                                max_chars=OPTION_TITLE_MAX_CHARS, required=True),
            "description": self._text(o.description, "option description"),
            "pros": self._text(o.pros, "option pros"),
            "cons": self._text(o.cons, "option cons"),
            "proposed_by": actor.name,
        }

    def _load(self, actor: Participant, thread_id: str, *,
              kinds: tuple[str, ...] = ("decision",)) -> Thread:
        thread = self.ctx.store.get_thread(thread_id) if isinstance(thread_id, str) else None
        if thread is None:
            raise AgoraError("not_found", "no such thread")
        thread = self.apply_timeout(thread)
        self.access.require(self.access.can_read_thread(actor, thread),
                            "you cannot read this thread")
        if thread.kind not in kinds:
            raise AgoraError("invalid", "not a decision request")
        return thread

    def _option_ids(self, thread: Thread) -> list[str]:
        return [o["id"] for o in thread.payload.get("options") or []]

    def _decider(self, thread: Thread) -> str | None:
        """The person who closes ``thread``: its originator if a person, else
        the originating agent's owner. None for an ownerless agent."""
        rec = self.ctx.known(thread.originator)
        if rec is None:
            return None
        if rec.kind == "person":
            return rec.name
        return rec.owner

    def _is_originator_side(self, actor: Participant, thread: Thread) -> bool:
        if actor.name == thread.originator:
            return True
        rec = self.ctx.known(thread.originator)
        return rec is not None and rec.owner == actor.name

    def _commit(self, actor: Participant | None, before: Thread, after: Thread, *,
                message: Message | None = None, action: str | None = None,
                headline: str | None = None) -> Thread:
        if self.highlights is not None:
            result: Thread = self.highlights.commit_thread_event(
                actor, before, after, message=message, action=action, headline=headline)
            return result
        res = self.ctx.store.commit_event(thread=after, expected_version=before.version,
                                          message=message)
        if not res.ok or res.thread is None:
            raise AgoraError("conflict", "the thread changed meanwhile; read it again "
                             "and retry")
        return res.thread

    def _event_message(self, actor: Participant | None, thread: Thread,
                       body: str) -> Message:
        return Message(new_id("msg"), thread.id, actor.name if actor else "system",
                       actor.via if actor else "", body, (), self.ctx.clock())

    # --- creation -----------------------------------------------------------
    def build_payload(self, actor: Participant, d: DecisionInput,
                      now: float) -> tuple[dict[str, Any], float]:
        if actor.is_autonomous:
            raise AgoraError("forbidden", "an agent without an owner cannot open a "
                             "decision request; a person must close it")
        timeout = d.timeout_sec if d.timeout_sec is not None else \
            self.ctx.settings.decision_timeout_sec
        if (not isinstance(timeout, int) or isinstance(timeout, bool)
                or not AGORA_MIN_DECISION_TIMEOUT_SEC <= timeout
                <= AGORA_MAX_DECISION_TIMEOUT_SEC):
            raise AgoraError("invalid", "timeout_sec must be between "
                             f"{AGORA_MIN_DECISION_TIMEOUT_SEC} and "
                             f"{AGORA_MAX_DECISION_TIMEOUT_SEC}")
        if len(d.options) > MAX_OPTIONS:
            raise AgoraError("invalid", f"at most {MAX_OPTIONS} options")
        options = [self._option(actor, o) for o in d.options]
        recommendation = None
        if d.recommendation is not None:
            idx = d.recommendation.option_index
            if not isinstance(idx, int) or not 0 <= idx < len(options):
                raise AgoraError("invalid", "the recommendation names no option")
            recommendation = {
                "option_id": options[idx]["id"],
                "rationale": self._text(d.recommendation.rationale, "rationale",
                                        required=True),
                "proposed_by": actor.name,
            }
        payload = {
            "question": self._text(d.question, "question", required=True),
            "context": self._text(d.context, "context"),
            "options": options,
            "recommendation": recommendation,
            "voting": bool(d.voting),
            "agent_votes": (bool(d.agent_votes) if d.agent_votes is not None
                            else self.ctx.settings.agent_votes),
            "close_policy": "originator",
            "decided": None,
        }
        return payload, now + timeout

    # --- timeout -----------------------------------------------------------
    def apply_timeout(self, thread: Thread) -> Thread:
        """An open decision past its timeout becomes unresolved_advisory.
        Nothing waits on it; the originator can still decide or withdraw."""
        if (thread.kind != "decision" or thread.state != "open"
                or thread.timeout_at is None or thread.timeout_at > self.ctx.clock()):
            return thread
        expired = self._expire(thread.id)
        if expired is not None:
            return expired
        fresh = self.ctx.store.get_thread(thread.id)
        return fresh if fresh is not None else thread

    def _expire(self, thread_id: str) -> Thread | None:
        """Expire through the store's transactional read-check-write, never a
        compare-and-swap, so it cannot lose to a racing reply and racing
        callers record one timeout."""
        if self.highlights is not None:
            result: Thread | None = self.highlights.expire(thread_id)
            return result

        def build(fresh: Thread) -> tuple[Message, None]:
            line = fit_headline(fresh.title, TIMEOUT_SUFFIX)
            return self._event_message(None, fresh, line), None

        expired = self.ctx.store.expire_if_due(thread_id, self.ctx.clock(), build)
        if expired is not None:
            self.ctx.record("decision.timeout", None, thread=thread_id)
        return expired

    def expire_due(self) -> int:
        """Expire every overdue open decision; run before every catch-up and
        count so a timed-out decision shows without anyone opening it."""
        count = 0
        for t in self.ctx.store.list_due_decisions(self.ctx.clock()):
            if self._expire(t.id) is not None:
                count += 1
        return count

    # --- options and recommendation ----------------------------------------
    def add_option(self, actor: Participant, thread_id: str, option: OptionInput) -> str:
        thread = self._load(actor, thread_id)
        if thread.state != "open":
            raise AgoraError("conflict", f"the decision is {thread.state}")
        self.access.require_direct_write(actor, thread)
        options = list(thread.payload.get("options") or [])
        if len(options) >= MAX_OPTIONS:
            raise AgoraError("invalid", f"at most {MAX_OPTIONS} options")
        new = self._option(actor, option)
        payload = {**thread.payload, "options": [*options, new]}
        self._commit(actor, thread, replace(thread, payload=payload,
                                            updated_at=self.ctx.clock()))
        self.ctx.record("decision.option", actor, thread=thread.id, option=new["id"])
        return str(new["id"])

    def recommend(self, actor: Participant, thread_id: str, option_id: str,
                  rationale: str) -> Thread:
        text = self._text(rationale, "rationale", required=True)
        thread = self._load(actor, thread_id)
        if thread.state != "open":
            raise AgoraError("conflict", f"the decision is {thread.state}")
        self.access.require_direct_write(actor, thread)
        if option_id not in self._option_ids(thread):
            raise AgoraError("invalid", "no such option")
        payload = {**thread.payload, "recommendation": {
            "option_id": option_id, "rationale": text, "proposed_by": actor.name}}
        result = self._commit(actor, thread, replace(thread, payload=payload,
                                                     updated_at=self.ctx.clock()))
        self.ctx.record("decision.recommend", actor, thread=thread.id, option=option_id,
                        **body_fields(text))
        return result

    # --- votes --------------------------------------------------------------
    def vote(self, actor: Participant, thread_id: str, option_id: str, *,
             rationale: str = "", confidence: float | None = None,
             needs_human: bool = False) -> Vote:
        thread = self._load(actor, thread_id)
        if thread.state != "open":
            raise AgoraError("conflict", f"the decision is {thread.state}")
        if not thread.payload.get("voting"):
            raise AgoraError("invalid", "voting is off for this decision")
        self.access.require_direct_write(actor, thread)
        if option_id not in self._option_ids(thread):
            raise AgoraError("invalid", "no such option")
        now = self.ctx.clock()
        if actor.kind == "person":
            text = self._text(rationale, "rationale")
            v = Vote(thread.id, actor.name, "person", option_id, text, None, "cast",
                     None, now)
        else:
            if not thread.payload.get("agent_votes"):
                raise AgoraError("forbidden", "agent votes are off for this decision")
            text = self._text(rationale, "rationale", required=True)
            if (confidence is None or isinstance(confidence, bool)
                    or not isinstance(confidence, int | float)
                    or not 0.0 <= float(confidence) <= 1.0):
                raise AgoraError("invalid", "an agent vote needs a confidence in [0, 1]")
            cast = (float(confidence) >= self.ctx.settings.agent_vote_threshold
                    and not needs_human)
            v = Vote(thread.id, actor.name, "agent", option_id, text, float(confidence),
                     "cast" if cast else "tentative", None, now)
        # Re-checked inside the write: a vote racing a decision, withdrawal or
        # timeout is refused rather than counted after it.
        if not self.ctx.store.put_vote(v, thread_state="open"):
            raise AgoraError("conflict", "the decision is no longer open")
        self.ctx.record("decision.vote", actor, thread=thread.id, option=option_id,
                        status=v.status, confidence=v.confidence, **body_fields(text))
        return v

    def review_vote(self, actor: Participant, thread_id: str, *, agent: str,
                    action: Literal["confirm", "change", "drop"],
                    option_id: str | None = None) -> Vote:
        if action not in ("confirm", "change", "drop"):
            raise AgoraError("invalid", "action must be confirm, change or drop")
        thread = self._load(actor, thread_id)
        rec = self.ctx.known(agent)
        self.access.require(rec is not None and rec.owner == actor.name,
                            "only the agent's owner may review its vote")
        current = {v.voter: v for v in self.ctx.store.list_votes(thread.id)}.get(agent)
        if current is None or current.status != "tentative":
            raise AgoraError("conflict", "there is no tentative vote to review")
        if thread.state != "open":
            raise AgoraError("conflict", f"the decision is {thread.state}")
        now = self.ctx.clock()
        if action == "drop":
            v = replace(current, status="dropped", reviewed_by=actor.name, updated_at=now)
        else:
            chosen = current.option_id
            if action == "change":
                if option_id not in self._option_ids(thread):
                    raise AgoraError("invalid", "no such option")
                chosen = str(option_id)
            v = replace(current, option_id=chosen, status="confirmed",
                        reviewed_by=actor.name, updated_at=now)
        # The vote must still be the tentative one reviewed: an agent's
        # re-vote in between makes this review stale.
        if not self.ctx.store.put_vote(v, thread_state="open", expected=current):
            raise AgoraError("conflict", "the vote changed meanwhile; review it again")
        self.ctx.record("decision.vote_review", actor, thread=thread.id, agent=agent,
                        review=action, option=v.option_id)
        return v

    def tally(self, thread_id: str) -> Tally:
        people: dict[str, int] = {}
        agents: dict[str, int] = {}
        agent_votes: list[dict[str, Any]] = []
        tentative: list[dict[str, Any]] = []
        for v in self.ctx.store.list_votes(thread_id):
            if v.voter_kind == "agent":
                agent_votes.append({"voter": v.voter, "option_id": v.option_id,
                                    "confidence": v.confidence, "status": v.status})
            if v.status in COUNTED:
                bucket = people if v.voter_kind == "person" else agents
                bucket[v.option_id] = bucket.get(v.option_id, 0) + 1
            elif v.status == "tentative":
                rec = self.ctx.known(v.voter)
                tentative.append({"voter": v.voter, "option_id": v.option_id,
                                  "confidence": v.confidence,
                                  "ownerless": rec is None or rec.owner is None})
        return Tally(people, agents, agent_votes, tentative)

    # --- closing ------------------------------------------------------------
    def decide(self, actor: Participant, thread_id: str, option_id: str,
               rationale: str, outcome: str = "") -> Thread:
        text = self._text(rationale, "rationale", required=True)
        line = short_text(outcome, field="outcome", max_chars=200,
                          required=self.highlights is not None)
        thread = self._load(actor, thread_id)
        self.access.require(actor.kind == "person" and actor.name == self._decider(thread),
                            "only the person who opened the decision, or the owner of "
                            "the agent that did, may decide it")
        if thread.state not in OPEN_STATES:
            raise AgoraError("conflict", f"the decision is {thread.state}")
        if option_id not in self._option_ids(thread):
            raise AgoraError("invalid", "no such option")
        now = self.ctx.clock()
        payload = {**thread.payload, "decided": {
            "option_id": option_id, "rationale": text, "by": actor.name, "at": now}}
        after = replace(thread, state="decided", payload=payload, updated_at=now)
        msg = self._event_message(actor, thread, line) if line else None
        result = self._commit(actor, thread, after, message=msg, action="decided",
                              headline=line or None)
        self.ctx.record("decision.decide", actor, thread=thread.id, option=option_id,
                        **body_fields(text))
        return result

    def withdraw(self, actor: Participant, thread_id: str, outcome: str = "") -> Thread:
        line = short_text(outcome, field="outcome", max_chars=200)
        thread = self._load(actor, thread_id, kinds=("decision", "handover"))
        self.access.require(self._is_originator_side(actor, thread),
                            "only the originator or its owner may withdraw")
        allowed = OPEN_STATES if thread.kind == "decision" else ("offered",)
        if thread.state not in allowed:
            raise AgoraError("conflict", f"the thread is {thread.state}")
        after = replace(thread, state="withdrawn", updated_at=self.ctx.clock())
        headline = line or fit_headline(thread.title, ": withdrawn")
        msg = self._event_message(actor, thread, headline) if headline else None
        result = self._commit(actor, thread, after, message=msg, action="withdrawn",
                              headline=headline or None)
        self.ctx.record("decision.withdraw", actor, thread=thread.id, kind=thread.kind)
        return result

    # --- inbox --------------------------------------------------------------
    def inbox_reasons(self, actor_names: set[str], person: str | None,
                      threads: Iterable[Thread]) -> list[tuple[Thread, str, str]]:
        out: list[tuple[Thread, str, str]] = []
        for t in threads:
            if t.kind != "decision":
                continue
            votes = self.ctx.store.list_votes(t.id) if t.state == "open" else []
            if person is not None and self._decider(t) == person:
                counted = any(v.status in COUNTED for v in votes)
                if t.state == "unresolved_advisory" or (
                        t.state == "open" and (counted or t.payload.get("recommendation"))):
                    out.append((t, "decision_to_decide", person))
            if person is not None:
                for v in votes:
                    if v.status != "tentative":
                        continue
                    rec = self.ctx.known(v.voter)
                    if rec is not None and rec.owner == person:
                        out.append((t, "tentative_vote_to_review", person))
                        break
            if t.state == "open" and t.payload.get("voting"):
                voted = {v.voter for v in votes}
                for name in actor_names:
                    rec = self.ctx.known(name)
                    if name in voted or name == t.originator or rec is None:
                        continue
                    if rec.kind == "agent" and not t.payload.get("agent_votes"):
                        continue
                    out.append((t, "decision_vote_requested", name))
        return out
