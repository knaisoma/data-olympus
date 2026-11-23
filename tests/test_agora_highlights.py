# ruff: noqa: F811 - pytest fixtures imported from tests.agora_world
"""Agora headlines and the catch-up board (issue #492, Task 9)."""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import pytest

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.highlights import ACTION_WEIGHTS, MAX_SNAPSHOT, fit_headline
from data_olympus.agora.models import Message
from data_olympus.agora.threads import DecisionInput, HandoverInput, OptionInput
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
    from data_olympus.agora.context import AgoraContext
    from data_olympus.agora.decisions import DecisionService
    from data_olympus.agora.highlights import CatchUp, HighlightService
    from data_olympus.agora.models import Highlight, Participant, Thread
    from data_olympus.agora.service import AgoraService
    from data_olympus.agora.threads import ThreadService


def _all(cu: CatchUp) -> list[Highlight]:
    seen: dict[str, Highlight] = {}
    for g in cu.groups:
        for h in g.items:
            seen.setdefault(h.id, h)
    return sorted(seen.values(), key=lambda h: h.seq)


def _alpha(core: AgoraService, owner: Participant, *members: Participant) -> None:
    core.access.resolve_scope(owner, "project:alpha", claim=True)
    for m in members:
        core.membership(owner, action="invite", project="project:alpha",
                        participant=m.name)
        core.membership(m, action="accept", project="project:alpha", participant=m.name)


def _decision(ts: ThreadService, who: Participant, *, scope: str = "universal",
              title: str = "Ledger store", timeout: int | None = None,
              audience: str = "everyone",
              topics: tuple[str, ...] = ()) -> tuple[Thread, str]:
    th = ts.post(who, scope=scope, audience=audience, kind="decision", title=title,
                 topics=topics,
                 decision=DecisionInput(question="Which DB?",
                                        options=[OptionInput("Postgres"),
                                                 OptionInput("SQLite")],
                                        timeout_sec=timeout))
    return th, th.payload["options"][0]["id"]


def test_decide_requires_outcome_and_emits_headline(ds: DecisionService,
                                                    ts: ThreadService,
                                                    hs: HighlightService,
                                                    ana: Participant,
                                                    ben: Participant) -> None:
    th, opt = _decision(ts, ana)
    with pytest.raises(AgoraError, match="invalid"):
        ds.decide(ana, th.id, opt, "why", outcome="")
    ds.decide(ana, th.id, opt, "why", outcome="Use Postgres for the ledger")
    cu = hs.catch_up(ben, threads=ts, decisions=ds)
    [h] = [h for h in _all(cu) if h.action == "decided"]
    assert h.headline == "Use Postgres for the ledger" and h.weight == 100
    assert ts.get(ben, th.id, around=h.message_id).messages[-1].body == (
        "Use Postgres for the ledger")
    opened = [h for h in _all(cu) if h.action == "decision_opened"]
    assert opened and opened[0].headline == "Ledger store"


def test_closing_needs_outcome(ts: ThreadService, ana: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="question",
                 title="Q", body="?")
    with pytest.raises(AgoraError) as e:
        ts.set_state(ana, th.id, "answered")
    assert e.value.code == "invalid"
    with pytest.raises(AgoraError) as e2:
        ts.set_state(ana, th.id, "answered", outcome="x" * 201)
    assert e2.value.code == "too_large"


def test_return_needs_outcome(ts: ThreadService, ana: Participant,
                              ana_claude: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="participant:ana-claude",
                 kind="handover", title="H", handover=HandoverInput(summary="x"))
    ts.handover_respond(ana_claude, th.id, "accept")
    with pytest.raises(AgoraError) as e:
        ts.handover_respond(ana_claude, th.id, "return")
    assert e.value.code == "invalid"


def test_direct_thread_headline_hidden_from_other_members(
    hs: HighlightService, ts: ThreadService, ds: DecisionService, core: AgoraService,
    ana: Participant, ben: Participant, carol: Participant, admin: Participant,
) -> None:
    _alpha(core, ana, ben, carol)
    core.contact(ben, action="accept", other="ana")
    carol_before = hs.count_since(carol, ds)  # membership headlines she may see
    th, opt = _decision(ts, ana, scope="project:alpha", audience="participant:ben",
                        topics=("secret-topic",))
    ds.decide(ana, th.id, opt, "r", outcome="Private outcome")
    carol_cu = hs.catch_up(carol, threads=ts, decisions=ds)
    assert all(h.thread_id != th.id for h in _all(carol_cu))
    assert "secret-topic" not in carol_cu.topics
    assert hs.count_since(carol, ds) == carol_before == carol_cu.total
    for who in (ben, ana, admin):
        cu = hs.catch_up(who, threads=ts, decisions=ds)
        assert any(h.headline == "Private outcome" for h in _all(cu))
        assert cu.topics.get("secret-topic") == 2


def test_headline_hidden_after_owner_removed(hs: HighlightService, ts: ThreadService,
                                             ds: DecisionService, core: AgoraService,
                                             ana: Participant, ben: Participant,
                                             ana_claude: Participant) -> None:
    _alpha(core, ben, ana)
    th = ts.post(ben, scope="project:alpha", audience="everyone", kind="question",
                 title="Alpha Q", body="?")
    ts.set_state(ben, th.id, "answered", outcome="Alpha answer")
    assert any(h.thread_id == th.id for h in _all(hs.catch_up(ana_claude, threads=ts,
                                                              decisions=ds)))
    before = hs.count_since(ana_claude, ds)
    core.membership(ben, action="remove", project="project:alpha", participant="ana")
    after = hs.catch_up(ana_claude, threads=ts, decisions=ds)
    assert all(h.thread_id != th.id and h.scope != "project:alpha" for h in _all(after))
    assert hs.count_since(ana_claude, ds) < before


def _flood(ts: ThreadService, ana: Participant, n: int, *, scope: str = "universal",
           topics: tuple[str, ...] = ()) -> None:
    for i in range(n):
        th = ts.post(ana, scope=scope, audience="everyone", kind="question",
                     title=f"Q{i}", body="?", topics=topics)
        ts.set_state(ana, th.id, "answered", outcome=f"A{i}")


def test_snapshot_cap_never_skips_unread(hs: HighlightService, ts: ThreadService,
                                         ds: DecisionService, ctx: AgoraContext,
                                         ana: Participant, ben: Participant) -> None:
    ctx.clock = lambda: 5_000_000.0  # every headline gets the same timestamp
    _flood(ts, ana, 501)
    first = hs.catch_up(ben, threads=ts, decisions=ds, limit=1000)
    assert len(_all(first)) == MAX_SNAPSHOT == 500
    assert first.complete is False and first.total == 501 and first.ack is not None
    assert hs.mark_caught_up(ben, first.ack) == first.ack
    second = hs.catch_up(ben, threads=ts, decisions=ds)
    assert len(_all(second)) == 1 and second.complete is True and second.total == 1
    assert second.ack is not None
    hs.mark_caught_up(ben, second.ack)
    assert hs.count_since(ben, ds) == 0


def test_commit_after_bound_waits_for_next_call(hs: HighlightService, ts: ThreadService,
                                                ds: DecisionService, ana: Participant,
                                                ben: Participant,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _flood(ts, ana, 2)
    real = hs.ctx.store.list_highlights

    def racing(**kwargs: Any) -> list[Highlight]:
        out = real(**kwargs)
        monkeypatch.setattr(hs.ctx.store, "list_highlights", real)
        _flood(ts, ana, 1)  # commits after the bound was taken
        return out

    monkeypatch.setattr(hs.ctx.store, "list_highlights", racing)
    cu = hs.catch_up(ben, threads=ts, decisions=ds)
    assert cu.total == 2 and cu.complete and cu.ack is not None
    hs.mark_caught_up(ben, cu.ack)
    nxt = hs.catch_up(ben, threads=ts, decisions=ds)
    assert [h.headline for h in _all(nxt)] == ["A0"]  # the third flood's first item
    assert nxt.total == 1


def test_filtered_view_continues_and_has_no_ack(hs: HighlightService, ts: ThreadService,
                                                ds: DecisionService, core: AgoraService,
                                                ana: Participant) -> None:
    core.scope_create(ana, "project:big")
    _flood(ts, ana, 300, scope="project:big")
    p1 = hs.catch_up(ana, threads=ts, decisions=ds, scope="project:big")
    assert p1.ack is None and not p1.complete and len(_all(p1)) == 200
    assert p1.total == 300
    p2 = hs.catch_up(ana, threads=ts, decisions=ds, scope="project:big",
                     after_seq=p1.last_seq)
    assert p2.ack is None and p2.complete and len(_all(p2)) == 100
    assert {h.id for h in _all(p1)}.isdisjoint({h.id for h in _all(p2)})
    unfiltered_page = hs.catch_up(ana, threads=ts, decisions=ds, after_seq=p1.last_seq)
    assert unfiltered_page.ack is None  # did not start at the marker


def test_history_browse_includes_pinned(hs: HighlightService, ts: ThreadService,
                                        ds: DecisionService, ctx: AgoraContext,
                                        ana: Participant, ben: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="Old", body="the important bit")
    first = ts.get(ana, th.id).messages[0]
    pin = hs.pin(ana, message_id=first.id, headline="Remember this")
    assert pin.pinned_by == "ana" and pin.weight == ACTION_WEIGHTS["pinned"]
    cu = hs.catch_up(ben, threads=ts, decisions=ds)
    hs.mark_caught_up(ben, cu.ack or 0)
    later = ctx.clock() + 10_000
    _flood(ts, ana, 1)
    hist = hs.catch_up(ben, threads=ts, decisions=ds, after_seq=0, since_ts=later)
    assert "Remember this" in [h.headline for h in _all(hist)]
    pinned_only = hs.catch_up(ben, threads=ts, decisions=ds, after_seq=0, pinned_only=True)
    assert [h.headline for h in _all(pinned_only)] == ["Remember this"]
    hs.unpin(ana, pin.id)
    again = hs.catch_up(ben, threads=ts, decisions=ds, after_seq=0, pinned_only=True)
    assert _all(again) == []


def test_expiry_shows_without_opening_thread(hs: HighlightService, ds: DecisionService,
                                             ts: ThreadService, ctx: AgoraContext,
                                             ana: Participant, ben: Participant) -> None:
    th, _ = _decision(ts, ana, timeout=3600)
    cu = hs.catch_up(ben, threads=ts, decisions=ds)
    hs.mark_caught_up(ben, cu.ack or 0)
    assert th.timeout_at is not None
    deadline = th.timeout_at
    ctx.clock = lambda: deadline + 5
    assert hs.count_since(ben, ds) == 1
    [h] = _all(hs.catch_up(ben, threads=ts, decisions=ds))
    assert h.action == "unresolved" and h.actor == "system"
    assert h.headline == "Ledger store: no decision before the timeout"
    stored = ctx.store.get_thread(th.id)
    assert stored is not None and stored.state == "unresolved_advisory"


def test_concurrent_expiry_emits_one_headline(ds: DecisionService,
                                              ts: ThreadService, ctx: AgoraContext,
                                              ana: Participant) -> None:
    th, _ = _decision(ts, ana, timeout=3600)
    assert th.timeout_at is not None
    deadline = th.timeout_at
    ctx.clock = lambda: deadline + 5
    workers = [threading.Thread(target=ds.expire_due) for _ in range(6)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    unresolved = [h for h in ctx.store.list_highlights(after_seq=0,
                                                       upto_seq=ctx.store.max_seq(),
                                                       scopes={"universal"})
                  if h.action == "unresolved"]
    assert len(unresolved) == 1


def test_expiry_retries_after_racing_reply(ds: DecisionService,
                                           ts: ThreadService, ctx: AgoraContext,
                                           ana: Participant,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    th, _ = _decision(ts, ana, timeout=3600)
    assert th.timeout_at is not None
    deadline = th.timeout_at
    real = ctx.store.list_due_decisions

    def racing(now: float) -> list[Thread]:
        due = real(now)
        # A reply lands between listing and expiring, bumping the version.
        ctx.store.add_message(Message("msg_race", th.id, "ben", "", "one more", (),
                                      now))
        return due

    monkeypatch.setattr(ctx.store, "list_due_decisions", racing)
    ctx.clock = lambda: deadline + 5
    assert ds.expire_due() == 1
    stored = ctx.store.get_thread(th.id)
    assert stored is not None and stored.state == "unresolved_advisory"


def test_membership_only_commit_reports_success(hs: HighlightService, ts: ThreadService,
                                                ds: DecisionService, core: AgoraService,
                                                ana: Participant, ben: Participant) -> None:
    _alpha(core, ana, ben)
    cu = hs.catch_up(ana, threads=ts, decisions=ds)
    [joined] = [h for h in _all(cu) if h.action == "membership"]
    assert joined.headline == "ben joined project:alpha" and joined.thread_id is None
    assert joined.weight == ACTION_WEIGHTS["membership"]
    core.membership(ana, action="block", project="project:alpha", participant="ben")
    cu2 = hs.catch_up(ana, threads=ts, decisions=ds)
    assert "ben blocked from project:alpha" in [h.headline for h in _all(cu2)]
    # ben, now blocked, no longer sees the project's headlines.
    assert all(h.scope != "project:alpha"
               for h in _all(hs.catch_up(ben, threads=ts, decisions=ds)))


def test_decline_racing_accept_gets_conflict(ts: ThreadService, core: AgoraService,
                                             ana: Participant, ben: Participant,
                                             ben_claude: Participant,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    core.profile_set(ben, skills=["devops"])
    core.profile_set(ben_claude, skills=["devops"])
    th = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    real_load = ts.load
    raced = {"done": False}

    def stale_load(actor: Participant, thread_id: str) -> Thread:
        stale = real_load(actor, thread_id)
        if actor.name == "ben" and not raced["done"]:
            raced["done"] = True
            ts.handover_respond(ben_claude, thread_id, "accept")
        return stale

    monkeypatch.setattr(ts, "load", stale_load)
    with pytest.raises(AgoraError) as e:
        ts.handover_respond(ben, th.id, "decline", note="busy")
    assert e.value.code == "conflict"
    stored = ts.ctx.store.get_thread(th.id)
    assert stored is not None and stored.state == "accepted"
    assert stored.payload["accepted_by"] == "ben-claude"


def test_failed_headline_write_rolls_back_transition(ds: DecisionService,
                                                     ts: ThreadService,
                                                     hs: HighlightService,
                                                     ana: Participant,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    th, opt = _decision(ts, ana)
    existing = hs.ctx.store.list_highlights(after_seq=0, upto_seq=99,
                                            scopes={"universal"})[0]
    real_make = hs.make

    def duplicate(*args: Any, **kwargs: Any) -> Highlight:
        from dataclasses import replace
        return replace(real_make(*args, **kwargs), id=existing.id)

    monkeypatch.setattr(hs, "make", duplicate)
    with pytest.raises(Exception):  # noqa: B017 - the store's integrity error
        ds.decide(ana, th.id, opt, "why", outcome="Postgres")
    stored = ds.ctx.store.get_thread(th.id)
    assert stored is not None and stored.state == "open"
    assert all(m.body != "Postgres" for m in ds.ctx.store.list_messages(th.id))


def test_timeout_in_long_thread_anchors_event_message(hs: HighlightService,
                                                      ts: ThreadService,
                                                      ds: DecisionService,
                                                      ctx: AgoraContext,
                                                      ana: Participant,
                                                      ben: Participant) -> None:
    th, _ = _decision(ts, ana, timeout=3600)
    for i in range(250):
        ts.reply(ben, th.id, f"msg {i}")
    assert th.timeout_at is not None
    deadline = th.timeout_at
    ctx.clock = lambda: deadline + 5
    [h] = [h for h in _all(hs.catch_up(ben, threads=ts, decisions=ds))
           if h.action == "unresolved"]
    view = ts.get(ben, th.id, around=h.message_id, before=3, after=3)
    assert [m.body for m in view.messages] == [
        "msg 247", "msg 248", "msg 249", "Ledger store: no decision before the timeout"]
    assert view.messages[-1].author == "system"


def test_fallback_headline_fits_200(ts: ThreadService, hs: HighlightService,
                                    ds: DecisionService, ana: Participant,
                                    ana_claude: Participant) -> None:
    title = "T" * 200
    th = ts.post(ana, scope="universal", audience="participant:ana-claude",
                 kind="handover", title=title, handover=HandoverInput(summary="x"))
    ts.handover_respond(ana_claude, th.id, "accept")
    [h] = [h for h in _all(hs.catch_up(ana, threads=ts, decisions=ds))
           if h.action == "handover_accepted"]
    assert len(h.headline) <= 200 and h.headline.endswith(": accepted by ana-claude")
    assert "..." in h.headline
    assert fit_headline("Short", ": withdrawn") == "Short: withdrawn"


def test_plain_reply_makes_no_headline(ts: ThreadService, hs: HighlightService,
                                       ana: Participant) -> None:
    th = ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                 title="Chat", body="x")
    before = hs.ctx.store.max_seq()
    ts.reply(ana, th.id, "another message")
    assert hs.ctx.store.max_seq() == before == 0


def test_group_by_topic_action_scope(ts: ThreadService, hs: HighlightService,
                                     ds: DecisionService, ana: Participant) -> None:
    a = ts.post(ana, scope="universal", audience="everyone", kind="question", title="QA",
                body="?", topics=["db", "infra"])
    ts.set_state(ana, a.id, "answered", outcome="Answered A")
    b = ts.post(ana, scope="universal", audience="everyone", kind="question", title="QB",
                body="?")
    ts.set_state(ana, b.id, "closed", outcome="Closed B")
    by_topic = hs.catch_up(ana, threads=ts, decisions=ds, group_by="topic")
    keys = {g.key: [h.headline for h in g.items] for g in by_topic.groups}
    assert keys == {"db": ["Answered A"], "infra": ["Answered A"], "untagged": ["Closed B"]}
    assert by_topic.topics == {"db": 1, "infra": 1}
    by_action = hs.catch_up(ana, threads=ts, decisions=ds, group_by="action")
    assert {g.key for g in by_action.groups} == {"answered", "closed"}
    by_scope = hs.catch_up(ana, threads=ts, decisions=ds, group_by="scope")
    [group] = by_scope.groups
    assert group.key == "universal"
    assert [h.headline for h in group.items] == ["Answered A", "Closed B"]  # by weight
    only = hs.catch_up(ana, threads=ts, decisions=ds, actions=["closed"])
    assert [h.headline for h in _all(only)] == ["Closed B"] and only.ack is None


def test_only_persons_pin_readable_messages(hs: HighlightService, ts: ThreadService,
                                            core: AgoraService, ana: Participant,
                                            ana_claude: Participant,
                                            carol: Participant) -> None:
    core.scope_create(ana, "project:secret")
    th = ts.post(ana, scope="project:secret", audience="everyone", kind="discussion",
                 title="S", body="private detail")
    msg = ts.get(ana, th.id).messages[0]
    with pytest.raises(AgoraError) as e:
        hs.pin(ana_claude, message_id=msg.id, headline="mine")
    assert e.value.code == "forbidden"
    with pytest.raises(AgoraError) as e2:
        hs.pin(carol, message_id=msg.id, headline="peek")
    assert e2.value.code == "forbidden"
    with pytest.raises(AgoraError) as e3:
        hs.pin(ana, message_id="msg_missing", headline="x")
    assert e3.value.code == "not_found"
    pin = hs.pin(ana, message_id=msg.id, headline="Key detail")
    with pytest.raises(AgoraError) as e4:
        hs.unpin(carol, pin.id)
    assert e4.value.code in ("forbidden", "not_found")


def test_topics_validated(ts: ThreadService, ana: Participant) -> None:
    for topics in (["a", "b", "c", "d", "e", "f"], ["Bad Tag"]):
        with pytest.raises(AgoraError) as e:
            ts.post(ana, scope="universal", audience="everyone", kind="discussion",
                    title="T", body="x", topics=topics)
        assert e.value.code == "invalid"


def test_outcome_secret_refused(ds: DecisionService, ts: ThreadService,
                                ana: Participant) -> None:
    th, opt = _decision(ts, ana)
    with pytest.raises(AgoraError) as e:
        ds.decide(ana, th.id, opt, "why", outcome="token ghp_" + "c" * 36)
    assert e.value.code == "secret_detected"
    stored = ds.ctx.store.get_thread(th.id)
    assert stored is not None and stored.state == "open"


def test_needs_you_excludes_activity(hs: HighlightService, ts: ThreadService,
                                     ds: DecisionService, core: AgoraService,
                                     ana: Participant, ben: Participant) -> None:
    core.subscribe(ben, scope="universal")
    core.contact(ben, action="accept", other="ana")
    ts.post(ana, scope="universal", audience="everyone", kind="discussion", title="Chat",
            body="x")
    q = ts.post(ana, scope="universal", audience="participant:ben", kind="question",
                title="For ben", body="?")
    cu = hs.catch_up(ben, threads=ts, decisions=ds)
    assert [(i.reason, i.thread_id) for i in cu.needs_you] == [("question_to_you", q.id)]


def test_marker_never_moves_back_and_clamps(hs: HighlightService, ts: ThreadService,
                                            ana: Participant) -> None:
    _flood(ts, ana, 3)
    assert hs.mark_caught_up(ana, 10_000) == 3
    assert hs.mark_caught_up(ana, 1) == 3
    with pytest.raises(AgoraError):
        hs.mark_caught_up(ana, -1)
