#!/usr/bin/env python3
"""Fail closed stage decisions for the Data Olympus release outcome."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _expected_next_version(current: str, bump: str) -> str:
    major, minor, patch = _version_tuple(current)
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    raise ReleaseInputError("releasable computation must use patch or minor bump")


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

    if next_version == "0.6.0":
        raise ReleaseInputError("v0.6.0 is immutable; the next release must move forward")
    expected_next = _expected_next_version(current, bump)
    if next_version != expected_next:
        raise ReleaseInputError("computed_release.next_version does not match its bump")
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

    release_pr = _object(
        input_document.get("release_pr"),
        "release_pr",
    )
    number = _integer(release_pr.get("number"), "release_pr.number")
    if number <= 0:
        raise ReleaseInputError("release_pr.number must be positive")
    url = _string(release_pr.get("url"), "release_pr.url")
    if url != f"https://github.com/knaisoma/data-olympus/pull/{number}":
        raise ReleaseInputError("release_pr.url is outside the Data Olympus repository")
    _true(release_pr.get("merged"), "release_pr.merged")
    base_revision = _sha40(
        release_pr.get("base_source_revision"),
        "release_pr.base_source_revision",
    )
    head_revision = _sha40(
        release_pr.get("head_revision"),
        "release_pr.head_revision",
    )
    if base_revision == head_revision or head_revision == source_revision:
        raise ReleaseInputError("release_pr revisions do not prove a squash merge")
    _same_source(
        source_revision,
        release_pr.get("source_revision"),
        "release_pr.source_revision",
    )
    if release_pr.get("candidate_version") != version:
        raise ReleaseInputError("release_pr.candidate_version does not match the candidate")

    return _result(
        "pass",
        f"release pull request {number} produced candidate {version}",
        {
            "source_revision": source_revision,
            "version": version,
            "extra_context": context,
            "changelog_hash": changelog_hash,
            "security_hash": security_hash,
            "tests_hash": tests_hash,
            "rollback_digest": rollback_digest,
            "release_pr_head_revision": head_revision,
            "release_pr_base_revision": base_revision,
        },
        {
            "release_pr_number": number,
            "release_pr_url": url,
        },
    )


def _validate(input_document: dict[str, Any]) -> StageResult:
    _, version, source_revision = _candidate(
        input_document.get("candidate"),
        require_artifact=False,
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
        require_artifact=False,
    )
    _same_source(
        source_revision,
        input_document.get("current_source_revision"),
        "current_source_revision",
    )

    executor = _object(input_document.get("executor"), "executor")
    executor_family = _string(executor.get("family"), "executor.family")
    if executor_family != "deterministic":
        raise ReleaseInputError("executor.family must be deterministic")
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
    if reviewer_family != "claude":
        raise ReleaseInputError("release review must use Claude")
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

    review_model_use_id = _integer(
        input_document.get("review_model_use_id"),
        "review_model_use_id",
    )
    if review_model_use_id <= 0:
        raise ReleaseInputError("review_model_use_id must be positive")

    approval = _object(
        input_document.get("candidate_approval"),
        "candidate_approval",
    )
    authority = _string(
        approval.get("authority"),
        "candidate_approval.authority",
    )
    if authority not in {"operator", "standing-delegation"}:
        raise ReleaseInputError("candidate_approval.authority is invalid")
    _same_source(
        source_revision,
        approval.get("candidate_revision"),
        "candidate_approval.candidate_revision",
    )
    contract_digest = _hash(
        approval.get("contract_digest"),
        "candidate_approval.contract_digest",
    )
    if contract_digest != input_document.get("contract_revision"):
        raise ReleaseInputError("candidate approval contract digest changed")
    approval_hash = _hash(
        approval.get("approval_id_hash"),
        "candidate_approval.approval_id_hash",
    )
    if approval.get("review_model_use_id") != review_model_use_id:
        raise ReleaseInputError("candidate approval review evidence changed")
    if approval.get("run_id") != input_document.get("run_id"):
        raise ReleaseInputError("candidate approval run changed")

    return _result(
        "pass",
        f"candidate {version} has independent review and SHA bound approval",
        {
            "source_revision": source_revision,
            "executor_family": executor_family,
            "reviewer_family": reviewer_family,
            "review_hash": review_hash,
            "approval_authority": authority,
            "approval_id_hash": approval_hash,
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

_RUN_INPUT_FIELDS = {
    "authority_revision",
    "contract_id",
    "contract_revision",
    "executor_capability",
    "extra_context",
    "model_request",
    "notification",
    "outcome",
    "project",
    "reviewer_capability",
    "run_id",
    "source_revision",
}
_MODEL_REQUEST_FIELDS = {
    "command",
    "control_environment",
    "ticket_environment",
}
_ISSUED_TICKET_FIELDS = {
    "model_use_id",
    "purpose",
    "route_id",
    "source_revision",
    "ticket",
}
_MODEL_RESPONSE_FIELDS = {
    "model_use_id",
    "route_id",
    "status",
    "verdict",
}
_CANDIDATE_APPROVAL_FIELDS = {
    "approval_id_hash",
    "authority",
    "candidate_revision",
    "contract_digest",
    "review_model_use_id",
    "run_id",
}
_EXPECTED_OUTCOME = (
    "One immutable reviewed release is published and every channel and "
    "deployment matches its source revision."
)


def _exact_fields(value: dict[str, Any], fields: set[str], name: str) -> None:
    unknown = sorted(set(value) - fields)
    missing = sorted(fields - set(value))
    if unknown:
        raise ReleaseInputError(f"unknown {name} field: {unknown[0]}")
    if missing:
        raise ReleaseInputError(f"missing {name} field: {missing[0]}")


def parse_release_run_input(raw_input: str) -> dict[str, Any]:
    """Parse the exact bounded input emitted by the central runner."""
    try:
        parsed = json.loads(raw_input)
    except json.JSONDecodeError as error:
        raise ReleaseInputError("AI_OPERATIONS_RUN_INPUT must be valid JSON") from error
    value = _object(parsed, "run input")
    _exact_fields(value, _RUN_INPUT_FIELDS, "run input")
    if value["contract_id"] != "data-olympus-release":
        raise ReleaseInputError("contract identifier does not match Data Olympus release")
    if value["project"] != "data-olympus" or value["outcome"] != _EXPECTED_OUTCOME:
        raise ReleaseInputError("run input does not match the Data Olympus release outcome")
    if value["executor_capability"] != "deterministic-release-execution":
        raise ReleaseInputError("executor capability does not match Data Olympus release")
    if value["reviewer_capability"] != "high-risk-review":
        raise ReleaseInputError("reviewer capability does not match Data Olympus release")
    _sha40(value["authority_revision"], "authority_revision")
    _sha40(value["source_revision"], "source_revision")
    _hash(value["contract_revision"], "contract_revision")
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        _string(value["run_id"], "run_id"),
    ) is None:
        raise ReleaseInputError("run_id must be a UUID")
    _extra_context(value["extra_context"])

    notification = _object(value["notification"], "notification")
    _exact_fields(
        notification,
        {"confirmation", "destination", "transport"},
        "notification",
    )
    if notification != {
        "transport": "telegram",
        "destination": "data-olympus-operations",
        "confirmation": "send_and_readback",
    }:
        raise ReleaseInputError("notification does not match Data Olympus operations")

    model_request = _object(value["model_request"], "model_request")
    _exact_fields(model_request, _MODEL_REQUEST_FIELDS, "model_request")
    command = model_request["command"]
    if (
        type(command) is not list
        or len(command) < 2
        or any(type(item) is not str or not item.strip() for item in command)
        or command[-2:] != ["models", "request"]
        or model_request["control_environment"] != "AI_OPERATIONS_RUN_CONTROL"
        or model_request["ticket_environment"] != "AI_OPERATIONS_MODEL_TICKET"
    ):
        raise ReleaseInputError("model_request does not match the runner boundary")
    if not Path(command[0]).is_absolute():
        raise ReleaseInputError("model request executable must be absolute")
    return value


def _issued_model_ticket(value: Any) -> dict[str, Any]:
    ticket = _object(value, "model ticket")
    _exact_fields(ticket, _ISSUED_TICKET_FIELDS, "model ticket")
    if type(ticket["model_use_id"]) is not int or ticket["model_use_id"] < 1:
        raise ReleaseInputError("model ticket identifier must be a positive integer")
    if ticket["purpose"] != "review":
        raise ReleaseInputError("release model ticket purpose must be review")
    route_id = _string(ticket["route_id"], "model ticket route")
    if route_id != "claude-code":
        raise ReleaseInputError(
            "release review ticket must use the qualified Claude route"
        )
    _sha40(ticket["source_revision"], "model ticket source_revision")
    _string(ticket["ticket"], "model ticket")
    return ticket


def _approved_model_response(value: Any, ticket: dict[str, Any]) -> dict[str, Any]:
    response = _object(value, "review response")
    _exact_fields(response, _MODEL_RESPONSE_FIELDS, "review response")
    if response["model_use_id"] != ticket["model_use_id"]:
        raise ReleaseInputError("review response model use does not match its ticket")
    if response["route_id"] != ticket["route_id"]:
        raise ReleaseInputError("review response route does not match its ticket")
    if response["status"] != "approved" or response["verdict"] != "APPROVE":
        raise ReleaseInputError("independent review blocked the release")
    return response


def _candidate_approval_response(
    value: Any,
    run_input: dict[str, Any],
    candidate_revision: str,
    review_model_use_id: int,
) -> dict[str, Any]:
    approval = _object(value, "candidate approval")
    _exact_fields(
        approval,
        _CANDIDATE_APPROVAL_FIELDS,
        "candidate approval",
    )
    if approval["authority"] not in {"operator", "standing-delegation"}:
        raise ReleaseInputError("candidate approval authority is invalid")
    if approval["run_id"] != run_input["run_id"]:
        raise ReleaseInputError("candidate approval run does not match")
    if approval["contract_digest"] != run_input["contract_revision"]:
        raise ReleaseInputError("candidate approval contract does not match")
    if approval["candidate_revision"] != candidate_revision:
        raise ReleaseInputError("candidate approval revision does not match")
    if approval["review_model_use_id"] != review_model_use_id:
        raise ReleaseInputError("candidate approval review evidence does not match")
    _hash(approval["approval_id_hash"], "candidate approval identifier hash")
    return approval


def _collect_candidate_approval(
    run_input: dict[str, Any],
    dependencies: dict[str, Any],
    candidate: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    candidate_revision = _sha40(
        candidate.get("source_revision"),
        "candidate.source_revision",
    )
    if "collect_candidate_approval" in dependencies:
        response = dependencies["collect_candidate_approval"](
            run_input,
            candidate,
            review,
        )
    else:
        command = run_input["model_request"]["command"]
        process = subprocess.run(
            [
                *command[:-2],
                "approvals",
                "check",
                run_input["run_id"],
                candidate_revision,
                "--contract-digest",
                run_input["contract_revision"],
            ],
            cwd=Path(dependencies.get("workspace", Path.cwd())).resolve(),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if process.returncode != 0:
            raise ReleaseInputError("candidate approval check failed")
        try:
            response = json.loads(process.stdout.strip())
        except json.JSONDecodeError as error:
            raise ReleaseInputError(
                "candidate approval check returned invalid JSON"
            ) from error
    return _candidate_approval_response(
        response,
        run_input,
        candidate_revision,
        review["model_use_id"],
    )


def _request_model_ticket_from_runner(
    run_input: dict[str, Any],
    source_revision: str,
    workspace: Path,
) -> dict[str, Any]:
    model_request = run_input["model_request"]
    command = model_request["command"]
    environment = dict(os.environ)
    environment.pop(model_request["ticket_environment"], None)
    process = subprocess.run(
        [
            *command,
            run_input["run_id"],
            "review",
            str(workspace),
            source_revision,
        ],
        cwd=workspace,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if process.returncode != 0:
        raise ReleaseInputError("review model ticket request failed")
    try:
        return _issued_model_ticket(json.loads(process.stdout.strip()))
    except json.JSONDecodeError as error:
        raise ReleaseInputError("review model ticket request returned invalid JSON") from error


def _invoke_ticketed_model_from_runner(
    run_input: dict[str, Any],
    ticket: dict[str, Any],
    packet: dict[str, Any],
    workspace: Path,
) -> dict[str, Any]:
    model_request = run_input["model_request"]
    scratch_root = workspace / "to-delete"
    scratch_root.mkdir(exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="aiops-release-review-", dir=scratch_root))
    try:
        packet_path = scratch / "review.json"
        packet_path.write_text(
            json.dumps(packet, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        packet_path.chmod(0o600)
        command = model_request["command"]
        environment = dict(os.environ)
        environment[model_request["ticket_environment"]] = ticket["ticket"]
        process = subprocess.run(
            [
                *command[:-1],
                "invoke",
                run_input["run_id"],
                "review",
                str(packet_path),
                str(workspace),
            ],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        try:
            response = json.loads(process.stdout.strip())
        except json.JSONDecodeError as error:
            raise ReleaseInputError("review model invocation returned invalid JSON") from error
        parsed = _approved_model_response(response, ticket)
        if process.returncode != 0:
            raise ReleaseInputError("review model invocation failed")
        return parsed
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _milestone(
    sequence: int,
    name: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "type": "milestone",
        "sequence": sequence,
        "name": name,
        "status": "pass",
        "reason": reason,
        "evidence": evidence or {},
    }


def _project_result(
    sequence: int,
    status: str,
    reason: str,
    run_input: dict[str, Any],
    evidence: dict[str, Any] | None = None,
    candidate_revision: str | None = None,
    review_model_use_id: int | None = None,
) -> dict[str, Any]:
    return {
        "type": "result",
        "sequence": sequence,
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
        "source_revision": run_input["source_revision"],
        "candidate_revision": candidate_revision,
        "review_model_use_id": review_model_use_id,
    }


def _request_review_ticket(
    run_input: dict[str, Any],
    dependencies: dict[str, Any],
    source_revision: str,
) -> dict[str, Any]:
    workspace = Path(dependencies.get("workspace", Path.cwd())).resolve()
    request = {
        "purpose": "review",
        "run_id": run_input["run_id"],
        "source_revision": source_revision,
        "workspace": str(workspace),
    }
    if "request_model_ticket" in dependencies:
        ticket = _issued_model_ticket(dependencies["request_model_ticket"](request))
    else:
        ticket = _request_model_ticket_from_runner(run_input, source_revision, workspace)
    if ticket["source_revision"] != source_revision:
        raise ReleaseInputError("review model ticket source does not match the candidate")
    return ticket


def _invoke_review(
    run_input: dict[str, Any],
    dependencies: dict[str, Any],
    ticket: dict[str, Any],
    packet: dict[str, Any],
) -> dict[str, Any]:
    workspace = Path(dependencies.get("workspace", Path.cwd())).resolve()
    request = {
        "packet": packet,
        "purpose": "review",
        "run_input": run_input,
        "ticket": ticket,
        "workspace": str(workspace),
    }
    if "invoke_model" in dependencies:
        response = dependencies["invoke_model"](request)
    else:
        response = _invoke_ticketed_model_from_runner(
            run_input,
            ticket,
            packet,
            workspace,
        )
    return _approved_model_response(response, ticket)


def execute_release_run(
    raw_input: str,
    dependencies: dict[str, Any],
    *,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Execute one complete project-owned release outcome."""
    run_input: dict[str, Any] | None = None
    events: list[dict[str, Any]] = []
    candidate_revision: str | None = None

    def record(event: dict[str, Any]) -> dict[str, Any]:
        event["sequence"] = len(events) + 1
        events.append(event)
        if emit is not None:
            emit(event)
        return event

    def heartbeat() -> None:
        record({"type": "heartbeat", "sequence": 0})

    try:
        run_input = parse_release_run_input(raw_input)
        if "set_heartbeat" in dependencies:
            dependencies["set_heartbeat"](heartbeat)
        if dependencies["source_revision"]() != run_input["source_revision"]:
            raise ReleaseInputError("source revision does not match the admitted run")
        admission_input = dependencies["collect_admission"](run_input)
        admission = evaluate_stage("admission", admission_input)
        if admission["status"] in {"blocked", "failed"}:
            record(
                _project_result(
                    1,
                    admission["status"],
                    admission["reason"],
                    run_input,
                    admission["evidence"],
                )
            )
            return events
        record(
            _milestone(1, "admission", admission["reason"], admission["evidence"])
        )
        if admission["status"] == "no_action":
            ticket = _request_review_ticket(
                run_input,
                dependencies,
                run_input["source_revision"],
            )
            review = _invoke_review(
                run_input,
                dependencies,
                ticket,
                {
                    "admission": admission,
                    "instruction": (
                        "Approve only when exact current remote main evidence proves "
                        "that no Data Olympus release is due."
                    ),
                    "source_revision": run_input["source_revision"],
                },
            )
            record(
                _project_result(
                    2,
                    "no_action",
                    admission["reason"],
                    run_input,
                    admission["evidence"],
                    review_model_use_id=review["model_use_id"],
                )
            )
            return events

        prepared_input = dependencies["prepare"](run_input, admission)
        prepared = evaluate_stage("prepare", prepared_input)
        if prepared["status"] != "pass":
            raise ReleaseInputError(prepared["reason"])
        candidate = _object(prepared_input["candidate"], "candidate")
        candidate_revision = _sha40(
            candidate.get("source_revision"),
            "candidate.source_revision",
        )
        record(
            _milestone(2, "prepare", prepared["reason"], prepared["evidence"])
        )

        validation_input = dependencies["validate"](run_input, admission, prepared)
        validation = evaluate_stage("validate", validation_input)
        if validation["status"] != "pass":
            raise ReleaseInputError(validation["reason"])
        record(
            _milestone(3, "validate", validation["reason"], validation["evidence"])
        )
        revision_reader = dependencies.get(
            "candidate_revision",
            dependencies["source_revision"],
        )
        if revision_reader() != candidate_revision:
            raise ReleaseInputError("candidate revision changed during validation")

        ticket = _request_review_ticket(run_input, dependencies, candidate_revision)
        packet = {
            "candidate": candidate,
            "instruction": (
                "Independently review the exact deterministic Data Olympus release "
                "candidate. Approve only when source identity, tests, security, "
                "version, rollback point, and immutable release procedures pass."
            ),
            "prepared_evidence": prepared["evidence"],
            "source_revision": candidate_revision,
            "validation_evidence": validation["evidence"],
        }
        review = _invoke_review(run_input, dependencies, ticket, packet)
        approval = _collect_candidate_approval(
            run_input,
            dependencies,
            candidate,
            review,
        )
        reviewed = evaluate_stage(
            "review",
            {
                "candidate": candidate,
                "current_source_revision": candidate_revision,
                "contract_revision": run_input["contract_revision"],
                "run_id": run_input["run_id"],
                "review_model_use_id": review["model_use_id"],
                "executor": {
                    "family": "deterministic",
                    "source_revision": candidate_revision,
                },
                "companion_review": {
                    "family": "claude",
                    "verdict": "APPROVE",
                    "reviewed_source_revision": candidate_revision,
                    "evidence_hash": sha256(
                        json.dumps(
                            {"packet": packet, "response": review},
                            separators=(",", ":"),
                            sort_keys=True,
                        ).encode()
                    ).hexdigest(),
                },
                "candidate_approval": approval,
            },
        )
        if reviewed["status"] != "pass":
            raise ReleaseInputError(reviewed["reason"])
        record(
            _milestone(4, "domain_review", reviewed["reason"], reviewed["evidence"])
        )
        if revision_reader() != candidate_revision:
            raise ReleaseInputError("candidate revision changed before delivery")

        delivery_input = dependencies["deliver"](
            run_input,
            admission,
            prepared,
            reviewed,
        )
        delivery = evaluate_stage("deliver", delivery_input)
        if delivery["status"] != "pass":
            raise ReleaseInputError(delivery["reason"])
        record(
            _milestone(5, "deliver", delivery["reason"], delivery["evidence"])
        )

        verification_input = dependencies["verify"](run_input, admission, delivery)
        verification = evaluate_stage("verify", verification_input)
        if verification["status"] != "pass":
            raise ReleaseInputError(verification["reason"])
        record(
            _milestone(6, "verify", verification["reason"], verification["evidence"])
        )
        record(
            _project_result(
                7,
                "delivered",
                verification["reason"],
                run_input,
                {
                    **delivery["outputs"],
                    **verification["evidence"],
                    "rollback_digest": prepared["evidence"][
                        "rollback_digest"
                    ],
                    "release_pr_number": prepared["outputs"][
                        "release_pr_number"
                    ],
                },
                candidate_revision=candidate_revision,
                review_model_use_id=review["model_use_id"],
            )
        )
        return events
    except (
        KeyError,
        OSError,
        ReleaseInputError,
        ValueError,
        subprocess.SubprocessError,
    ) as error:
        if run_input is None:
            raise
        delivered = any(event.get("name") == "deliver" for event in events)
        external_state_changed = bool(
            getattr(error, "external_state_changed", False)
        )
        failure_evidence = getattr(error, "evidence", {})
        if type(failure_evidence) is not dict:
            failure_evidence = {}
        record(
            _project_result(
                len(events) + 1,
                "failed" if delivered or external_state_changed else "blocked",
                str(error),
                run_input,
                failure_evidence,
                candidate_revision=candidate_revision,
            )
        )
        return events


def _default_dependencies() -> dict[str, Any]:
    from scripts.operations.release_runtime import default_release_dependencies

    return default_release_dependencies()


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


def main(
    argv: list[str] | None = None,
    *,
    dependencies: dict[str, Any] | None = None,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["rollback"]:
        active_dependencies = (
            dependencies if dependencies is not None else _default_dependencies()
        )
        raw_recovery = os.environ.get("AI_OPERATIONS_RECOVERY_INPUT")
        if raw_recovery is None:
            output = {
                "status": "failed",
                "reason": "AI_OPERATIONS_RECOVERY_INPUT is required",
                "evidence": {},
                "source_revision": "unavailable",
            }
            print(json.dumps(output, separators=(",", ":"), sort_keys=True))
            return 2
        recovery_input: Any = None
        try:
            recovery_input = json.loads(raw_recovery)
            recovery = active_dependencies["rollback"](recovery_input)
        except (
            KeyError,
            OSError,
            ReleaseInputError,
            ValueError,
            json.JSONDecodeError,
            subprocess.SubprocessError,
        ) as error:
            recovery_source = (
                recovery_input.get("source_revision")
                if type(recovery_input) is dict
                else None
            )
            failure_evidence = getattr(error, "evidence", {})
            if type(failure_evidence) is not dict:
                failure_evidence = {}
            recovery = {
                "status": "failed",
                "reason": str(error),
                "evidence": failure_evidence,
                "source_revision": (
                    recovery_source
                    if type(recovery_source) is str
                    and re.fullmatch(r"[0-9a-f]{40}", recovery_source)
                    else "unavailable"
                ),
            }
        print(json.dumps(recovery, separators=(",", ":"), sort_keys=True))
        return 0 if recovery.get("status") == "pass" else 1
    if not arguments:
        raw_run = os.environ.get("AI_OPERATIONS_RUN_INPUT")
        if raw_run is None:
            output = _result("failed", "AI_OPERATIONS_RUN_INPUT is required")
            print(json.dumps(output, separators=(",", ":"), sort_keys=True))
            return 2
        active_dependencies = (
            dependencies if dependencies is not None else _default_dependencies()
        )
        def emit_event(event: dict[str, Any]) -> None:
            print(
                json.dumps(event, separators=(",", ":"), sort_keys=True),
                flush=True,
            )

        try:
            events = execute_release_run(
                raw_run,
                active_dependencies,
                emit=emit_event,
            )
        except ReleaseInputError as error:
            print(
                json.dumps(
                    _result("failed", str(error)),
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
            return 1
        final = events[-1]
        return 0 if final["status"] in {"delivered", "no_action"} else 1
    output = _result("failed", "unsupported Data Olympus release operation")
    print(json.dumps(output, separators=(",", ":"), sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
