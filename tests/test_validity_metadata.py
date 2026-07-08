"""Tests for validity/freshness frontmatter metadata with hard expiry semantics
(issue #107, decision comment 2026-07-08).

Covers the engine (Index.search: default expiry exclusion, in_force window,
include_expired, validity_state facet), kb_search_fn/kb_get_fn freshness
wiring, kb_consult never returning expired docs, REST param threading, the
health malformed-validity counter, and the CLI validity report.

All date logic is exercised with an explicit, injected ``today`` so nothing
here depends on the wall clock.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from data_olympus.index import Index
from data_olympus.server import build_app
from data_olympus.tools_enforce import kb_consult_fn
from data_olympus.tools_read import kb_get_fn, kb_search_fn

if TYPE_CHECKING:
    from pathlib import Path

TODAY = "2026-07-08"
YESTERDAY = "2026-07-07"
TOMORROW = "2026-07-09"


def _shift(day: str, days: int) -> str:
    """Return ``day`` (ISO date) plus ``days`` calendar days, as ISO."""
    import datetime

    return (datetime.date.fromisoformat(day) + datetime.timedelta(days=days)).isoformat()


def _write(
    kb: Path,
    rel: str,
    *,
    id_: str,
    status: str = "active",
    title: str = "",
    body: str = "widget body",
    validity: str = "",
    tier: str = "T1",
) -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: {tier}\n"
        f"category: foundation\ntitle: {title or id_}\n"
        f"{validity}"
        "---\n"
        f"# {title or id_}\n\n{body}\n",
        encoding="utf-8",
    )


def _build_kb(tmp_path: Path, *, today: str = TODAY) -> Path:
    """A KB whose validity dates are RELATIVE to ``today``.

    The engine/tools tests inject the fixed ``TODAY`` everywhere, so they use
    the module constants unchanged. The REST tests exercise the real server
    path (which reads the wall clock), so they pass ``today=today_iso()`` and
    the yesterday/boundary/tomorrow dates shift with it — the assertions stay
    true on any calendar day (codex review blocker 1).
    """
    yesterday = _shift(today, -1)
    tomorrow = _shift(today, 1)
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(kb, "universal/foundation/fresh.md", id_="DOC-FRESH", body="widget content fresh")
    _write(
        kb, "universal/foundation/expired.md", id_="DOC-EXPIRED",
        body="widget content expired",
        validity=f"validity:\n  valid_until: {yesterday}\n",
    )
    _write(
        kb, "universal/foundation/boundary.md", id_="DOC-BOUNDARY",
        body="widget content boundary",
        validity=f"validity:\n  valid_until: {today}\n",
    )
    _write(
        kb, "universal/foundation/upcoming.md", id_="DOC-UPCOMING",
        body="widget content upcoming",
        validity=f"validity:\n  valid_from: {tomorrow}\n",
    )
    _write(
        kb, "universal/foundation/stale.md", id_="DOC-STALE",
        body="widget content stale",
        validity=f"validity:\n  recheck_by: {yesterday}\n",
    )
    return kb


def _idx(tmp_path: Path, tmp_index_path: Path) -> Index:
    kb = _build_kb(tmp_path)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    return idx


# ---------------------------------------------------------------------------
# Scenario 1: expired doc absent by default and from in_force=True; present
# with include_expired=True; kb_get always resolves; kb_consult never returns it.
# ---------------------------------------------------------------------------


def test_expired_doc_absent_from_default_search(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, today=TODAY)}
    assert "DOC-EXPIRED" not in ids
    assert "DOC-FRESH" in ids


def test_expired_doc_absent_from_in_force(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-EXPIRED" not in ids


def test_expired_doc_present_with_include_expired(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, include_expired=True, today=TODAY)}
    assert "DOC-EXPIRED" in ids


def test_kb_search_fn_expired_carries_freshness_expired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", include_expired=True, today=TODAY)
    hit = next(h for h in resp.hits if h.id == "DOC-EXPIRED")
    assert hit.freshness == "expired"


def test_kb_search_fn_default_never_carries_expired_freshness(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    assert all(h.id != "DOC-EXPIRED" for h in resp.hits)
    assert all(h.freshness != "expired" for h in resp.hits)


def test_kb_get_always_resolves_expired_doc(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_get_fn(idx=idx, id="DOC-EXPIRED", today=TODAY)
    assert resp.id == "DOC-EXPIRED"
    assert resp.freshness == "expired"
    assert resp.validity is not None
    assert resp.validity["valid_until"] == YESTERDAY


def test_kb_consult_never_returns_expired_doc(tmp_path: Path, tmp_index_path: Path) -> None:
    from data_olympus.enforce_policy import ConsultationLedger, IntentClassifier

    idx = _idx(tmp_path, tmp_index_path)
    classifier = IntentClassifier()
    ledger = ConsultationLedger()
    resp = kb_consult_fn(
        idx=idx, classifier=classifier, ledger=ledger,
        workspace="ws", intent="schema convention for widget docs",
        source_session="s1", agent_identity="tester", ttl_sec=3600, now=1000.0,
    )
    assert resp.is_governed_decision is True
    ids = {h.id for h in resp.rules}
    assert "DOC-EXPIRED" not in ids


# ---------------------------------------------------------------------------
# Scenario 2: valid_until == today is still in force and visible (inclusive).
# ---------------------------------------------------------------------------


def test_boundary_day_valid_until_today_is_in_force(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-BOUNDARY" in ids


def test_boundary_day_visible_in_default_search(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, today=TODAY)}
    assert "DOC-BOUNDARY" in ids


# ---------------------------------------------------------------------------
# Scenario 3: valid_from tomorrow -> excluded from in_force, present in
# default search flagged upcoming.
# ---------------------------------------------------------------------------


def test_upcoming_doc_excluded_from_in_force(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-UPCOMING" not in ids


def test_upcoming_doc_present_in_default_search(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, today=TODAY)}
    assert "DOC-UPCOMING" in ids


def test_kb_search_fn_upcoming_freshness(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    hit = next(h for h in resp.hits if h.id == "DOC-UPCOMING")
    assert hit.freshness == "upcoming"


# ---------------------------------------------------------------------------
# Scenario 4: recheck_by yesterday -> in force, visible, freshness=stale.
# ---------------------------------------------------------------------------


def test_stale_doc_in_force_and_visible(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-STALE" in ids


def test_kb_search_fn_stale_freshness(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    hit = next(h for h in resp.hits if h.id == "DOC-STALE")
    assert hit.freshness == "stale"


def test_lint_fires_for_stale_doc(tmp_path: Path) -> None:
    from data_olympus.format.document import Document
    from data_olympus.format.validate import validate_document

    p = tmp_path / "x.md"
    p.write_text(
        "---\nid: A-1\ntype: standard\nstatus: active\ntier: T1\n"
        "title: t\ndescription: d\ntags: [x]\ntimestamp: 2026-01-01\n"
        f"validity:\n  recheck_by: {YESTERDAY}\n---\nbody\n",
        encoding="utf-8",
    )
    findings = validate_document(Document.load(p), today=TODAY)
    assert any(f.field == "validity" and "recheck_by" in f.message for f in findings)


# ---------------------------------------------------------------------------
# Scenario 5: validity_state facet.
# ---------------------------------------------------------------------------


def test_validity_state_expired_returns_only_expired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("widget", limit=20, validity_state="expired", today=TODAY)
    ids = {h.id for h in hits}
    assert ids == {"DOC-EXPIRED"}


def test_validity_state_expiring_within_days(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    # DOC-BOUNDARY expires TODAY, so it falls within an "expiring in 5 days"
    # window; DOC-EXPIRED already expired YESTERDAY and must not appear here.
    hits = idx.search(
        "widget", limit=20, validity_state="expiring_within:5", today=TODAY,
    )
    ids = {h.id for h in hits}
    assert "DOC-BOUNDARY" in ids
    assert "DOC-EXPIRED" not in ids


def test_validity_state_stale_returns_only_stale(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search("widget", limit=20, validity_state="stale", today=TODAY)
    ids = {h.id for h in hits}
    assert ids == {"DOC-STALE"}


def test_validity_state_expired_implies_include_expired(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    """Filtering FOR expired must not also require include_expired=True."""
    idx = _idx(tmp_path, tmp_index_path)
    hits = idx.search(
        "widget", limit=20, validity_state="expired", include_expired=False, today=TODAY,
    )
    assert {h.id for h in hits} == {"DOC-EXPIRED"}


# ---------------------------------------------------------------------------
# Scenario 6: malformed validity -> absent + health counter + lint warning.
# ---------------------------------------------------------------------------


def test_malformed_validity_treated_as_absent_and_counted(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(
        kb, "universal/foundation/bad.md", id_="DOC-BAD",
        body="widget content bad",
        validity="validity:\n  valid_until: not-a-real-date\n",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    # Absent: the doc is NOT excluded as expired, and is NOT flagged upcoming.
    hits = idx.search("widget", limit=20, today=TODAY)
    ids = {h.id for h in hits}
    assert "DOC-BAD" in ids
    health = idx.health()
    assert health.get("malformed_validity", 0) == 1


def test_lint_warns_malformed_validity_end_to_end(tmp_path: Path) -> None:
    from data_olympus.format import lint_bundle

    kb = tmp_path / "kb"
    kb.mkdir()
    _write(
        kb, "bad.md", id_="DOC-BAD", body="body",
        validity="validity:\n  valid_until: not-a-real-date\n",
    )
    results = lint_bundle(kb)
    findings = results.get(kb / "bad.md", [])
    assert any(
        f.severity == "warning" and f.field == "validity" and "malformed" in f.message
        for f in findings
    )
    assert all(f.severity != "error" for f in findings)


# ---------------------------------------------------------------------------
# Scenario 7: datetime and timezone-suffixed values normalize to dates.
# ---------------------------------------------------------------------------


def test_datetime_and_tz_suffixed_validity_values_normalize(
    tmp_path: Path, tmp_index_path: Path,
) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    _write(
        kb, "universal/foundation/tz.md", id_="DOC-TZ", body="widget content tz",
        validity="validity:\n  valid_until: '2026-07-08T23:00:00+02:00'\n",
    )
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    # Normalized to date 2026-07-08 == TODAY, so still in force (boundary).
    ids = {h.id for h in idx.search("widget", limit=20, in_force=True, today=TODAY)}
    assert "DOC-TZ" in ids
    # And expired the day after.
    ids_tomorrow = {
        h.id for h in idx.search("widget", limit=20, in_force=True, today=TOMORROW)
    }
    assert "DOC-TZ" not in ids_tomorrow


# ---------------------------------------------------------------------------
# Scenario 8: lint - expired-but-active warning fires; none is an error.
# ---------------------------------------------------------------------------


def test_lint_expired_but_active_end_to_end(tmp_path: Path) -> None:
    from data_olympus.format import lint_bundle

    kb = tmp_path / "kb"
    kb.mkdir()
    _write(
        kb, "expired.md", id_="DOC-X", status="active", body="body",
        validity=f"validity:\n  valid_until: {YESTERDAY}\n",
    )
    results = lint_bundle(kb)
    findings = results.get(kb / "expired.md", [])
    assert any(f.field == "validity" and "valid_until" in f.message for f in findings)
    assert all(f.severity != "error" for f in findings)


# ---------------------------------------------------------------------------
# Scenario 10: docs without validity behave exactly as before (regression).
# ---------------------------------------------------------------------------


def test_docs_without_validity_are_unaffected(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_search_fn(idx=idx, query="widget", today=TODAY)
    hit = next(h for h in resp.hits if h.id == "DOC-FRESH")
    assert hit.freshness == ""


def test_get_response_no_validity_when_absent(tmp_path: Path, tmp_index_path: Path) -> None:
    idx = _idx(tmp_path, tmp_index_path)
    resp = kb_get_fn(idx=idx, id="DOC-FRESH", today=TODAY)
    assert resp.validity is None
    assert resp.freshness == ""


# ---------------------------------------------------------------------------
# REST param threading (include_expired / validity_state).
# ---------------------------------------------------------------------------


def _validity_http_app(tmp_path: Path):
    from data_olympus.format.validate import today_iso

    # The REST path reads the REAL clock, so this fixture's validity dates
    # must shift with it or the assertions rot after the authoring date
    # (codex review blocker 1).
    kb = _build_kb(tmp_path, today=today_iso())
    app = build_app(
        kb_main_path=kb,
        kb_index_path=tmp_path / "idx.db",
        sync_interval_sec=60,
        staleness_degraded_sec=600,
        bootstrap_now=True,
    )
    return app.http_app()


@pytest.mark.asyncio
async def test_rest_include_expired_threads(tmp_path: Path) -> None:
    http_app = _validity_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        default_resp = await client.get("/api/v1/search", params={"q": "widget"})
        expired_resp = await client.get(
            "/api/v1/search", params={"q": "widget", "include_expired": "true"},
        )
    default_ids = {h["id"] for h in default_resp.json()["hits"]}
    expired_ids = {h["id"] for h in expired_resp.json()["hits"]}
    assert "DOC-EXPIRED" not in default_ids
    assert "DOC-EXPIRED" in expired_ids


@pytest.mark.asyncio
async def test_rest_validity_state_threads(tmp_path: Path) -> None:
    http_app = _validity_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/search", params={"q": "widget", "validity_state": "expired"},
        )
    ids = {h["id"] for h in resp.json()["hits"]}
    assert ids == {"DOC-EXPIRED"}


@pytest.mark.asyncio
async def test_rest_invalid_validity_state_is_400(tmp_path: Path) -> None:
    """A malformed validity_state is a client error, not an opaque 500
    (codex review blocker 2)."""
    http_app = _validity_http_app(tmp_path)
    transport = httpx.ASGITransport(app=http_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        bogus = await client.get(
            "/api/v1/search", params={"q": "widget", "validity_state": "bogus"},
        )
        bad_days = await client.get(
            "/api/v1/search",
            params={"q": "widget", "validity_state": "expiring_within:abc"},
        )
    assert bogus.status_code == 400
    assert bogus.json()["error"] == "bad_request"
    assert bad_days.status_code == 400
    assert bad_days.json()["error"] == "bad_request"


# ---------------------------------------------------------------------------
# Schema/version bookkeeping.
# ---------------------------------------------------------------------------


def test_index_schema_stores_validity_columns(tmp_path: Path, tmp_index_path: Path) -> None:
    import sqlite3

    _idx(tmp_path, tmp_index_path)
    conn = sqlite3.connect(tmp_index_path)
    row = conn.execute(
        "SELECT valid_from, valid_until, last_verified, recheck_by, "
        "verification_source FROM docs WHERE id = 'DOC-EXPIRED'"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[1] == YESTERDAY
