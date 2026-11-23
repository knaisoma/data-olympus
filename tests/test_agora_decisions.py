# ruff: noqa: F811 - pytest fixtures imported from tests.agora_world
"""Agora decision requests and votes (issue #492, Task 8)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from data_olympus.agora.errors import AgoraError
from data_olympus.agora.threads import (
    DecisionInput,
    HandoverInput,
    OptionInput,
    RecommendationInput,
)
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
    from data_olympus.agora.models import Participant, Thread
    from data_olympus.agora.threads import ThreadService


def _decision(ts: ThreadService, who: Participant, *, agent_votes: bool | None = None,
              timeout: int | None = None, voting: bool = True,
              recommend: bool = False) -> tuple[Thread, str, str]:
    th = ts.post(who, scope="universal", audience="everyone", kind="decision",
                 title="Ledger store",
                 decision=DecisionInput(
                     question="Which database for the ledger?",
                     context="We need ACID.",
                     options=[OptionInput("Postgres", pros="mature"),
                              OptionInput("SQLite", cons="single writer")],
                     recommendation=(RecommendationInput(0, "battle tested")
                                     if recommend else None),
                     voting=voting, agent_votes=agent_votes, timeout_sec=timeout))
    opts = [o["id"] for o in th.payload["options"]]
    return th, opts[0], opts[1]


def test_payload_shape(ts: ThreadService, ana: Participant) -> None:
    th, a, _ = _decision(ts, ana, recommend=True)
    p = th.payload
    assert th.state == "open" and th.timeout_at is not None
    assert p["question"] == "Which database for the ledger?" and p["context"] == "We need ACID."
    assert [o["title"] for o in p["options"]] == ["Postgres", "SQLite"]
    assert p["options"][0]["proposed_by"] == "ana" and p["options"][0]["pros"] == "mature"
    assert p["recommendation"] == {"option_id": a, "rationale": "battle tested",
                                   "proposed_by": "ana"}
    assert p["voting"] is True and p["agent_votes"] is True
    assert p["close_policy"] == "originator" and p["decided"] is None


def test_person_vote_counts(ds: DecisionService, ts: ThreadService, ana: Participant,
                            ben: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    v = ds.vote(ben, th.id, a, confidence=0.2)
    assert v.status == "cast" and v.confidence is None
    assert ds.tally(th.id).people == {a: 1}


def test_agent_high_confidence_cast(ds: DecisionService, ts: ThreadService,
                                    ana: Participant, ana_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    v = ds.vote(ana_claude, th.id, a, rationale="fits", confidence=0.9)
    assert v.status == "cast"
    t = ds.tally(th.id)
    assert t.agents == {a: 1} and t.people == {}
    assert t.agent_votes == [{"voter": "ana-claude", "option_id": a, "confidence": 0.9,
                              "status": "cast"}]


def test_agent_low_confidence_tentative_goes_to_owner(ds: DecisionService,
                                                      ts: ThreadService, ana: Participant,
                                                      ben: Participant,
                                                      ana_claude: Participant) -> None:
    th, opt, _ = _decision(ts, ben)
    v = ds.vote(ana_claude, th.id, opt, rationale="unsure", confidence=0.4)
    assert v.status == "tentative" and ds.tally(th.id).agents == {}
    assert any(i.reason == "tentative_vote_to_review" for i in ts.inbox(ana))
    assert not any(i.reason == "tentative_vote_to_review" for i in ts.inbox(ben))


def test_needs_human_forces_tentative(ds: DecisionService, ts: ThreadService,
                                      ana: Participant, ana_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    v = ds.vote(ana_claude, th.id, a, rationale="people matter", confidence=0.99,
                needs_human=True)
    assert v.status == "tentative"


def test_owner_confirms_changes_or_drops(ds: DecisionService, ts: ThreadService,
                                         ana: Participant, ben: Participant,
                                         ana_claude: Participant,
                                         ben_claude: Participant) -> None:
    th, a, b = _decision(ts, carol_person())
    ds.vote(ana_claude, th.id, a, rationale="r", confidence=0.1)
    ds.vote(ben_claude, th.id, a, rationale="r", confidence=0.1)
    c = ds.review_vote(ana, th.id, agent="ana-claude", action="confirm")
    assert c.status == "confirmed" and c.reviewed_by == "ana"
    ch = ds.review_vote(ben, th.id, agent="ben-claude", action="change", option_id=b)
    assert ch.status == "confirmed" and ch.option_id == b
    assert ds.tally(th.id).agents == {a: 1, b: 1}
    with pytest.raises(AgoraError) as e:
        ds.review_vote(ana, th.id, agent="ana-claude", action="drop")
    assert e.value.code == "conflict"  # no longer tentative
    ds.vote(ana_claude, th.id, b, rationale="r2", confidence=0.2)
    d = ds.review_vote(ana, th.id, agent="ana-claude", action="drop")
    assert d.status == "dropped"
    assert ds.tally(th.id).agents == {b: 1}


def carol_person() -> Participant:
    from tests.agora_world import participant
    return participant("carol")


def test_revote_clears_review(ds: DecisionService, ts: ThreadService, ana: Participant,
                              ana_claude: Participant) -> None:
    th, a, b = _decision(ts, ana)
    ds.vote(ana_claude, th.id, a, rationale="r", confidence=0.1)
    ds.review_vote(ana, th.id, agent="ana-claude", action="confirm")
    v = ds.vote(ana_claude, th.id, b, rationale="changed my mind", confidence=0.95)
    assert v.status == "cast" and v.reviewed_by is None and v.option_id == b


def test_stranger_cannot_review(ds: DecisionService, ts: ThreadService, ana: Participant,
                                ben: Participant, ana_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    ds.vote(ana_claude, th.id, a, rationale="r", confidence=0.1)
    with pytest.raises(AgoraError) as e:
        ds.review_vote(ben, th.id, agent="ana-claude", action="confirm")
    assert e.value.code == "forbidden"


def test_ownerless_tentative_shown_not_counted(ds: DecisionService, ts: ThreadService,
                                               ana: Participant, rogue: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    ds.vote(rogue, th.id, a, rationale="maybe", confidence=0.3)
    t = ds.tally(th.id)
    assert t.agents == {} and t.tentative[0]["ownerless"] is True
    assert t.tentative[0]["confidence"] == 0.3


def test_agent_votes_off_per_decision_and_instance(ds: DecisionService,
                                                   ts: ThreadService, ctx: AgoraContext,
                                                   ana: Participant,
                                                   ana_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana, agent_votes=False)
    with pytest.raises(AgoraError) as e:
        ds.vote(ana_claude, th.id, a, rationale="r", confidence=0.9)
    assert e.value.code == "forbidden"
    from dataclasses import replace
    ctx.settings = replace(ctx.settings, agent_votes=False)
    th2, a2, _ = _decision(ts, ana)
    assert th2.payload["agent_votes"] is False
    with pytest.raises(AgoraError):
        ds.vote(ana_claude, th2.id, a2, rationale="r", confidence=0.9)
    th3, a3, _ = _decision(ts, ana, agent_votes=True)
    assert ds.vote(ana_claude, th3.id, a3, rationale="r", confidence=0.9).status == "cast"


def test_agent_vote_requires_confidence_and_rationale(ds: DecisionService,
                                                      ts: ThreadService, ana: Participant,
                                                      ana_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    for kwargs in ({"confidence": 0.9}, {"rationale": "r"},
                   {"rationale": "r", "confidence": 1.5},
                   {"rationale": "r", "confidence": -0.1}):
        with pytest.raises(AgoraError) as e:
            ds.vote(ana_claude, th.id, a, **kwargs)  # type: ignore[arg-type]
        assert e.value.code == "invalid"


def test_vote_errors(ds: DecisionService, ts: ThreadService, ana: Participant,
                     ben: Participant) -> None:
    th, _, _ = _decision(ts, ana)
    with pytest.raises(AgoraError) as e:
        ds.vote(ben, th.id, "opt_missing")
    assert e.value.code == "invalid"
    off, a, _ = _decision(ts, ana, voting=False)
    with pytest.raises(AgoraError) as e2:
        ds.vote(ben, off.id, a)
    assert e2.value.code == "invalid"


def test_only_person_decides_and_agent_owner_decides_for_agent(
    ds: DecisionService, ts: ThreadService, ana: Participant, ana_claude: Participant,
    ben: Participant,
) -> None:
    th, a, _ = _decision(ts, ana_claude)
    with pytest.raises(AgoraError) as e:
        ds.decide(ana_claude, th.id, a, "agents never close", outcome="x")
    assert e.value.code == "forbidden"
    with pytest.raises(AgoraError) as e2:
        ds.decide(ben, th.id, a, "not mine", outcome="x")
    assert e2.value.code == "forbidden"
    done = ds.decide(ana, th.id, a, "Postgres fits", outcome="Postgres for the ledger")
    assert done.state == "decided"
    assert done.payload["decided"]["option_id"] == a
    assert done.payload["decided"]["by"] == "ana"
    with pytest.raises(AgoraError) as e3:
        ds.decide(ana, th.id, a, "again", outcome="again")
    assert e3.value.code == "conflict"


def test_autonomous_agent_cannot_originate_decision(ts: ThreadService,
                                                    rogue: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        _decision(ts, rogue)
    assert e.value.code == "forbidden"


def test_tallies_separate_people_and_agents(ds: DecisionService, ts: ThreadService,
                                            ana: Participant, ben: Participant,
                                            ana_claude: Participant,
                                            ben_claude: Participant) -> None:
    th, a, b = _decision(ts, ana)
    ds.vote(ana, th.id, a)
    ds.vote(ben, th.id, b, rationale="simpler")
    ds.vote(ana_claude, th.id, a, rationale="r", confidence=0.9)
    ds.vote(ben_claude, th.id, a, rationale="r", confidence=0.95)
    t = ds.tally(th.id)
    assert t.people == {a: 1, b: 1} and t.agents == {a: 2}


def test_options_and_recommendation(ds: DecisionService, ts: ThreadService,
                                    ana: Participant, ben_claude: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    c = ds.add_option(ben_claude, th.id, OptionInput("DynamoDB", cons="vendor lock"))
    after = ds.recommend(ben_claude, th.id, c, "scales")
    assert after.payload["recommendation"] == {"option_id": c, "rationale": "scales",
                                               "proposed_by": "ben-claude"}
    assert [o["title"] for o in after.payload["options"]][-1] == "DynamoDB"
    with pytest.raises(AgoraError):
        ds.recommend(ben_claude, th.id, "opt_missing", "x")
    for i in range(7):
        ds.add_option(ana, th.id, OptionInput(f"o{i}"))
    with pytest.raises(AgoraError) as e:
        ds.add_option(ana, th.id, OptionInput("eleventh"))
    assert e.value.code == "invalid"


def test_too_many_options_at_post(ts: ThreadService, ana: Participant) -> None:
    with pytest.raises(AgoraError) as e:
        ts.post(ana, scope="universal", audience="everyone", kind="decision", title="D",
                decision=DecisionInput(question="?",
                                       options=[OptionInput(f"o{i}") for i in range(11)]))
    assert e.value.code == "invalid"
    with pytest.raises(AgoraError) as e2:
        ts.post(ana, scope="universal", audience="everyone", kind="decision", title="D",
                decision=DecisionInput(question="?", options=[OptionInput("a")],
                                       recommendation=RecommendationInput(3, "x")))
    assert e2.value.code == "invalid"


def test_timeout_applies_lazily_on_read(ds: DecisionService, ts: ThreadService,
                                        ctx: AgoraContext, ana: Participant,
                                        ben: Participant) -> None:
    th, opt, _ = _decision(ts, ana)
    assert th.timeout_at is not None
    deadline = th.timeout_at
    ctx.clock = lambda: deadline + 1
    assert ts.get(ben, th.id).thread.state == "unresolved_advisory"
    with pytest.raises(AgoraError, match="conflict"):
        ds.vote(ben, th.id, opt)
    assert ds.decide(ana, th.id, opt, "late call", outcome="Late: Postgres").state == "decided"


def test_unresolved_can_be_withdrawn_and_inbox_asks_decider(
    ds: DecisionService, ts: ThreadService, ctx: AgoraContext, ana: Participant,
) -> None:
    th, _, _ = _decision(ts, ana)
    assert th.timeout_at is not None
    deadline = th.timeout_at
    ctx.clock = lambda: deadline + 1
    assert th.id in [i.thread_id for i in ts.inbox(ana) if i.reason == "decision_to_decide"]
    assert ds.withdraw(ana, th.id).state == "withdrawn"


def test_timeout_bounds(ts: ThreadService, ana: Participant) -> None:
    for bad in (60, 3599, 7776001):
        with pytest.raises(AgoraError, match="invalid"):
            ts.post(ana, scope="universal", audience="everyone", kind="decision",
                    title="D", decision=DecisionInput(question="?", timeout_sec=bad))
    for ok in (3600, 7776000):
        ts.post(ana, scope="universal", audience="everyone", kind="decision", title="D",
                decision=DecisionInput(question="?", timeout_sec=ok))


def test_decided_and_withdrawn_refuse_votes(ds: DecisionService, ts: ThreadService,
                                            ana: Participant, ben: Participant) -> None:
    th, a, _ = _decision(ts, ana)
    ds.decide(ana, th.id, a, "done", outcome="Postgres")
    with pytest.raises(AgoraError) as e:
        ds.vote(ben, th.id, a)
    assert e.value.code == "conflict"
    th2, a2, _ = _decision(ts, ana)
    ds.withdraw(ana, th2.id, outcome="No longer needed")
    with pytest.raises(AgoraError) as e2:
        ds.vote(ben, th2.id, a2)
    assert e2.value.code == "conflict"
    with pytest.raises(AgoraError) as e3:
        ds.withdraw(ana, th2.id)
    assert e3.value.code == "conflict"


def test_withdraw_rules(ds: DecisionService, ts: ThreadService, core: Any,
                        ana: Participant, ben: Participant, ana_claude: Participant,
                        ben_claude: Participant) -> None:
    th, _, _ = _decision(ts, ana_claude)
    with pytest.raises(AgoraError) as e:
        ds.withdraw(ben, th.id)
    assert e.value.code == "forbidden"
    assert ds.withdraw(ana, th.id).state == "withdrawn"
    core.profile_set(ben_claude, skills=["devops"])
    ho = ts.post(ana, scope="universal", audience="capability:devops", kind="handover",
                 title="Deploy", handover=HandoverInput(summary="x"))
    assert ds.withdraw(ana, ho.id).state == "withdrawn"
    with pytest.raises(AgoraError) as e2:
        ts.handover_respond(ben_claude, ho.id, "accept")
    assert e2.value.code == "conflict"


def test_inbox_decision_reasons(ds: DecisionService, ts: ThreadService, ana: Participant,
                                ben: Participant, core: Any) -> None:
    core.subscribe(ben, scope="universal")
    th, a, _ = _decision(ts, ana)
    assert [i.reason for i in ts.inbox(ben) if i.thread_id == th.id] == [
        "decision_vote_requested"]
    assert th.id not in [i.thread_id for i in ts.inbox(ana)
                         if i.reason == "decision_to_decide"]
    ds.vote(ben, th.id, a)
    assert th.id not in [i.thread_id for i in ts.inbox(ben)
                         if i.reason == "decision_vote_requested"]
    assert th.id in [i.thread_id for i in ts.inbox(ana) if i.reason == "decision_to_decide"]
