#!/usr/bin/env python3
"""Fail closed stage decisions for the Data Olympus release outcome."""

from __future__ import annotations

import json
import os
import re
import sys
from hashlib import sha256
from typing import Any

DEFAULT_EXTRA_CONTEXT = "No extra context for this run"

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_CANDIDATE_TAG = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-rc\.([1-9]\d*)$")
_ALLOWED_STAGES = {
    "authority",
    "admission",
    "prepare",
    "validate",
    "review",
    "deliver",
    "verify",
    "notify",
    "rollback",
}

StageResult = dict[str, Any]


class ReleaseInputError(ValueError):
    """Raised when a release stage input is missing or inconsistent."""


def _result(
    status: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
) -> StageResult:
    return {
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
        "outputs": outputs or {},
    }


def _object(value: Any, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ReleaseInputError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ReleaseInputError(f"{name} must be a nonempty string")
    return value


def _sha40(value: Any, name: str) -> str:
    text = _string(value, name)
    if _SHA40.fullmatch(text) is None:
        raise ReleaseInputError(f"{name} must be a 40 character git SHA")
    return text


def _revision(value: Any, name: str) -> str:
    text = _string(value, name)
    if _HEX_REVISION.fullmatch(text) is None:
        raise ReleaseInputError(f"{name} must be a 40 or 64 character hexadecimal revision")
    return text


def _hash(value: Any, name: str) -> str:
    text = _string(value, name)
    if _SHA256.fullmatch(text) is None:
        raise ReleaseInputError(f"{name} must be a SHA256 value")
    return text


def _digest(value: Any, name: str) -> str:
    text = _string(value, name)
    if _DIGEST.fullmatch(text) is None:
        raise ReleaseInputError(f"{name} must be an OCI SHA256 digest")
    return text


def _version(value: Any, name: str) -> str:
    text = _string(value, name)
    if _VERSION.fullmatch(text) is None:
        raise ReleaseInputError(f"{name} must be a semantic version")
    return text


def _integer(value: Any, name: str) -> int:
    if type(value) is not int:
        raise ReleaseInputError(f"{name} must be an integer")
    return value


def _true(value: Any, name: str) -> None:
    if value is not True:
        raise ReleaseInputError(f"{name} must be true")


def _exit_zero(value: Any, name: str) -> None:
    if type(value) is not int or value != 0:
        raise ReleaseInputError(f"{name} must be integer zero")


def _version_tuple(value: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _same_source(
    expected: str,
    value: Any,
    name: str,
) -> None:
    observed = _sha40(value, name)
    if observed != expected:
        raise ReleaseInputError(f"{name} does not match the candidate source")


def _candidate(
    value: Any,
    *,
    require_artifact: bool,
) -> tuple[dict[str, Any], str, str]:
    candidate = _object(value, "candidate")
    version = _version(candidate.get("version"), "candidate.version")
    if version == "0.6.0":
        raise ReleaseInputError("v0.6.0 is immutable and cannot be a candidate")
    source_revision = _sha40(
        candidate.get("source_revision"),
        "candidate.source_revision",
    )
    if require_artifact:
        candidate_tag = _string(
            candidate.get("candidate_tag"),
            "candidate.candidate_tag",
        )
        match = _CANDIDATE_TAG.fullmatch(candidate_tag)
        if match is None or candidate_tag.split("-rc.", 1)[0] != version:
            raise ReleaseInputError("candidate.candidate_tag does not match candidate.version")
        _digest(candidate.get("image_digest"), "candidate.image_digest")
    return candidate, version, source_revision


def _extra_context(value: Any) -> dict[str, Any]:
    text = (
        _string(value, "extra_context")
        .replace("\r\n", "\n")
        .replace(
            "\r",
            "\n",
        )
        .strip()
    )
    return {
        "default": text == DEFAULT_EXTRA_CONTEXT,
        "length": len(text),
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
    }


def _authority(input_document: dict[str, Any]) -> StageResult:
    authority_revision = _sha40(
        input_document.get("authority_revision"),
        "authority_revision",
    )
    contract_revision = _revision(
        input_document.get("contract_revision"),
        "contract_revision",
    )
    source_revision = _sha40(
        input_document.get("source_revision"),
        "source_revision",
    )
    evidence = {
        "authority_revision": authority_revision,
        "contract_revision": contract_revision,
        "source_revision": source_revision,
    }
    return _result(
        "pass",
        "authority, release contract, and source revisions are bound",
        evidence,
    )


def _admission(input_document: dict[str, Any]) -> StageResult:
    branch = _string(input_document.get("branch"), "branch")
    if branch != "main":
        raise ReleaseInputError("release admission requires the main branch")
    source_revision = _sha40(
        input_document.get("source_revision"),
        "source_revision",
    )
    remote_main_revision = _sha40(
        input_document.get("remote_main_revision"),
        "remote_main_revision",
    )
    if source_revision != remote_main_revision:
        raise ReleaseInputError("candidate source does not match remote main")

    computed = _object(
        input_document.get("computed_release"),
        "computed_release",
    )
    releasable = computed.get("releasable")
    if type(releasable) is not bool:
        raise ReleaseInputError("computed_release.releasable must be boolean")
    bump = _string(computed.get("bump"), "computed_release.bump")
    current = _version(
        computed.get("current_version"),
        "computed_release.current_version",
    )
    next_version = _version(
        computed.get("next_version"),
        "computed_release.next_version",
    )
    changes = _object(
        computed.get("changes"),
        "computed_release.changes",
    )

    evidence = {
        "source_revision": source_revision,
        "current_version": current,
        "computed_release": {
            "releasable": releasable,
            "bump": bump,
            "next_version": next_version,
            "functional_changed": computed.get("functional_changed"),
            "changes": changes,
        },
    }
    if not releasable:
        if bump != "none" or next_version != current:
            raise ReleaseInputError("nonreleasable computation is internally inconsistent")
        return _result(
            "no_action",
            "no merged unreleased work exists on remote main",
            evidence,
        )

    if bump not in {"patch", "minor"}:
        raise ReleaseInputError("releasable computation must use patch or minor bump")
    if next_version == "0.6.0":
        raise ReleaseInputError("v0.6.0 is immutable; the next release must move forward")
    if _version_tuple(next_version) <= _version_tuple(current):
        raise ReleaseInputError("computed release does not move beyond the current version")
    return _result(
        "pass",
        f"release {next_version} is admitted from remote main",
        evidence,
        {
            "candidate": {
                "version": next_version,
                "source_revision": source_revision,
                "bump": bump,
            }
        },
    )


def _prepare(input_document: dict[str, Any]) -> StageResult:
    _, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=False,
    )
    context = _extra_context(input_document.get("extra_context"))

    changelog = _object(input_document.get("changelog"), "changelog")
    _same_source(
        source_revision,
        changelog.get("source_revision"),
        "changelog.source_revision",
    )
    changelog_hash = _hash(
        changelog.get("content_hash"),
        "changelog.content_hash",
    )

    security = _object(input_document.get("security"), "security")
    _exit_zero(security.get("exit_code"), "security.exit_code")
    security_hash = _hash(
        security.get("report_hash"),
        "security.report_hash",
    )

    tests = _object(input_document.get("tests"), "tests")
    _same_source(
        source_revision,
        tests.get("source_revision"),
        "tests.source_revision",
    )
    _true(tests.get("passed"), "tests.passed")
    tests_hash = _hash(
        tests.get("evidence_hash"),
        "tests.evidence_hash",
    )

    rollback = _object(
        input_document.get("rollback_point"),
        "rollback_point",
    )
    rollback_digest = _digest(
        rollback.get("digest"),
        "rollback_point.digest",
    )
    image = _string(rollback.get("image"), "rollback_point.image")
    if not image.endswith(rollback_digest):
        raise ReleaseInputError("rollback_point.image does not match rollback_point.digest")
    if rollback.get("keel_policy") != "never":
        raise ReleaseInputError("rollback_point.keel_policy must be never")

    issue = _object(
        input_document.get("release_issue"),
        "release_issue",
    )
    if _integer(issue.get("count"), "release_issue.count") != 1:
        raise ReleaseInputError("exactly one private release issue is required")
    _true(issue.get("private"), "release_issue.private")
    number = _integer(issue.get("number"), "release_issue.number")
    if number <= 0:
        raise ReleaseInputError("release_issue.number must be positive")
    url = _string(issue.get("url"), "release_issue.url")
    if not url.startswith("https://github.com/knaisoma/data-olympus/issues/"):
        raise ReleaseInputError("release_issue.url is outside the repository")
    _same_source(
        source_revision,
        issue.get("source_revision"),
        "release_issue.source_revision",
    )
    if issue.get("candidate_version") != version:
        raise ReleaseInputError("release_issue.candidate_version does not match the candidate")
    issue_hash = _hash(
        issue.get("body_hash"),
        "release_issue.body_hash",
    )

    return _result(
        "pass",
        f"private release issue {number} records candidate {version}",
        {
            "source_revision": source_revision,
            "version": version,
            "extra_context": context,
            "changelog_hash": changelog_hash,
            "security_hash": security_hash,
            "tests_hash": tests_hash,
            "rollback_digest": rollback_digest,
            "release_issue_hash": issue_hash,
        },
        {
            "release_issue_number": number,
            "release_issue_url": url,
        },
    )


def _validate(input_document: dict[str, Any]) -> StageResult:
    _, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=True,
    )
    _same_source(
        source_revision,
        input_document.get("current_source_revision"),
        "current_source_revision",
    )

    ci = _object(input_document.get("ci"), "ci")
    _same_source(
        source_revision,
        ci.get("source_revision"),
        "ci.source_revision",
    )
    _true(ci.get("all_success"), "ci.all_success")
    if ci.get("missing_required") != []:
        raise ReleaseInputError("ci.missing_required must be empty")

    security = _object(input_document.get("security"), "security")
    _exit_zero(security.get("exit_code"), "security.exit_code")

    version_free = _object(
        input_document.get("version_free"),
        "version_free",
    )
    if version_free.get("version") != version:
        raise ReleaseInputError("version_free.version does not match the candidate")
    _true(version_free.get("free"), "version_free.free")

    tests = _object(input_document.get("tests"), "tests")
    _same_source(
        source_revision,
        tests.get("source_revision"),
        "tests.source_revision",
    )
    _true(tests.get("passed"), "tests.passed")

    return _result(
        "pass",
        f"candidate {version} is unchanged and every gate passed",
        {
            "source_revision": source_revision,
            "version": version,
            "ci_green": True,
            "security_clear": True,
            "version_free": True,
            "tests_passed": True,
        },
    )


def _review(input_document: dict[str, Any]) -> StageResult:
    _, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=True,
    )
    _same_source(
        source_revision,
        input_document.get("current_source_revision"),
        "current_source_revision",
    )

    executor = _object(input_document.get("executor"), "executor")
    executor_family = _string(executor.get("family"), "executor.family")
    if executor_family not in {"claude", "codex"}:
        raise ReleaseInputError("executor.family must be claude or codex for a red release")
    _same_source(
        source_revision,
        executor.get("source_revision"),
        "executor.source_revision",
    )

    review = _object(
        input_document.get("companion_review"),
        "companion_review",
    )
    reviewer_family = _string(
        review.get("family"),
        "companion_review.family",
    )
    if {executor_family, reviewer_family} != {"claude", "codex"}:
        raise ReleaseInputError("release execution and review must cross Claude and Codex families")
    if review.get("verdict") != "APPROVE":
        raise ReleaseInputError("companion_review.verdict must be APPROVE")
    _same_source(
        source_revision,
        review.get("reviewed_source_revision"),
        "companion_review.reviewed_source_revision",
    )
    review_hash = _hash(
        review.get("evidence_hash"),
        "companion_review.evidence_hash",
    )

    approval = _object(
        input_document.get("operator_approval"),
        "operator_approval",
    )
    _true(approval.get("approved"), "operator_approval.approved")
    _same_source(
        source_revision,
        approval.get("source_revision"),
        "operator_approval.source_revision",
    )
    approval_id = _string(
        approval.get("approval_id"),
        "operator_approval.approval_id",
    )

    return _result(
        "pass",
        f"candidate {version} has crossed review and SHA bound approval",
        {
            "source_revision": source_revision,
            "executor_family": executor_family,
            "reviewer_family": reviewer_family,
            "review_hash": review_hash,
            "operator_approval_id": approval_id,
        },
    )


def _deliver(input_document: dict[str, Any]) -> StageResult:
    candidate, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=True,
    )
    _same_source(
        source_revision,
        input_document.get("current_source_revision"),
        "current_source_revision",
    )
    candidate_tag = candidate["candidate_tag"]

    workflows = input_document.get("workflows")
    if type(workflows) is not list:
        raise ReleaseInputError("workflows must be an array")
    by_name: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(workflows):
        workflow = _object(raw, f"workflows[{index}]")
        name = _string(workflow.get("name"), f"workflows[{index}].name")
        if name in by_name:
            raise ReleaseInputError(f"duplicate workflow receipt: {name}")
        by_name[name] = workflow
    required = {"rc-publish.yml", "tag-release.yml", "set-channel.yml"}
    if set(by_name) != required:
        raise ReleaseInputError(
            "delivery must use only rc-publish.yml, tag-release.yml, and set-channel.yml"
        )
    for name, workflow in by_name.items():
        if workflow.get("conclusion") != "success":
            raise ReleaseInputError(f"{name} did not conclude successfully")
        _same_source(
            source_revision,
            workflow.get("source_revision"),
            f"{name}.source_revision",
        )
    if by_name["rc-publish.yml"].get("candidate_tag") != candidate_tag:
        raise ReleaseInputError("rc-publish.yml candidate tag does not match")
    if by_name["tag-release.yml"].get("candidate_tag") != candidate_tag:
        raise ReleaseInputError("tag-release.yml candidate tag does not match")
    if by_name["set-channel.yml"].get("source_tag") != f"v{version}":
        raise ReleaseInputError("set-channel.yml source tag does not match stable version")

    canary = _object(input_document.get("canary"), "canary")
    if canary.get("candidate_tag") != candidate_tag:
        raise ReleaseInputError("canary.candidate_tag does not match")
    _same_source(
        source_revision,
        canary.get("source_revision"),
        "canary.source_revision",
    )
    canary_digest = _digest(canary.get("digest"), "canary.digest")
    if canary_digest != candidate["image_digest"]:
        raise ReleaseInputError("canary.digest does not match the candidate")
    if canary.get("keel_policy") != "never":
        raise ReleaseInputError("canary.keel_policy must remain never")
    for field in (
        "rollout_complete",
        "healthy",
        "ready",
        "mcp_search",
        "enforcement",
    ):
        _true(canary.get(field), f"canary.{field}")

    deployment = _object(
        input_document.get("deployment"),
        "deployment",
    )
    if deployment.get("keel_policy") != "never":
        raise ReleaseInputError("deployment.keel_policy must remain never")
    _same_source(
        source_revision,
        deployment.get("source_revision"),
        "deployment.source_revision",
    )
    digest = _digest(
        deployment.get("image_digest"),
        "deployment.image_digest",
    )
    if digest != candidate["image_digest"]:
        raise ReleaseInputError("deployment.image_digest does not match the candidate")
    _true(
        deployment.get("rollout_complete"),
        "deployment.rollout_complete",
    )

    return _result(
        "pass",
        f"release {version} was delivered by the approved workflows",
        {
            "source_revision": source_revision,
            "candidate_tag": candidate_tag,
            "digest": digest,
            "workflows": sorted(required),
        },
        {"published_version": version},
    )


def _verify(input_document: dict[str, Any]) -> StageResult:
    candidate, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=True,
    )
    digest = candidate["image_digest"]

    github_release = _object(
        input_document.get("github_release"),
        "github_release",
    )
    if github_release.get("tag") != f"v{version}":
        raise ReleaseInputError("github_release.tag does not match")
    _same_source(
        source_revision,
        github_release.get("source_revision"),
        "github_release.source_revision",
    )

    pypi = _object(input_document.get("pypi"), "pypi")
    if pypi.get("version") != version:
        raise ReleaseInputError("pypi.version does not match")
    _same_source(
        source_revision,
        pypi.get("provenance_source_revision"),
        "pypi.provenance_source_revision",
    )

    image = _object(input_document.get("image"), "image")
    if image.get("tag") != f"v{version}":
        raise ReleaseInputError("image.tag does not match")
    if _digest(image.get("digest"), "image.digest") != digest:
        raise ReleaseInputError("image.digest does not match")

    deployment = _object(
        input_document.get("deployment"),
        "deployment",
    )
    _same_source(
        source_revision,
        deployment.get("source_revision"),
        "deployment.source_revision",
    )
    if _digest(deployment.get("digest"), "deployment.digest") != digest:
        raise ReleaseInputError("deployment.digest does not match")
    for field in (
        "healthy",
        "ready",
        "mcp_search",
        "enforcement",
        "documentation",
    ):
        _true(deployment.get(field), f"deployment.{field}")

    return _result(
        "pass",
        f"release {version} and kn dev match the reviewed source",
        {
            "source_revision": source_revision,
            "version": version,
            "digest": digest,
            "health": "verified",
            "mcp": "verified",
            "documentation": "verified",
        },
    )


def _notify(input_document: dict[str, Any]) -> StageResult:
    destination = _string(
        input_document.get("destination"),
        "destination",
    )
    readback_destination = _string(
        input_document.get("readback_destination"),
        "readback_destination",
    )
    if readback_destination != destination:
        raise ReleaseInputError("notification destination does not match")
    sent_id = _string(
        input_document.get("sent_message_id"),
        "sent_message_id",
    )
    readback_id = _string(
        input_document.get("readback_message_id"),
        "readback_message_id",
    )
    if sent_id != readback_id:
        raise ReleaseInputError("notification message identifier does not match")
    sent_hash = _hash(
        input_document.get("sent_content_hash"),
        "sent_content_hash",
    )
    readback_hash = _hash(
        input_document.get("readback_content_hash"),
        "readback_content_hash",
    )
    if sent_hash != readback_hash:
        raise ReleaseInputError("notification content does not match")
    return _result(
        "pass",
        "release notification was sent and read back exactly",
        {
            "destination": destination,
            "content_hash": sent_hash,
        },
        {"message_id": sent_id},
    )


def _rollback(input_document: dict[str, Any]) -> StageResult:
    failed_digest = _digest(
        input_document.get("failed_digest"),
        "failed_digest",
    )
    rollback = _object(
        input_document.get("rollback_point"),
        "rollback_point",
    )
    restored_digest = _digest(
        rollback.get("digest"),
        "rollback_point.digest",
    )
    if restored_digest == failed_digest:
        raise ReleaseInputError("rollback point must differ from the failed digest")
    image = _string(rollback.get("image"), "rollback_point.image")
    if not image.endswith(restored_digest):
        raise ReleaseInputError("rollback_point.image does not match rollback_point.digest")
    intended_policy = _string(
        rollback.get("intended_keel_policy"),
        "rollback_point.intended_keel_policy",
    )
    if intended_policy != "never":
        raise ReleaseInputError("rollback_point.intended_keel_policy must be never")

    events = input_document.get("events")
    if type(events) is not list:
        raise ReleaseInputError("events must be an array")
    expected_names = [
        "keel-paused",
        "digest-restored",
        "deployment-verified",
        "keel-policy-restored",
    ]
    names = [
        _string(_object(event, "rollback event").get("name"), "event.name") for event in events
    ]
    if names != expected_names:
        raise ReleaseInputError("rollback events are missing or out of order")

    paused, restored, verified, policy_restored = [
        _object(event, f"events[{index}]") for index, event in enumerate(events)
    ]
    if paused.get("policy") != "never":
        raise ReleaseInputError("Keel was not paused before rollback")
    if _digest(restored.get("digest"), "digest-restored.digest") != (restored_digest):
        raise ReleaseInputError("restored digest does not match rollback point")
    containers = restored.get("containers")
    if type(containers) is not list or set(containers) != {
        "data-olympus-mcp",
        "prepare-git",
    }:
        raise ReleaseInputError("both Data Olympus containers must restore the exact digest")
    if _digest(verified.get("digest"), "deployment-verified.digest") != (restored_digest):
        raise ReleaseInputError("verified digest does not match rollback point")
    _true(verified.get("healthy"), "deployment-verified.healthy")
    _true(verified.get("ready"), "deployment-verified.ready")
    if policy_restored.get("policy") != intended_policy:
        raise ReleaseInputError("Keel policy was not restored to the intended value")

    return _result(
        "pass",
        "previous exact digest was restored and verified while Keel stayed paused",
        {
            "failed_digest": failed_digest,
            "restored_digest": restored_digest,
            "keel_policy": intended_policy,
        },
    )


_EVALUATORS = {
    "authority": _authority,
    "admission": _admission,
    "prepare": _prepare,
    "validate": _validate,
    "review": _review,
    "deliver": _deliver,
    "verify": _verify,
    "notify": _notify,
    "rollback": _rollback,
}


def evaluate_stage(stage: str, input_document: Any) -> StageResult:
    """Evaluate one release lifecycle stage without trusting shortcut booleans."""
    if stage not in _ALLOWED_STAGES:
        return _result("failed", f"unknown release stage: {stage}")
    try:
        value = _object(input_document, "stage input")
        return _EVALUATORS[stage](value)
    except ReleaseInputError as error:
        status = (
            "blocked"
            if stage in {"authority", "admission", "prepare", "validate", "review"}
            else "failed"
        )
        return _result(status, str(error))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        output = _result("failed", "exactly one release stage is required")
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 2
    stage = arguments[0]
    raw_input = os.environ.get("AI_OPERATIONS_STAGE_INPUT")
    if raw_input is None:
        output = _result(
            "failed",
            "AI_OPERATIONS_STAGE_INPUT is required",
        )
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 2
    try:
        input_document = json.loads(raw_input)
    except json.JSONDecodeError:
        output = _result(
            "failed",
            "AI_OPERATIONS_STAGE_INPUT must be valid JSON",
        )
        print(json.dumps(output, separators=(",", ":"), sort_keys=True))
        return 2
    output = evaluate_stage(stage, input_document)
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 0 if output["status"] in {"pass", "no_action"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
