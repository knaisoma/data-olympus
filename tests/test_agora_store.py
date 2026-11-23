"""Agora storage protocol and SQLite backend (issue #492, Task 4)."""
from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING

from data_olympus.agora.models import (
    Contact,
    Highlight,
    Membership,
    Message,
    Participant,
    Profile,
    Scope,
    Subscription,
    Thread,
    TicketRef,
    Vote,
)
from data_olympus.agora.sqlite_store import SQLiteAgoraStore

if TYPE_CHECKING:
    from pathlib import Path


def _store(tmp_path: Path) -> SQLiteAgoraStore:
    s = SQLiteAgoraStore(str(tmp_path / "agora.db"))
    s.initialize()
    return s


def _thread(tid: str, *, scope: str = "universal", kind: str = "discussion",
            state: str = "open", audience: tuple[str, str] = ("everyone", ""),
            updated: float = 1.0, originator: str = "ana") -> Thread:
    return Thread(
        id=tid, scope=scope, audience_type=audience[0],  # type: ignore[arg-type]
        audience_value=audience[1], kind=kind,  # type: ignore[arg-type]
        title="T", originator=originator, state=state,
        tickets=(TicketRef("github", "o/r#1", "https://x", "Issue"),),
        payload={"k": [1, 2]}, created_at=1.0, updated_at=updated, timeout_at=None,
    )


def _msg(mid: str, tid: str, body: str, ts: float = 2.0) -> Message:
    return Message(mid, tid, "ana", "via", body, ("KB-1",), ts)


def test_initialize_creates_folder_and_leaves_siblings(tmp_path: Path) -> None:
    (tmp_path / "pending").mkdir()
    (tmp_path / "pending" / "p1.json").write_text("{}")
    store = SQLiteAgoraStore(str(tmp_path / "agora" / "agora.db"))
    store.initialize()
    store.initialize()
    assert (tmp_path / "agora" / "agora.db").exists()
    assert (tmp_path / "pending" / "p1.json").read_text() == "{}"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["agora", "pending"]


def test_data_survives_reopen(tmp_path: Path) -> None:
    path = str(tmp_path / "a.db")
    s = SQLiteAgoraStore(path)
    s.initialize()
    s.create_thread(_thread("th_1"), _msg("msg_1", "th_1", "hello"))
    s2 = SQLiteAgoraStore(path)
    s2.initialize()
    th = s2.get_thread("th_1")
    assert th is not None and th.title == "T"
    assert th.tickets == (TicketRef("github", "o/r#1", "https://x", "Issue"),)
    assert th.payload == {"k": [1, 2]}
    assert [m.body for m in s2.list_messages("th_1")] == ["hello"]
    assert s2.list_messages("th_1")[0].refs == ("KB-1",)


def test_concurrent_writers_lose_nothing(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_1"), None)
    errors: list[BaseException] = []

    def post(prefix: str) -> None:
        try:
            for i in range(50):
                s.add_message(_msg(f"msg_{prefix}{i}", "th_1", "x"))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=post, args=(p,)) for p in "ab"]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(s.list_messages("th_1", limit=500)) == 100


def test_list_threads_filters_and_orders(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_a", scope="project:a", updated=1.0), None)
    s.create_thread(_thread("th_b", scope="project:a", kind="question", updated=3.0), None)
    s.create_thread(_thread("th_c", scope="project:b", state="closed", updated=2.0), None)
    s.create_thread(_thread("th_d", audience=("participant", "ben"), updated=4.0,
                            originator="carol"), None)
    assert [t.id for t in s.list_threads()] == ["th_d", "th_b", "th_c", "th_a"]
    assert [t.id for t in s.list_threads(scopes={"project:a"})] == ["th_b", "th_a"]
    assert [t.id for t in s.list_threads(kinds={"question"})] == ["th_b"]
    assert [t.id for t in s.list_threads(states={"closed"})] == ["th_c"]
    assert [t.id for t in s.list_threads(audiences={("participant", "ben")})] == ["th_d"]
    assert [t.id for t in s.list_threads(originators={"carol"})] == ["th_d"]
    assert [t.id for t in s.list_threads(updated_after=2.0)] == ["th_d", "th_b"]
    assert [t.id for t in s.list_threads(limit=1)] == ["th_d"]
    assert s.list_threads(scopes=set()) == []


def test_add_message_bumps_thread_updated_at(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_1", updated=1.0), None)
    s.add_message(_msg("msg_1", "th_1", "x", ts=9.0))
    th = s.get_thread("th_1")
    assert th is not None and th.updated_at == 9.0


def test_vote_upsert_is_one_per_voter(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_1", kind="decision"), None)
    s.put_vote(Vote("th_1", "ana", "person", "opt_a", "", None, "cast", None, 1.0))
    s.put_vote(Vote("th_1", "ana", "person", "opt_b", "why", None, "cast", None, 2.0))
    s.put_vote(Vote("th_1", "bot", "agent", "opt_a", "r", 0.4, "tentative", None, 3.0))
    votes = {v.voter: v for v in s.list_votes("th_1")}
    assert len(votes) == 2 and votes["ana"].option_id == "opt_b"
    assert votes["bot"].confidence == 0.4 and votes["bot"].status == "tentative"


def test_search_is_limited_to_scopes(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_a", scope="project:a"), _msg("m1", "th_a", "kafka topic"))
    s.create_thread(_thread("th_b", scope="project:b"), _msg("m2", "th_b", "kafka broker"))
    hits = s.search('"kafka"', scopes={"project:a"}, limit=10)
    assert [h[0] for h in hits] == ["th_a"]
    both = s.search('"kafka"', scopes={"project:a", "project:b"}, limit=10)
    assert sorted(h[0] for h in both) == ["th_a", "th_b"]
    assert s.search('"kafka"', scopes=set(), limit=10) == []
    # Titles are searchable too, and a thread appears once.
    s.create_thread(
        Thread("th_t", "project:a", "everyone", "", "discussion", "Postgres ledger",
               "ana", "open", (), {}, 1.0, 1.0, None),
        _msg("m3", "th_t", "postgres again"),
    )
    assert [h[0] for h in s.search('"postgres"', scopes={"project:a"}, limit=10)] == ["th_t"]


def test_seen_map_and_subscriptions_roundtrip(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.mark_seen("ana", "th_1", 5.0)
    s.mark_seen("ana", "th_1", 7.0)
    assert s.seen_map("ana", ["th_1", "th_2"]) == {"th_1": 7.0}
    assert s.seen_map("ana", []) == {}
    s.put_subscription(Subscription("ana", "project:a", "reply", ("question",)))
    s.put_subscription(Subscription("ana", "project:a", "notify", ()))
    s.put_subscription(Subscription("ana", "universal", "notify", ()))
    subs = {x.scope: x for x in s.list_subscriptions("ana")}
    assert subs["project:a"].autonomy == "notify" and subs["project:a"].kinds == ()
    s.delete_subscription("ana", "project:a")
    assert [x.scope for x in s.list_subscriptions("ana")] == ["universal"]


def test_participants_profiles_scopes_memberships_contacts(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.upsert_participant(Participant("ana", "person", None, True), 1.0)
    s.upsert_participant(Participant("ana", "person", None, True), 5.0)
    rec = s.get_participant("ana")
    assert rec is not None and (rec.first_seen, rec.last_seen) == (1.0, 5.0)
    assert [r.name for r in s.list_participants()] == ["ana"]
    assert s.get_participant("nobody") is None

    s.put_profile(Profile("ana", "dev", ("python",), "codex", "UTC", 3.0))
    prof = s.get_profile("ana")
    assert prof is not None and prof.skills == ("python",)

    s.put_scope(Scope("project:a", "T3", None, None, "", True, 1.0))
    s.put_scope(Scope("project:a", "T3", "ana", "ana", "git@x", True, 1.0))
    sc = s.get_scope("project:a")
    assert sc is not None and sc.owner == "ana" and sc.remote_url == "git@x"
    assert [x.id for x in s.list_scopes()] == ["project:a"]

    s.put_membership(Membership("project:a", "ana", "owner", "active", None, 1.0))
    s.put_membership(Membership("project:a", "ben", "member", "invited", "ana", 2.0))
    s.put_membership(Membership("*", "ben", "member", "blocked", "adm", 3.0))
    m = s.get_membership("project:a", "ben")
    assert m is not None and m.state == "invited" and m.invited_by == "ana"
    assert len(s.list_memberships(project="project:a")) == 2
    assert len(s.list_memberships(participant="ben")) == 2

    s.put_contact(Contact("ana", "ben", "requested", "hi", 1.0))
    s.put_contact(Contact("ana", "ben", "accepted", "hi", 2.0))
    c = s.get_contact("ana", "ben")
    assert c is not None and c.state == "accepted"
    assert len(s.list_contacts(recipient="ana", state="accepted")) == 1
    assert s.list_contacts(sender="ana") == []


def _hl(hid: str, *, thread: str | None = "th_1", msg: str | None = None,
        scope: str = "universal", action: str = "decided", topics: tuple[str, ...] = (),
        pinned: str | None = None, ts: float = 5.0) -> Highlight:
    return Highlight(hid, 0, scope, thread, msg, action, f"headline {hid}", "ana", topics,
                     100, pinned, ts)


def test_commit_event_is_a_version_cas(tmp_path: Path) -> None:
    s = _store(tmp_path)
    th = _thread("th_1")
    s.create_thread(th, None)
    stored = s.get_thread("th_1")
    assert stored is not None and stored.version == 0
    res = s.commit_event(thread=replace(th, state="closed", payload={"x": 1}),
                         expected_version=0,
                         message=_msg("msg_1", "th_1", "Closed it", ts=8.0),
                         highlight=_hl("hl_1", msg="msg_1"))
    assert res.ok and res.thread is not None
    assert res.thread.version == 1 and res.thread.state == "closed"
    assert res.thread.updated_at == 8.0
    stale = s.commit_event(thread=replace(th, state="open"), expected_version=0,
                           message=_msg("msg_2", "th_1", "stale"),
                           highlight=_hl("hl_2", msg="msg_2"))
    assert not stale.ok and stale.thread is None
    got = s.get_thread("th_1")
    assert got is not None and got.state == "closed" and got.payload == {"x": 1}
    assert [m.id for m in s.list_messages("th_1")] == ["msg_1"]
    assert s.get_highlight("hl_2") is None
    h = s.get_highlight("hl_1")
    assert h is not None and h.seq == 1 and h.message_id == "msg_1"


def test_commit_event_rolls_back_as_a_unit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    th = _thread("th_1")
    s.create_thread(th, None)
    s.commit_event(highlight=_hl("hl_dup"))
    import sqlite3

    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        s.commit_event(thread=replace(th, state="closed"), expected_version=0,
                       message=_msg("msg_x", "th_1", "x"), highlight=_hl("hl_dup"))
    got = s.get_thread("th_1")
    assert got is not None and got.state == "open" and got.version == 0
    assert s.list_messages("th_1") == []


def test_membership_only_commit(tmp_path: Path) -> None:
    s = _store(tmp_path)
    res = s.commit_event(membership=Membership("project:p", "ana", "member", "active",
                                               None, 1.0),
                         highlight=_hl("hl_m", thread=None, scope="project:p",
                                       action="membership"))
    assert res.ok and res.thread is None
    assert s.get_membership("project:p", "ana") is not None
    s.put_membership(Membership("project:p", "ana", "owner", "active", None, 1.0))
    refused = s.commit_event(
        membership=Membership("project:p", "ana", "member", "removed", None, 2.0),
        highlight=_hl("hl_m2", thread=None, scope="project:p", action="membership"))
    assert not refused.ok and s.get_highlight("hl_m2") is None


def test_add_message_bumps_version(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_1"), None)
    s.add_message(_msg("msg_1", "th_1", "x"))
    got = s.get_thread("th_1")
    assert got is not None and got.version == 1


def test_create_thread_with_highlight_and_topics(tmp_path: Path) -> None:
    s = _store(tmp_path)
    th = replace(_thread("th_1"), topics=("db", "infra"))
    s.create_thread(th, _msg("msg_1", "th_1", "body"), highlight=_hl("hl_1", msg="msg_1"))
    got = s.get_thread("th_1")
    assert got is not None and got.topics == ("db", "infra")
    assert s.max_seq() == 1


def test_messages_around_and_get_message(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.create_thread(_thread("th_1"), None)
    for i in range(30):
        s.add_message(_msg(f"msg_{i}", "th_1", f"m{i}", ts=10.0 + i))
    around = s.list_messages_around("th_1", "msg_15", before=3, after=2)
    assert [m.id for m in around] == ["msg_12", "msg_13", "msg_14", "msg_15",
                                      "msg_16", "msg_17"]
    assert s.list_messages_around("th_1", "msg_missing", before=3, after=3) == []
    m = s.get_message("msg_3")
    assert m is not None and m.body == "m3"
    assert s.get_message("nope") is None


def test_expire_if_due_reads_fresh_state(tmp_path: Path) -> None:
    s = _store(tmp_path)
    th = replace(_thread("th_d", kind="decision"), timeout_at=50.0)
    s.create_thread(th, None)
    s.create_thread(replace(_thread("th_e", kind="decision"), timeout_at=500.0), None)
    assert [t.id for t in s.list_due_decisions(100.0)] == ["th_d"]
    s.add_message(_msg("msg_r", "th_d", "racing reply", ts=60.0))  # bumps the version

    def build(fresh: Thread) -> tuple[Message, Highlight | None]:
        assert fresh.version == 1
        return (_msg("msg_t", "th_d", "timed out", ts=100.0),
                _hl("hl_t", thread="th_d", msg="msg_t", action="unresolved"))

    expired = s.expire_if_due("th_d", 100.0, build)
    assert expired is not None and expired.state == "unresolved_advisory"
    assert expired.version == 2
    assert s.expire_if_due("th_d", 100.0, build) is None
    assert s.expire_if_due("th_e", 100.0, build) is None
    assert s.list_due_decisions(100.0) == []


def test_highlight_listing_and_catchup_marker(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.commit_event(highlight=_hl("h1", scope="project:a", topics=("db",), ts=1.0))
    s.commit_event(highlight=_hl("h2", scope="project:b", action="answered", ts=2.0))
    s.commit_event(highlight=_hl("h3", scope="project:a", pinned="ana", ts=3.0))
    assert s.max_seq() == 3
    ids = [h.id for h in s.list_highlights(after_seq=0, upto_seq=3,
                                           scopes={"project:a", "project:b"})]
    assert ids == ["h1", "h2", "h3"]
    assert [h.id for h in s.list_highlights(after_seq=1, upto_seq=2,
                                            scopes={"project:a", "project:b"})] == ["h2"]
    assert [h.id for h in s.list_highlights(after_seq=0, upto_seq=3,
                                            scopes={"project:a"})] == ["h1", "h3"]
    assert [h.id for h in s.list_highlights(after_seq=0, upto_seq=3, scopes={"project:a",
                                            "project:b"}, actions={"answered"})] == ["h2"]
    assert [h.id for h in s.list_highlights(after_seq=0, upto_seq=3, scopes={"project:a"},
                                            topics={"db"})] == ["h1"]
    assert [h.id for h in s.list_highlights(after_seq=0, upto_seq=3, scopes={"project:a"},
                                            pinned_only=True)] == ["h3"]
    assert [h.id for h in s.list_highlights(after_seq=0, upto_seq=3,
                                            scopes={"project:a", "project:b"},
                                            since_ts=2.0)] == ["h2", "h3"]
    h1 = s.get_highlight("h1")
    assert h1 is not None
    s.update_highlight(replace(h1, pinned_by="ben"))
    again = s.get_highlight("h1")
    assert again is not None and again.pinned_by == "ben" and again.seq == 1
    assert s.get_catchup("ana") == 0
    s.set_catchup("ana", 2)
    assert s.get_catchup("ana") == 2


def test_create_owned_scope_is_atomic_and_exclusive(tmp_path: Path) -> None:
    s = _store(tmp_path)
    first = s.create_owned_scope(
        Scope("project:p", "T3", "ana", "ana", "", False, 1.0),
        Membership("project:p", "ana", "owner", "active", None, 1.0))
    second = s.create_owned_scope(
        Scope("project:p", "T3", "ben", "ben", "", False, 2.0),
        Membership("project:p", "ben", "owner", "active", None, 2.0))
    assert first is True and second is False
    assert s.get_membership("project:p", "ben") is None
    scope = s.get_scope("project:p")
    assert scope is not None and scope.owner == "ana"


def test_update_membership_keeps_an_owner(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.put_scope(Scope("project:p", "T3", "ana", "ana", "", False, 1.0))
    s.put_membership(Membership("project:p", "ana", "owner", "active", None, 1.0))
    s.put_membership(Membership("project:p", "ben", "owner", "active", None, 1.0))
    # Two owners each remove the other from stale state: only one may succeed.
    assert s.update_membership(Membership("project:p", "ana", "owner", "removed", "ben", 2.0))
    assert not s.update_membership(Membership("project:p", "ben", "owner", "removed", "ana", 2.0))
    assert not s.update_membership(Membership("project:p", "ben", "member", "active", "x", 3.0))
    assert not s.update_membership(Membership("project:p", "ben", "member", "blocked", "x", 3.0))
    ben = s.get_membership("project:p", "ben")
    assert ben is not None and ben.role == "owner" and ben.state == "active"
    # Changes that leave an owner in place go through.
    assert s.update_membership(Membership("project:p", "cat", "member", "invited", "ben", 4.0))


def test_claim_refused_once_a_project_had_an_owner(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.put_scope(Scope("project:p", "T3", None, None, "", True, 1.0))
    s.put_membership(Membership("project:p", "ana", "owner", "removed", None, 1.0))
    assert not s.claim_project(
        "project:p", "ben", Membership("project:p", "ben", "owner", "active", None, 2.0))
    s.put_scope(Scope("project:q", "T3", None, None, "", True, 1.0))
    assert s.claim_project(
        "project:q", "ben", Membership("project:q", "ben", "owner", "active", None, 2.0))
    assert not s.claim_project(
        "project:q", "ana", Membership("project:q", "ana", "owner", "active", None, 3.0))
