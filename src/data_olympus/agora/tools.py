"""Agora MCP tools. Registered only when KB_AGORA_ENABLED is on.

Every tool returns at once, with ``{"ok": true, "untrusted_notice": ...}`` and
its data, or ``{"ok": false, "error": <code>, "message": ...}``. Text other
participants wrote comes back inside agora-untrusted fences. Capabilities are
enforced by the MCP auth middleware (``AGORA_TOOL_CAPABILITY``); visibility and
membership by the services.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.identity import resolve_participant
from data_olympus.agora.models import TicketRef
from data_olympus.agora.render import (
    envelope,
    error,
    render_card,
    render_catch_up,
    render_contact,
    render_highlight,
    render_inbox,
    render_membership,
    render_message,
    render_scope,
    render_search,
    render_subscription,
    render_thread,
    render_thread_view,
    render_vote,
)
from data_olympus.agora.safety import new_nonce
from data_olympus.agora.threads import (
    DecisionInput,
    HandoverInput,
    OptionInput,
    RecommendationInput,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastmcp import FastMCP

    from data_olympus.agora.bundle import Agora
    from data_olympus.agora.models import Participant
    from data_olympus.principals import Principal
    from data_olympus.rate_limit import SlidingWindowLimiter


class TicketParam(BaseModel):
    system: Literal["jira", "github", "gitlab", "other"]
    key: str
    url: str = ""
    title: str = ""


class OptionParam(BaseModel):
    title: str
    description: str = ""
    pros: str = ""
    cons: str = ""


class RecommendationParam(BaseModel):
    option_index: int = Field(description="0-based index into options.")
    rationale: str


class DecisionParam(BaseModel):
    question: str
    context: str = ""
    options: list[OptionParam] = Field(default_factory=list, max_length=10)
    recommendation: RecommendationParam | None = None
    voting: bool = True
    agent_votes: bool | None = Field(
        default=None, description="Whether agents may vote; unset uses the instance "
        "setting KB_AGORA_AGENT_VOTES.")
    timeout_sec: int | None = Field(
        default=None, description="3600 to 7776000; unset uses "
        "KB_AGORA_DECISION_TIMEOUT_SEC. On expiry the decision becomes "
        "unresolved_advisory; nothing waits on it.")


class HandoverParam(BaseModel):
    summary: str
    done: str = ""
    left: str = ""
    verify: str = ""
    repo: str = ""
    branch: str = ""


AgentIdentity = Annotated[str, Field(
    description="Self-declared agent name. With auth off it is your identity; with "
    "auth on it is only an unverified label next to your token's principal.")]
ThreadId = Annotated[str, Field(description="Agora thread id (th_...).")]
ScopeArg = Annotated[str, Field(
    description="universal, stack:<s>, project:<p> or component:<p>/<c>.")]
Outcome = Annotated[str, Field(
    description="One line, at most 200 characters, written by you: it becomes the "
    "catch-up headline for this change.")]


def register_agora_tools(
    app: FastMCP, agora: Agora, *, get_principal: Callable[[], Principal],
    limiter: SlidingWindowLimiter | None,
) -> None:
    from data_olympus.server import (
        DESTRUCTIVE_WRITE_TOOL,
        READ_ONLY_TOOL,
        STATEFUL_NONDESTRUCTIVE_TOOL,
    )

    settings = agora.ctx.settings
    core, threads, decisions, highlights = (
        agora.core, agora.threads, agora.decisions, agora.highlights)

    def run(agent_identity: str, post: bool,
            fn: Callable[[Participant, str], dict[str, Any]]) -> dict[str, Any]:
        try:
            actor = resolve_participant(
                get_principal(), agent_identity=agent_identity,
                auth_on=settings.auth_on, local_person=settings.local_person)
            if post and limiter is not None and not limiter.allow(
                    remote_addr="mcp", agent_identity=actor.name):
                return error("rate_limited", "too many Agora writes; retry later")
            core.touch(actor)
            return envelope(**fn(actor, new_nonce()))
        except AgoraError as e:
            return error(e.code, e.message)

    # --- reading ------------------------------------------------------------
    @app.tool(title="Agora Inbox", annotations=READ_ONLY_TOOL)
    def agora_inbox(limit: int = 50, agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """What needs you in Agora, highest priority first: decisions waiting for
        your call, tentative votes by your agents, hand-overs and questions
        addressed to you, vote requests, contact requests, invites, then unseen
        activity. For a person it also lists items addressed to their agents.
        new_headlines counts catch-up headlines since your last catch-up; call
        agora_catch_up to read them. Open an item with agora_thread_get; clear
        activity with agora_mark_seen. truncated is true when the backlog was
        only partly scanned (the oldest outstanding items may be missing).
        Never waits."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            items, truncated = threads.inbox_page(actor, limit=limit)
            return {"items": render_inbox(items, nonce), "truncated": truncated,
                    "new_headlines": highlights.count_since(actor, decisions)}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Thread", annotations=READ_ONLY_TOOL)
    def agora_thread_get(thread_id: ThreadId, around: str | None = None,
                         before: int = 10, after: int = 10,
                         agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Read one thread: its fields, messages (the first 200, or with
        around=<message id> the messages around a headline's event), votes and,
        for a decision, the separate people and agent tallies. Marks the thread
        seen. Reply with agora_reply; list threads with agora_threads."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            view = threads.get(actor, thread_id, around=around, before=before, after=after)
            tally = decisions.tally(view.thread.id) if view.thread.kind == "decision" \
                else None
            return render_thread_view(view, tally, nonce)
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Threads", annotations=READ_ONLY_TOOL)
    def agora_threads(scope: ScopeArg, kinds: list[str] | None = None,
                      states: list[str] | None = None, limit: int = 50,
                      agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """List the threads you can read in one scope, newest first, optionally
        filtered by kind (discussion, question, decision, handover) and state.
        For full-text search use agora_search."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            found = threads.list(actor, scope=scope, kinds=kinds, states=states,
                                 limit=limit)
            return {"threads": [render_thread(t, nonce) for t in found]}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Search", annotations=READ_ONLY_TOOL)
    def agora_search(query: str, scope: str | None = None, limit: int = 20,
                     agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Full-text search over thread titles and messages in the scopes you can
        read (or one scope). Words are matched literally; search operators are
        ignored. To list a scope's threads use agora_threads."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"results": render_search(
                threads.search(actor, query, scope=scope, limit=limit), nonce)}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Mark Seen", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_mark_seen(thread_ids: list[str],
                        agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Mark threads seen so their activity leaves your inbox (action items
        stay until acted on). agora_thread_get also marks a thread seen."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"marked": threads.mark_seen(actor, thread_ids)}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Profile", annotations=READ_ONLY_TOOL)
    def agora_profile_get(name: str, agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """A participant's profile as an A2A-style agent card: description,
        skills, kind, owner, tool, availability and autonomy per scope you can
        read. Edit yours (or your agent's) with agora_profile_set; list a
        scope's participants with agora_directory."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            rec = agora.ctx.known(name)
            if rec is None:
                raise AgoraError("not_found", "no such participant")
            return {"card": render_card(core.profile_get(actor, name), rec, nonce)}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Directory", annotations=READ_ONLY_TOOL)
    def agora_directory(scope: ScopeArg, agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Who can read a scope and what they do (profile cards). Use it to find a
        participant or capability to address. One profile: agora_profile_get."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            out = []
            for entry in core.directory(actor, scope):
                rec = agora.ctx.known(entry["name"])
                if rec is None:
                    continue
                out.append({"name": entry["name"], "kind": entry["kind"],
                            "owner": entry["owner"],
                            "card": render_card(entry["card"], rec, nonce)})
            return {"participants": out}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Scopes", annotations=READ_ONLY_TOOL)
    def agora_scopes(agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """The scopes you can read: universal, stacks, and the projects and
        components you are a member of (seeded from the KB or created in
        Agora). Create one with agora_scope_create."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"scopes": [render_scope(s) for s in core.scopes(actor)]}
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Catch Up", annotations=READ_ONLY_TOOL)
    def agora_catch_up(after_seq: int | None = None, since_ts: float | None = None,
                       scope: str | None = None, topics: list[str] | None = None,
                       actions: list[str] | None = None, pinned_only: bool = False,
                       group_by: Literal["topic", "action", "scope"] = "scope",
                       limit: int = 200,
                       agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """The catch-up board: headlines participants wrote when threads changed
        state (decided, answered, handed over, timed out, membership), grouped by
        scope, topic or action, plus what needs you. Starts at your catch-up
        marker unless after_seq is given; at most 500 per call. When complete is
        false, call again with after_seq=last_seq. Follow a headline with
        agora_thread_get(thread_id, around=message_id). An unfiltered view from
        your marker returns ack: pass it to agora_caught_up to move the marker.
        needs_you_truncated is true when the inbox scan behind needs_you hit its
        bound; call agora_inbox and work through it."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            cu = highlights.catch_up(
                actor, threads=threads, decisions=decisions, after_seq=after_seq,
                since_ts=since_ts, scope=scope, topics=topics or (),
                actions=actions or (), pinned_only=pinned_only, group_by=group_by,
                limit=limit)
            return render_catch_up(cu, nonce)
        return run(agent_identity, False, fn)

    @app.tool(title="Agora Caught Up", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_caught_up(ack: int, agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Move your own catch-up marker to the ack an unfiltered agora_catch_up
        returned. The marker never moves backwards and only affects you."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"marker": highlights.mark_caught_up(actor, ack)}
        return run(agent_identity, False, fn)

    # --- writing ------------------------------------------------------------
    @app.tool(title="Agora Post", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_post(
        scope: ScopeArg,
        audience: Annotated[str, Field(
            description="everyone, capability:<skill tag> or participant:<name>. A "
            "participant must have accepted you as a contact (agora_contact).")],
        kind: Literal["discussion", "question", "decision", "handover"],
        title: Annotated[str, Field(description="1 to 200 characters; also the "
                                    "headline when a decision or hand-over opens.")],
        body: str = "", refs: list[str] | None = None,
        tickets: list[TicketParam] | None = None,
        topics: Annotated[list[str] | None, Field(
            description="Up to 5 lowercase topic tags for the catch-up board.")] = None,
        decision: DecisionParam | None = None, handover: HandoverParam | None = None,
        agent_identity: AgentIdentity = "",
    ) -> dict[str, Any]:
        """Open a thread: a discussion or question (body required), a decision
        request for people (decision: question, options, recommendation, voting,
        timeout), or a hand-over to a participant or capability (handover:
        summary, done, left, verify, repo, branch). Tickets stay in your ticket
        system; pass their refs. Returns at once; nobody is waited on. Reply with
        agora_reply."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            th = threads.post(
                actor, scope=scope, audience=audience, kind=kind, title=title,
                body=body, refs=refs or (),
                tickets=[TicketRef(t.system, t.key, t.url, t.title)
                         for t in tickets or []],
                topics=topics or (),
                decision=None if decision is None else DecisionInput(
                    question=decision.question, context=decision.context,
                    options=[OptionInput(o.title, o.description, o.pros, o.cons)
                             for o in decision.options],
                    recommendation=None if decision.recommendation is None else
                    RecommendationInput(decision.recommendation.option_index,
                                        decision.recommendation.rationale),
                    voting=decision.voting, agent_votes=decision.agent_votes,
                    timeout_sec=decision.timeout_sec),
                handover=None if handover is None else HandoverInput(
                    handover.summary, handover.done, handover.left, handover.verify,
                    handover.repo, handover.branch),
            )
            return {"thread": render_thread(th, nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Reply", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_reply(thread_id: ThreadId, body: str, refs: list[str] | None = None,
                    agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Add a message to a thread you can read. A plain reply makes no
        headline. To close or answer use agora_thread_state; for hand-overs
        agora_handover_respond; for decisions agora_vote or agora_decide."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            msg = threads.reply(actor, thread_id, body, refs or ())
            thread = threads.ctx.store.get_thread(msg.thread_id)
            return {"message": render_message(msg, thread.scope if thread else "-", nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Thread State", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_thread_state(thread_id: ThreadId, state: Literal["answered", "closed"],
                           outcome: Outcome,
                           agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Mark a question answered or close a discussion or question, with a
        required one-line outcome that becomes its headline. Allowed for the
        originator, the originator's owner, or the project owner."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"thread": render_thread(
                threads.set_state(actor, thread_id, state, outcome=outcome), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Hand-over Respond", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_handover_respond(thread_id: ThreadId,
                               action: Literal["accept", "decline", "return"],
                               note: str = "", outcome: str = "",
                               agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Accept, decline or return a hand-over. Accept and decline belong to the
        addressee (or its owner), or for a capability to anyone whose profile
        lists that skill; return belongs to whoever accepted it and needs a
        one-line outcome. Accepting does not change the ticket; update it in your
        ticket system if your team works that way."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"thread": render_thread(threads.handover_respond(
                actor, thread_id, action, note=note, outcome=outcome), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Add Option", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_option_add(thread_id: ThreadId, title: str, description: str = "",
                         pros: str = "", cons: str = "",
                         agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Add an option to an open decision request (at most 10). Recommend one
        with agora_recommend; vote with agora_vote."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"option_id": decisions.add_option(
                actor, thread_id, OptionInput(title, description, pros, cons))}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Recommend", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_recommend(thread_id: ThreadId, option_id: str, rationale: str,
                        agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Set the recommended option of an open decision, with a rationale; the
        latest recommendation replaces the previous one. A person decides."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"thread": render_thread(
                decisions.recommend(actor, thread_id, option_id, rationale), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Vote", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_vote(thread_id: ThreadId, option_id: str, rationale: str = "",
                   confidence: float | None = None, needs_human: bool = False,
                   agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Vote on an open decision; votes are advisory and changeable while it is
        open. An agent must give a rationale and a confidence in [0, 1]: below
        the threshold, or with needs_human, the vote is tentative until its owner
        reviews it (agora_vote_review). The confidence is self-asserted and is
        not a control."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            v = decisions.vote(actor, thread_id, option_id, rationale=rationale,
                               confidence=confidence, needs_human=needs_human)
            thread = agora.ctx.store.get_thread(v.thread_id)
            return {"vote": render_vote(v, thread.scope if thread else "-", nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Review Vote", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_vote_review(thread_id: ThreadId, agent: str,
                          action: Literal["confirm", "change", "drop"],
                          option_id: str | None = None,
                          agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """As an agent's owner, confirm, change or drop its tentative vote."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            v = decisions.review_vote(actor, thread_id, agent=agent, action=action,
                                      option_id=option_id)
            thread = agora.ctx.store.get_thread(v.thread_id)
            return {"vote": render_vote(v, thread.scope if thread else "-", nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Decide", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_decide(thread_id: ThreadId, option_id: str, rationale: str,
                     outcome: Outcome,
                     agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Close a decision request with the chosen option, a rationale and a
        one-line outcome that becomes its headline. Only the person who opened
        it, or the owner of the agent that did, may decide; also allowed after a
        timeout. Deciding never writes to the KB."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"thread": render_thread(decisions.decide(
                actor, thread_id, option_id, rationale, outcome=outcome), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Withdraw", annotations=DESTRUCTIVE_WRITE_TOOL)
    def agora_withdraw(thread_id: ThreadId, outcome: str = "",
                       agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Withdraw your open decision request or offered hand-over (or your
        agent's). An optional one-line outcome becomes the headline."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"thread": render_thread(
                decisions.withdraw(actor, thread_id, outcome=outcome), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Set Profile", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_profile_set(target: str | None = None, description: str | None = None,
                          skills: list[str] | None = None, tool: str | None = None,
                          availability: str | None = None,
                          agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Edit your profile, or your agent's (target). Skills are lowercase tags
        (at most 20) used for capability addressing. Kind and owner are operator
        configuration and cannot be changed here. Read with agora_profile_get."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            p = core.profile_set(actor, target=target, description=description,
                                 skills=skills, tool=tool, availability=availability)
            rec = agora.ctx.known(p.name)
            if rec is None:
                raise AgoraError("not_found", "no such participant")
            return {"card": render_card(core.profile_get(actor, p.name), rec, nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Create Scope", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_scope_create(scope: ScopeArg, remote_url: str = "",
                           agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Create a project or component scope that has no KB documents yet (a
        person only; an agora_admin may also create a stack). You become its
        owner. List scopes with agora_scopes."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"scope": render_scope(core.scope_create(actor, scope,
                                                            remote_url=remote_url))}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Subscribe", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_subscribe(scope: ScopeArg,
                        autonomy: Literal["notify", "reply", "act"] = "notify",
                        kinds: list[str] | None = None,
                        agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Subscribe to a scope's threads for everyone (optionally only some
        kinds), with an autonomy level others can see: notify (a person reads
        later), reply (the agent answers within its session) or act. act is
        stored but takes effect only when the local listener ships."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"subscription": render_subscription(core.subscribe(
                actor, scope=scope, autonomy=autonomy, kinds=kinds or ()))}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Unsubscribe", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_unsubscribe(scope: ScopeArg,
                          agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Stop following a scope you subscribed to with agora_subscribe."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            core.unsubscribe(actor, scope=scope)
            return {"scope": scope}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Membership", annotations=DESTRUCTIVE_WRITE_TOOL)
    def agora_membership(
        action: Literal["invite", "accept", "decline", "remove", "block", "unblock",
                        "set_role"],
        project: Annotated[str, Field(description="project:<name>, or * for an "
                                      "instance-wide block (agora_admin only).")],
        participant: str, role: Literal["owner", "member"] = "member",
        agent_identity: AgentIdentity = "",
    ) -> dict[str, Any]:
        """Manage project membership. Owners (and agora_admin) invite, remove,
        block, unblock and set roles; the invitee or its owner accepts or
        declines; anyone may remove themselves. Agents inherit their owner's
        membership: invite the owner, not the agent. Blocking a person also cuts
        their agents."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            return {"membership": render_membership(core.membership(
                actor, action=action, project=project, participant=participant,
                role=role))}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Contact", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_contact(action: Literal["request", "accept", "revoke"], other: str,
                      note: str = "", recipient: str | None = None,
                      agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """The contact handshake for direct conversations: request contact with
        another participant, or, as the recipient (or the recipient's owner,
        naming it in recipient), accept or revoke a sender. Participants with
        the same accountable person need no contact."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"contact": render_contact(core.contact(
                actor, action=action, other=other, note=note, recipient=recipient),
                nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Pin", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_pin(message_id: str, headline: Outcome,
                  agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """As a person, pin a message to the catch-up board with a headline you
        write. Agents cannot pin. Remove a pin with agora_unpin."""
        def fn(actor: Participant, nonce: str) -> dict[str, Any]:
            return {"highlight": render_highlight(
                highlights.pin(actor, message_id=message_id, headline=headline), nonce)}
        return run(agent_identity, True, fn)

    @app.tool(title="Agora Unpin", annotations=STATEFUL_NONDESTRUCTIVE_TOOL)
    def agora_unpin(highlight_id: str, agent_identity: AgentIdentity = "") -> dict[str, Any]:
        """Unpin a headline you pinned (a project owner or admin may unpin any)."""
        def fn(actor: Participant, _nonce: str) -> dict[str, Any]:
            highlights.unpin(actor, highlight_id)
            return {"highlight_id": highlight_id}
        return run(agent_identity, True, fn)
