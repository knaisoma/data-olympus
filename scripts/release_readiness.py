#!/usr/bin/env python3
"""Release-readiness gate: pure, fail-closed evaluator over an evidence bundle.

Re-derives every hard-gate condition from raw typed records in the bundle. It
NEVER trusts a caller-supplied boolean like "ready": true or
"review_validated": true — those keys, if present, are ignored. Any
missing/None/unparseable field makes its condition False (fail-closed). A
malformed bundle never raises; it is simply NOT_READY with a blocker
explaining why.

CLI: `python3 scripts/release_readiness.py [--evidence <path>] [--json]`
Reads the evidence bundle as JSON from --evidence or stdin. Exit 0 iff ready,
else 1.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _is_sha40(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA40_RE.match(value))


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _is_exit_zero(value: Any) -> bool:
    """True only for the integer 0. Rejects False (== 0) and 0.0 truthiness traps."""
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _as_dict(value: Any) -> dict[str, Any]:
    """Coerce a possibly-missing/malformed field into a dict, never raising."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """Coerce a possibly-missing/malformed field into a list, never raising."""
    return value if isinstance(value, list) else []


@dataclass(frozen=True)
class GateResult:
    ready: bool
    conditions: dict[str, bool]
    blockers: list[str] = field(default_factory=list)
    report_md: str = ""


def _tickets_by_feature(bundle: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Index tickets by id; malformed ticket entries are dropped, not raised."""
    by_id: dict[str, list[dict[str, Any]]] = {}
    for raw in _as_list(bundle.get("tickets")):
        if not isinstance(raw, dict):
            continue
        tid = raw.get("id")
        if not isinstance(tid, str):
            continue
        by_id.setdefault(tid, []).append(raw)
    return by_id


def _check_manifest_complete(
    manifest: dict[str, Any], feature_ids: list[Any], by_feature: dict[str, list[dict[str, Any]]]
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    ok = True
    if not _non_empty_str(manifest.get("version")):
        ok = False
        blockers.append("manifest_complete: manifest.version missing or empty")
    if not _is_sha40(manifest.get("integration_sha")):
        ok = False
        blockers.append("manifest_complete: manifest.integration_sha missing or not 40-hex")
    if not feature_ids:
        ok = False
        blockers.append("manifest_complete: manifest.feature_ids missing or empty")
    if not _non_empty_str(manifest.get("contract_blob_sha")):
        ok = False
        blockers.append("manifest_complete: manifest.contract_blob_sha missing or empty")
    for fid in feature_ids:
        if not isinstance(fid, str):
            ok = False
            blockers.append(f"manifest_complete: feature_id {fid!r} is not a string")
            continue
        count = len(by_feature.get(fid, []))
        if count != 1:
            ok = False
            blockers.append(
                f"manifest_complete: feature {fid} appears {count} time(s) "
                "in tickets (expected exactly 1)"
            )
    return ok, blockers


def _check_every_feature_done(
    feature_ids: list[Any], by_feature: dict[str, list[dict[str, Any]]]
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    ok = True
    for fid in feature_ids:
        if not isinstance(fid, str):
            ok = False
            continue
        tickets = by_feature.get(fid, [])
        if len(tickets) != 1 or tickets[0].get("status") != "done":
            ok = False
            status = tickets[0].get("status") if len(tickets) == 1 else None
            blockers.append(
                f"every_feature_done: ticket {fid} status is {status!r}, expected 'done'"
            )
    return ok, blockers


def _check_review_validated(
    feature_ids: list[Any],
    by_feature: dict[str, list[dict[str, Any]]],
    security_classification: dict[str, Any],
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    ok = True
    for fid in feature_ids:
        if not isinstance(fid, str):
            ok = False
            continue
        tickets = by_feature.get(fid, [])
        if len(tickets) != 1:
            ok = False
            blockers.append(f"every_review_validated: {fid} has no unique ticket record")
            continue
        review = _as_dict(tickets[0].get("review_evidence"))
        if review.get("companion_review_present") is not True:
            ok = False
            blockers.append(
                f"every_review_validated: {fid} companion_review_present is not True"
            )
        if review.get("verdict") != "approved":
            ok = False
            blockers.append(
                f"every_review_validated: {fid} review verdict is "
                f"{review.get('verdict')!r}, expected 'approved'"
            )
        if not _is_sha40(review.get("reviewed_sha")):
            ok = False
            blockers.append(
                f"every_review_validated: {fid} review reviewed_sha missing or not 40-hex"
            )
        if review.get("acceptance_verified") is not True:
            ok = False
            blockers.append(f"every_review_validated: {fid} acceptance_verified is not True")
        # Fail-closed classification: any value that is not exactly "standard"
        # (including a missing/unknown classification) requires an approved
        # security_verdict. Absent classification defaults to high-risk.
        if (
            security_classification.get(fid) != "standard"
            and review.get("security_verdict") != "approved"
        ):
            ok = False
            blockers.append(
                f"every_review_validated: {fid} is security-classified "
                f"({security_classification.get(fid)!r} != 'standard') but security_verdict "
                f"is {review.get('security_verdict')!r}, expected 'approved'"
            )
    return ok, blockers


def _check_integration_review(
    integration_review: dict[str, Any], manifest: dict[str, Any]
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    ok = True
    if integration_review.get("approved") is not True:
        ok = False
        blockers.append("integration_review_approved: integration_review.approved is not True")
    if not _non_empty_str(integration_review.get("reviewer")):
        ok = False
        blockers.append(
            "integration_review_approved: integration_review.reviewer missing or empty"
        )
    if not _non_empty_str(integration_review.get("locked_revision_id")):
        ok = False
        blockers.append(
            "integration_review_approved: "
            "integration_review.locked_revision_id missing or empty"
        )
    reviewed_sha = integration_review.get("reviewed_sha")
    integration_sha = manifest.get("integration_sha")
    if not _is_sha40(reviewed_sha) or reviewed_sha != integration_sha:
        ok = False
        blockers.append(
            "integration_review_approved: integration_review.reviewed_sha "
            f"{reviewed_sha!r} does not match manifest.integration_sha {integration_sha!r}"
        )
    return ok, blockers


def _check_ci(ci: dict[str, Any], manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    ok = True
    if ci.get("found_any") is not True:
        ok = False
        blockers.append("ci_green_for_exact_sha: ci.found_any is not True")
    if ci.get("all_success") is not True:
        ok = False
        blockers.append("ci_green_for_exact_sha: ci.all_success is not True")
    integration_sha = manifest.get("integration_sha")
    if ci.get("sha") != integration_sha:
        ok = False
        blockers.append(
            f"ci_green_for_exact_sha: ci.sha {ci.get('sha')!r} does not match "
            f"manifest.integration_sha {integration_sha!r}"
        )
    missing_required = ci.get("missing_required")
    if missing_required != []:
        ok = False
        blockers.append(
            f"ci_green_for_exact_sha: ci.missing_required is {missing_required!r}, expected []"
        )
    return ok, blockers


def _check_rc_digest(
    deployed_digest: dict[str, Any], expected_rc_digest: Any
) -> tuple[bool, list[str]]:
    digest = deployed_digest.get("digest")
    if (
        _non_empty_str(digest)
        and _non_empty_str(expected_rc_digest)
        and digest == expected_rc_digest
    ):
        return True, []
    return False, [
        f"rc_digest_deployed: deployed_digest.digest {digest!r} does not match "
        f"expected_rc_digest {expected_rc_digest!r}"
    ]


def _check_verify(verify: dict[str, Any]) -> tuple[bool, list[str]]:
    # MVP: verify_passed requires a strict integer-0 exit code. The served-digest
    # identity check is deliberately NOT done here (it would be fail-open on a
    # missing digest_served, which the MVP verify cannot yet produce); the
    # deployed-vs-expected digest binding is enforced by rc_digest_deployed. The
    # authenticated served-digest identity is hardening-ledger item 2.
    if _is_exit_zero(verify.get("exit_code")):
        return True, []
    return False, [
        f"verify_passed: verify.exit_code is {verify.get('exit_code')!r}, expected integer 0"
    ]


def _check_version_free(version_free: dict[str, Any]) -> tuple[bool, list[str]]:
    if version_free.get("free") is True:
        return True, []
    return False, ["version_unpublished: version_free.free is not True"]


def _check_security(security: dict[str, Any]) -> tuple[bool, list[str]]:
    if _is_exit_zero(security.get("exit_code")):
        return True, []
    return False, [
        f"security_clear: security.exit_code is {security.get('exit_code')!r}, expected integer 0"
    ]


def _check_no_open_blockers(bundle: dict[str, Any]) -> tuple[bool, list[str]]:
    # Fail-closed: a MISSING open_blockers key is NOT ready (it means the field
    # was never populated, not that there are no blockers). Only a top-level
    # key that is explicitly present and equal to [] counts as clear.
    if "open_blockers" not in bundle:
        return False, ["no_open_blockers: open_blockers key is missing from evidence bundle"]
    open_blockers = bundle["open_blockers"]
    if open_blockers == []:
        return True, []
    return False, [f"no_open_blockers: open_blockers is {open_blockers!r}, expected []"]


def evaluate(bundle: dict[str, Any]) -> GateResult:
    """Derive every hard-gate condition from raw records in `bundle`.

    Pure function: never raises on missing/malformed input, never trusts any
    caller-supplied "ready"/"validated"-style boolean shortcut.
    """
    if not isinstance(bundle, dict):
        bundle = {}

    manifest = _as_dict(bundle.get("manifest"))
    feature_ids_raw = manifest.get("feature_ids")
    feature_ids = _as_list(feature_ids_raw)
    by_feature = _tickets_by_feature(bundle)
    security_classification = _as_dict(manifest.get("security_classification"))

    conditions: dict[str, bool] = {}
    blockers: list[str] = []

    ok, bl = _check_manifest_complete(manifest, feature_ids, by_feature)
    conditions["manifest_complete"] = ok
    blockers += bl

    ok, bl = _check_every_feature_done(feature_ids, by_feature)
    conditions["every_feature_done"] = ok
    blockers += bl

    ok, bl = _check_review_validated(feature_ids, by_feature, security_classification)
    conditions["every_review_validated"] = ok
    blockers += bl

    ok, bl = _check_integration_review(_as_dict(bundle.get("integration_review")), manifest)
    conditions["integration_review_approved"] = ok
    blockers += bl

    ok, bl = _check_ci(_as_dict(bundle.get("ci")), manifest)
    conditions["ci_green_for_exact_sha"] = ok
    blockers += bl

    expected_rc_digest = bundle.get("expected_rc_digest")
    ok, bl = _check_rc_digest(_as_dict(bundle.get("deployed_digest")), expected_rc_digest)
    conditions["rc_digest_deployed"] = ok
    blockers += bl

    ok, bl = _check_verify(_as_dict(bundle.get("verify")))
    conditions["verify_passed"] = ok
    blockers += bl

    ok, bl = _check_version_free(_as_dict(bundle.get("version_free")))
    conditions["version_unpublished"] = ok
    blockers += bl

    ok, bl = _check_security(_as_dict(bundle.get("security")))
    conditions["security_clear"] = ok
    blockers += bl

    ok, bl = _check_no_open_blockers(bundle)
    conditions["no_open_blockers"] = ok
    blockers += bl

    ready = all(conditions.values())
    report_md = _render_report(manifest, conditions, blockers, ready)
    return GateResult(ready=ready, conditions=conditions, blockers=blockers, report_md=report_md)


def _render_report(
    manifest: dict[str, Any], conditions: dict[str, bool], blockers: list[str], ready: bool
) -> str:
    version = manifest.get("version", "?")
    integration_sha = manifest.get("integration_sha", "?")
    lines = [
        "# Release readiness report",
        "",
        f"- version: {version}",
        f"- integration_sha: {integration_sha}",
        "",
        "## Conditions",
        "",
    ]
    for name, passed in conditions.items():
        lines.append(f"- {name}: {'PASS' if passed else 'FAIL'}")
    lines.append("")
    lines.append("## Blockers")
    lines.append("")
    if blockers:
        for b in blockers:
            lines.append(f"- {b}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("READY" if ready else "NOT_READY")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release_readiness")
    parser.add_argument("--evidence", help="path to evidence bundle JSON (default: stdin)")
    parser.add_argument(
        "--json", action="store_true", help="print GateResult as JSON instead of report_md"
    )
    args = parser.parse_args(argv)

    try:
        if args.evidence:
            with open(args.evidence, encoding="utf-8") as fh:
                text = fh.read()
        else:
            text = sys.stdin.read()
    except OSError as exc:
        # Fail-closed: an unreadable evidence source is NOT_READY, never a crash.
        result = GateResult(
            ready=False,
            conditions={},
            blockers=[f"could not read evidence source: {exc}"],
            report_md=(
                "# Release readiness report\n\n"
                f"could not read evidence source: {exc}\n\nNOT_READY"
            ),
        )
        if args.json:
            print(json.dumps(asdict(result), indent=2))
        else:
            print(result.report_md)
        return 1

    try:
        bundle = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as exc:
        result = GateResult(
            ready=False,
            conditions={},
            blockers=[f"evidence bundle is not valid JSON: {exc}"],
            report_md=(
                "# Release readiness report\n\n"
                f"evidence bundle is not valid JSON: {exc}\n\nNOT_READY"
            ),
        )
    else:
        result = evaluate(bundle)

    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print(result.report_md)
    return 0 if result.ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
