"""Turn Agora records into MCP responses.

Every text field another participant wrote (titles, bodies, options,
recommendations and vote rationales, hand-over fields, profile text, contact
notes, headlines and search snippets) is wrapped by ``quote_untrusted`` with a
per-response nonce. Names, ids, states, timestamps, tags and ticket refs are
plain values.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from data_olympus.agora.safety import UNTRUSTED_NOTICE, quote_untrusted

if TYPE_CHECKING:
    from data_olympus.agora.decisions import Tally
    from data_olympus.agora.highlights import CatchUp
    from data_olympus.agora.models import (
        Contact,
        Highlight,
        Membership,
        Message,
        ParticipantRecord,
        Scope,
        Subscription,
        Thread,
        Vote,
    )
    from data_olympus.agora.threads import InboxItem, SearchResult, ThreadView


def envelope(**fields: Any) -> dict[str, Any]:
    return {"ok": True, "untrusted_notice": UNTRUSTED_NOTICE, **fields}


def error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": code, "message": message}


def _q(text: str | None, *, author: str, scope: str, nonce: str) -> str:
    if not text:
        return ""
    return quote_untrusted(text, author=author, scope=scope, nonce=nonce)


def _audience(thread: Thread) -> str:
    if thread.audience_type == "everyone":
        return "everyone"
    return f"{thread.audience_type}:{thread.audience_value}"


def _payload(thread: Thread, nonce: str) -> dict[str, Any]:
    p = thread.payload
    scope = thread.scope
    if thread.kind == "handover":
        out: dict[str, Any] = {
            k: _q(p.get(k), author=thread.originator, scope=scope, nonce=nonce)
            for k in ("summary", "done", "left", "verify", "repo", "branch")}
        out["accepted_by"] = p.get("accepted_by")
        out["declined_by"] = list(p.get("declined_by") or [])
        return out
    if thread.kind == "decision":
        options = [{
            "id": o["id"],
            "title": _q(o.get("title"), author=o["proposed_by"], scope=scope, nonce=nonce),
            "description": _q(o.get("description"), author=o["proposed_by"],
                              scope=scope, nonce=nonce),
            "pros": _q(o.get("pros"), author=o["proposed_by"], scope=scope, nonce=nonce),
            "cons": _q(o.get("cons"), author=o["proposed_by"], scope=scope, nonce=nonce),
            "proposed_by": o["proposed_by"],
        } for o in p.get("options") or []]
        rec = p.get("recommendation")
        decided = p.get("decided")
        return {
            "question": _q(p.get("question"), author=thread.originator, scope=scope,
                           nonce=nonce),
            "context": _q(p.get("context"), author=thread.originator, scope=scope,
                          nonce=nonce),
            "options": options,
            "recommendation": None if not rec else {
                "option_id": rec["option_id"], "proposed_by": rec["proposed_by"],
                "rationale": _q(rec.get("rationale"), author=rec["proposed_by"],
                                scope=scope, nonce=nonce),
            },
            "voting": bool(p.get("voting")),
            "agent_votes": bool(p.get("agent_votes")),
            "close_policy": p.get("close_policy", "originator"),
            "decided": None if not decided else {
                "option_id": decided["option_id"], "by": decided["by"],
                "at": decided["at"],
                "rationale": _q(decided.get("rationale"), author=decided["by"],
                                scope=scope, nonce=nonce),
            },
        }
    return {}


def render_thread(thread: Thread, nonce: str) -> dict[str, Any]:
    return {
        "id": thread.id, "scope": thread.scope, "audience": _audience(thread),
        "kind": thread.kind, "state": thread.state, "originator": thread.originator,
        "title": _q(thread.title, author=thread.originator, scope=thread.scope,
                    nonce=nonce),
        "topics": list(thread.topics),
        "tickets": [{"system": t.system, "key": t.key, "url": t.url, "title": t.title}
                    for t in thread.tickets],
        "payload": _payload(thread, nonce),
        "created_at": thread.created_at, "updated_at": thread.updated_at,
        "timeout_at": thread.timeout_at, "version": thread.version,
    }


def render_message(m: Message, scope: str, nonce: str) -> dict[str, Any]:
    return {"id": m.id, "author": m.author, "via": m.via, "created_at": m.created_at,
            "refs": list(m.refs), "body": _q(m.body, author=m.author, scope=scope,
                                              nonce=nonce)}


def render_vote(v: Vote, scope: str, nonce: str) -> dict[str, Any]:
    return {"voter": v.voter, "voter_kind": v.voter_kind, "option_id": v.option_id,
            "rationale": _q(v.rationale, author=v.voter, scope=scope, nonce=nonce),
            "confidence": v.confidence, "status": v.status,
            "reviewed_by": v.reviewed_by, "updated_at": v.updated_at}


def render_tally(tally: Tally) -> dict[str, Any]:
    return {"people": dict(tally.people), "agents": dict(tally.agents),
            "agent_votes": list(tally.agent_votes), "tentative": list(tally.tentative)}


def render_thread_view(view: ThreadView, tally: Tally | None, nonce: str) -> dict[str, Any]:
    scope = view.thread.scope
    out: dict[str, Any] = {
        "thread": render_thread(view.thread, nonce),
        "messages": [render_message(m, scope, nonce) for m in view.messages],
        "votes": [render_vote(v, scope, nonce) for v in view.votes],
    }
    if tally is not None:
        out["tally"] = render_tally(tally)
    return out


def render_inbox(items: list[InboxItem], nonce: str) -> list[dict[str, Any]]:
    return [{
        "reason": i.reason, "thread_id": i.thread_id, "kind": i.kind, "scope": i.scope,
        "title": _q(i.title, author=i.author or "system", scope=i.scope or "-",
                    nonce=nonce),
        "state": i.state, "updated_at": i.updated_at, "unseen": i.unseen,
        "for_participant": i.for_participant, "subject": i.subject,
    } for i in items]


def render_card(card: dict[str, Any], record: ParticipantRecord,
                nonce: str) -> dict[str, Any]:
    extra = dict(card.get("x-agora") or {})
    for key in ("tool", "availability"):
        extra[key] = _q(extra.get(key), author=record.name, scope="-", nonce=nonce)
    return {
        "name": card["name"],
        "description": _q(card.get("description"), author=record.name, scope="-",
                          nonce=nonce),
        "skills": list(card.get("skills") or []),
        "x-agora": extra,
    }


def render_highlight(h: Highlight, nonce: str) -> dict[str, Any]:
    return {"id": h.id, "seq": h.seq, "scope": h.scope, "thread_id": h.thread_id,
            "message_id": h.message_id, "action": h.action, "actor": h.actor,
            "headline": _q(h.headline, author=h.actor, scope=h.scope, nonce=nonce),
            "topics": list(h.topics), "weight": h.weight, "pinned_by": h.pinned_by,
            "created_at": h.created_at}


def render_catch_up(c: CatchUp, nonce: str) -> dict[str, Any]:
    return {
        "after_seq": c.after_seq, "last_seq": c.last_seq, "complete": c.complete,
        "ack": c.ack, "total": c.total, "topics": dict(c.topics),
        "needs_you": render_inbox(c.needs_you, nonce),
        "groups": [{"key": g.key, "items": [render_highlight(h, nonce) for h in g.items]}
                   for g in c.groups],
    }


def render_search(results: list[SearchResult], nonce: str) -> list[dict[str, Any]]:
    return [{"thread_id": r.thread_id, "scope": r.scope, "kind": r.kind,
             "state": r.state,
             "title": _q(r.title, author="-", scope=r.scope, nonce=nonce),
             "snippet": _q(r.snippet, author="-", scope=r.scope, nonce=nonce)}
            for r in results]


def render_scope(s: Scope) -> dict[str, Any]:
    return {"id": s.id, "tier": s.tier, "owner": s.owner, "created_by": s.created_by,
            "remote_url": s.remote_url, "has_kb_docs": s.has_kb_docs,
            "created_at": s.created_at}


def render_membership(m: Membership) -> dict[str, Any]:
    return {"project": m.project, "participant": m.participant, "role": m.role,
            "state": m.state, "invited_by": m.invited_by, "updated_at": m.updated_at}


def render_contact(c: Contact, nonce: str) -> dict[str, Any]:
    return {"recipient": c.recipient, "sender": c.sender, "state": c.state,
            "note": _q(c.note, author=c.sender, scope="-", nonce=nonce),
            "updated_at": c.updated_at}


def render_subscription(s: Subscription) -> dict[str, Any]:
    return {"participant": s.participant, "scope": s.scope, "autonomy": s.autonomy,
            "kinds": list(s.kinds)}
