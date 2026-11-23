# ruff: noqa: F811 - pytest fixtures imported from tests.agora_world
"""Agora threads, hand-overs, inbox and search (issue #492, Task 7)."""
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.models import TicketRef
from data_olympus.agora.threads import HandoverInput
from tests.agora_world import (  # noqa: F401 - pytest fixtures
    access,
    admin,
    ana,
    ana_claude,
    ben,
    ben_claude,
    carol,
    core,
    ctx,
    ds,
    hs,
    rogue,
    svc,
    ts,
)

if TYPE_CHECKING:
    from data_olympus.agora.models import Participant
    from data_olympus.agora.service import AgoraService
    from data_olympus.agora.threads import ThreadService


def _alpha(core: AgoraService, owner: Participant, *members: Participant) -> None:
    core.access.resolve_scope(owner, "project:alpha", claim=True)
    for m in members:
        core.membership(owner, action="invite", project="project:alpha",
                        participant=m.name)
        core.membership(m, action="accept", project="project:alpha", participant=m.name)


def test_post_and_reply_roundtrip(ts: ThreadService, ana: Participant,
                                  ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="  Kafka or NATS?  ", body="Thoughts?", refs=["STD-U-001"],
                 tickets=[TicketRef("jira", "PROJ-1", "https://j/PROJ-1", "Pick a bus")])
    assert th.state == "open" and th.title == "Kafka or NATS?"
    assert th.tickets[0].key == "PROJ-1"
    msg = ts.reply(ben, th.id, "NATS, it is simpler.")
    assert msg.author == "ben"
    view = ts.get(ben, th.id)
    assert [m.body for m in view.messages] == ["Thoughts?", "NATS, it is simpler."]
    assert view.messages[0].refs == ("STD-U-001",)


def test_post_validation(ts: ThreadService, ana: Participant) -> None:
    cases = [
        dict(kind="discussion", title="", body="x"),
        dict(kind="discussion", title="x" * 201, body="x"),
        dict(kind="discussion", title="T", body=""),
        dict(kind="question", title="T", body="  "),
        dict(kind="poll", title="T", body="x"),
    ]
    for case in cases:
        with pytest.raises(AgoraError) as e:
            ts.post(ana, scope="universal", audience="everyone", **case)  # type: ignore[arg-type]
        assert e.value.code in ("invalid", "too_large")
    for audience in ("all", "capability:Bad Tag", "participant:", "team:x"):
        with pytest.raises(AgoraError) as e2:
            ts.post(ana, scope="universal", audience=audience, kind="discussion",
                    title="T", body="x")
        assert e2.value.code == "invalid"
    with pytest.raises(AgoraError) as e3:
        ts.post(ana, scope="universal", audience="participant:nobody",
                kind="question", title="T", body="x")
    assert e3.value.code == "not_found"
    with pytest.raises(AgoraError) as e4:
        ts.post(ana, scope="universal", audience="everyone", kind="decision", title="D")
    assert e4.value.code == "invalid"  # a decision needs its DecisionInput


def test_reply_refused_when_closed(ts: ThreadService, ana: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="T", body="x")
    ts.set_state(ana, th.id, "closed", outcome="Settled on NATS")
    with pytest.raises(AgoraError) as e:
        ts.reply(ana, th.id, "late")
    assert e.value.code == "conflict"


def test_question_answered_only_by_originator_side(ts: ThreadService, ana: Participant,
                                                   ana_claude: Participant,
                                                   ben: Participant) -> None:
    th = ts.post(ana_claude, scope="universal", audience="everyone", kind="question",
                 title="Which port?", body="?")
    with pytest.raises(AgoraError) as e:
        ts.set_state(ben, th.id, "answered", outcome="8080")
    assert e.value.code == "forbidden"
    assert ts.set_state(ana, th.id, "answered", outcome="8080").state == "answered"
    assert ts.set_state(ana_claude, th.id, "closed", outcome="Done").state == "closed"
    with pytest.raises(AgoraError) as e2:
        ts.set_state(ana, th.id, "answered", outcome="again")
    assert e2.value.code == "conflict"


def test_discussion_cannot_be_answered(ts: ThreadService, ana: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="T", body="x")
    with pytest.raises(AgoraError) as e:
        ts.set_state(ana, th.id, "answered", outcome="x")
    assert e.value.code == "invalid"


def test_project_owner_may_close(ts: ThreadService, core: AgoraService, ana: Participant,
                                 ben: Participant) -> None:
    _alpha(core, ana, ben)
    th = ts.post(ben, scope="project:alpha", audience="everyone", kind="discussion",
                 title="T", body="x")
    assert ts.set_state(ana, th.id, "closed", outcome="Off topic").state == "closed"


def test_direct_post_needs_contact(ts: ThreadService, core: AgoraService,
                                   ana: Participant, ben: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        ts.post(ben, scope="universal", audience="participant:ana", kind="question",
                title="Q", body="hi")
    assert e.value.code == "contact_required"
    core.contact(ana, action="accept", other="ben")
    th = ts.post(ben, scope="universal", audience="participant:ana", kind="question",
                 title="Q", body="hi")
    assert th.audience_type == "participant" and th.audience_value == "ana"


def test_own_agent_needs_no_contact(ts: ThreadService, ana: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="participant:ana-claude",
                 kind="question", title="Q", body="status?")
    assert th.audience_value == "ana-claude"


def test_direct_thread_hidden_from_others(ts: ThreadService, core: AgoraService,
                                          ana: Participant, ben: Participant,
                                          carol: Participant) -> None:
    core.contact(ana, action="accept", other="ben")
    th = ts.post(ben, scope="universal", audience="participant:ana", kind="question",
                 title="Private", body="hi")
    with pytest.raises(AgoraError) as e:
        ts.get(carol, th.id)
    assert e.value.code == "forbidden"
    with pytest.raises(AgoraError):
        ts.reply(carol, th.id, "me too")
    assert th.id not in [t.id for t in ts.list(carol, scope="universal")]
    assert th.id in [t.id for t in ts.list(ana, scope="universal")]


def test_get_unknown_thread(ts: ThreadService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        ts.get(ana, "th_nope")
    assert e.value.code == "not_found"


def test_handover_capability_accept_by_skilled_agent(ts: ThreadService,
                                                     core: AgoraService,
                                                     ana: Participant,
                                                     ben_claude: Participant) -> None:
    core.profile_set(ben_claude, skills=["devops"])
    th = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="ship it", left="smoke test"))
    assert th.state == "offered" and th.payload["summary"] == "ship it"
    assert ts.handover_respond(ben_claude, th.id, "accept").payload["accepted_by"] == "ben-claude"


def test_handover_needs_input_and_addressee(ts: ThreadService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                title="Deploy")
    assert e.value.code == "invalid"
    with pytest.raises(AgoraError) as e2:
        ts.post(ana, scope="universal", audience="everyone", kind="handover",
                title="Deploy", handover=HandoverInput(summary="x"))
    assert e2.value.code == "invalid"


def test_handover_unskilled_cannot_accept(ts: ThreadService, ana: Participant,
                                          ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    with pytest.raises(AgoraError) as e:
        ts.handover_respond(ben, th.id, "accept")
    assert e.value.code == "forbidden"


def test_handover_decline_on_capability_keeps_offered(ts: ThreadService,
                                                      core: AgoraService,
                                                      ana: Participant, ben: Participant,
                                                      ben_claude: Participant) -> None:
    core.profile_set(ben, skills=["devops"])
    core.profile_set(ben_claude, skills=["devops"])
    th = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    after = ts.handover_respond(ben, th.id, "decline", note="busy this week")
    assert after.state == "offered" and after.payload["declined_by"] == ["ben"]
    assert ts.handover_respond(ben_claude, th.id, "accept").state == "accepted"
    with pytest.raises(AgoraError) as e:
        ts.handover_respond(ben, th.id, "accept")
    assert e.value.code == "conflict"
    bodies = [m.body for m in ts.get(ana, th.id).messages]
    assert bodies[-2:] == ["busy this week", "Deploy: accepted by ben-claude"]


def test_handover_direct_decline_and_owner_accepts(ts: ThreadService, ana: Participant,
                                                   ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="participant:ana-claude",
                 kind="handover", title="Refactor", handover=HandoverInput(summary="x"))
    assert ts.handover_respond(ana, th.id, "accept").state == "accepted"
    with pytest.raises(AgoraError) as e:
        ts.handover_respond(ben, th.id, "return", outcome="nope")
    assert e.value.code == "forbidden"


def test_handover_return_by_acceptor(ts: ThreadService, core: AgoraService,
                                     ana: Participant, ben: Participant,
                                     ben_claude: Participant) -> None:
    core.profile_set(ben_claude, skills=["devops"])
    th = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    ts.handover_respond(ben_claude, th.id, "accept")
    back = ts.handover_respond(ben, th.id, "return", note="blocked on creds",
                               outcome="Returned: missing credentials")
    assert back.state == "returned"


def test_inbox_priority_and_seen(ts: ThreadService, core: AgoraService,
                                 ana: Participant, ben: Participant) -> None:
    core.subscribe(ben, scope="universal")
    core.profile_set(ben, skills=["devops"])
    chat = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                   title="Chat", body="x")
    ho = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    items = ts.inbox(ben)
    reasons = [(i.reason, i.thread_id) for i in items]
    assert reasons[0] == ("handover_offered", ho.id)
    assert ("activity", chat.id) in reasons
    ts.get(ben, chat.id)
    assert ("activity", chat.id) not in [(i.reason, i.thread_id) for i in ts.inbox(ben)]
    ts.get(ben, ho.id)
    assert ("handover_offered", ho.id) in [(i.reason, i.thread_id) for i in ts.inbox(ben)]


def test_own_post_is_not_activity(ts: ThreadService, ana: Participant,
                                  ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="Mine", body="x")
    assert th.id not in [i.thread_id for i in ts.inbox(ana)]
    ts.reply(ben, th.id, "reply")
    assert [(i.reason, i.thread_id) for i in ts.inbox(ana)] == [("activity", th.id)]


def test_inbox_question_contact_and_invite(ts: ThreadService, core: AgoraService,
                                           ana: Participant, ben: Participant,
                                           carol: Participant) -> None:
    core.contact(ben, action="request", other="ana", note="hello")
    core.access.resolve_scope(ben, "project:alpha", claim=True)
    core.membership(ben, action="invite", project="project:alpha", participant="ana")
    core.contact(ana, action="accept", other="ben")
    q = ts.post(ben, scope="universal", audience="participant:ana", kind="question",
                title="Q", body="?")
    items = ts.inbox(ana)
    assert [i.reason for i in items] == ["question_to_you", "membership_invite"]
    assert items[0].thread_id == q.id and items[1].subject == "project:alpha"
    core.contact(carol, action="request", other="ana")
    assert "contact_request" in [i.reason for i in ts.inbox(ana)]


def test_inbox_includes_agents_items_for_person(ts: ThreadService, ana: Participant,
                                                ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="participant:ana-claude",
                 kind="question", title="Status?", body="?")
    th2 = ts.post(ben, scope="universal", audience="everyone", kind="question",
                  title="Anyone?", body="?")
    items = ts.inbox(ana)
    assert any(i.thread_id == th.id and i.for_participant == "ana-claude" for i in items)
    assert th2.id not in [i.thread_id for i in items if i.reason == "question_to_you"]


def test_inbox_drops_project_after_owner_block(ts: ThreadService, core: AgoraService,
                                               ana: Participant, ben: Participant,
                                               ana_claude: Participant) -> None:
    _alpha(core, ben, ana)
    core.contact(ana, action="accept", other="ben", recipient="ana-claude")
    th = ts.post(ben, scope="project:alpha", audience="participant:ana-claude",
                 kind="question", title="Can you check CI?", body="?")
    assert th.id in [i.thread_id for i in ts.inbox(ana_claude)]
    core.membership(ben, action="block", project="project:alpha", participant="ana")
    assert th.id not in [i.thread_id for i in ts.inbox(ana_claude)]
    assert th.id not in [i.thread_id for i in ts.inbox(ana)]
    with pytest.raises(AgoraError) as e:
        ts.get(ana_claude, th.id)
    assert e.value.code == "forbidden"


def test_search_only_readable_scopes(ts: ThreadService, core: AgoraService,
                                     ana: Participant, rogue: Participant) -> None:
    core.scope_create(ana, "project:secret")
    ts.post(ana, scope="project:secret", audience="everyone", kind="discussion",
            title="Ledger design", body="postgres ledger schema")
    ts.post(ana, scope="universal", audience="everyone", kind="discussion",
            title="Public", body="postgres upgrade")
    assert [r.title for r in ts.search(rogue, "postgres")] == ["Public"]
    assert sorted(r.title for r in ts.search(ana, "postgres")) == ["Ledger design", "Public"]
    with pytest.raises(AgoraError) as e:
        ts.search(rogue, "postgres", scope="project:secret")
    assert e.value.code == "forbidden"


def test_mark_seen_counts_readable_only(ts: ThreadService, core: AgoraService,
                                        ana: Participant, ben: Participant) -> None:
    core.scope_create(ana, "project:secret")
    a = ts.post(ana, scope="project:secret", audience="everyone", kind="discussion",
                title="S", body="x")
    b = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                title="U", body="x")
    assert ts.mark_seen(ben, [a.id, b.id, "th_missing"]) == 1


def test_body_too_large_and_secret_refused(ts: ThreadService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                title="T", body="x" * 40000)
    assert e.value.code == "too_large"
    with pytest.raises(AgoraError) as e2:
        ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                title="T", body="use ghp_" + "b" * 36)
    assert e2.value.code == "secret_detected"
    assert ts.ctx.store.list_threads() == []


def test_post_is_audited_with_hash_not_body(ts: ThreadService, ana: Participant) -> None:
    ts.post(ana, scope="universal", audience="everyone", kind="discussion",
            title="T", body="a very distinctive body")
    with open(ts.ctx.audit._path, encoding="utf-8") as f:
        text = f.read()
    assert "a very distinctive body" not in text
    assert '"agora.thread.post"' in text and "body_sha256" in text
