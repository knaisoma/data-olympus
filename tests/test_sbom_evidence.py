"""Per-artifact SBOM evidence: stage-one receipt, stage-two binding, promotion."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest

from scripts import rc_verify_and_publish as stage
from scripts import release_record
from scripts import sbom_evidence as sbom
from tests.test_rc_verify_and_publish import digest, make_build, tar

PLATFORMS = {"linux/amd64": "amd64", "linux/arm64": "arm64"}


def spdx(*, root: dict, components: int = 1, generator: str = sbom.GENERATOR) -> bytes:
    packages = [{"SPDXID": "SPDXRef-Root", **root}] + [
        {"SPDXID": f"SPDXRef-Package-{i}", "name": f"dep{i}", "versionInfo": "1.0"}
        for i in range(components)]
    return json.dumps({
        "spdxVersion": "SPDX-2.3", "SPDXID": "SPDXRef-DOCUMENT", "name": "x",
        "creationInfo": {"creators": ["Organization: Anchore, Inc", generator]},
        "packages": packages,
        "relationships": [{"spdxElementId": "SPDXRef-DOCUMENT",
                           "relatedSpdxElement": "SPDXRef-Root",
                           "relationshipType": "DESCRIBES"}]}).encode()


def image_doc(manifest_digest: str, **kwargs) -> bytes:
    return spdx(root={"name": sbom.IMAGE, "checksums": [
        {"algorithm": "SHA256", "checksumValue": manifest_digest[7:]}]}, **kwargs)


def package_doc(version: str, **kwargs) -> bytes:
    return spdx(root={"name": sbom.PROJECT, "versionInfo": version}, **kwargs)


def index_archive(path, *, attestation: bool = True) -> tuple[str, dict[str, str], bytes]:
    """A multi-platform OCI archive like buildx writes: index root, platform
    manifests and an unknown/unknown attestation manifest."""
    config = b"{}"
    blobs = {"blobs/sha256/" + digest(config): config}
    entries, manifests = [], {}
    platforms = [(p.split("/")[0], p.split("/")[1]) for p in PLATFORMS]
    if attestation:
        platforms.append(("unknown", "unknown"))
    for os_name, arch in platforms:
        manifest = json.dumps({"schemaVersion": 2, "config": {
            "digest": "sha256:" + digest(config), "size": len(config)},
            "layers": [], "annotations": {"arch": arch}}).encode()
        blobs["blobs/sha256/" + digest(manifest)] = manifest
        entries.append({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": "sha256:" + digest(manifest), "size": len(manifest),
                        "platform": {"os": os_name, "architecture": arch}})
        if os_name != "unknown":
            manifests[f"{os_name}/{arch}"] = "sha256:" + digest(manifest)
    index = json.dumps({"schemaVersion": 2, "mediaType": sbom.OCI_INDEX[0],
                        "manifests": entries}).encode()
    blobs["blobs/sha256/" + digest(index)] = index
    tar(path, {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
               "index.json": json.dumps({"schemaVersion": 2, "manifests": [
                   {"mediaType": sbom.OCI_INDEX[0], "digest": "sha256:" + digest(index),
                    "size": len(index)}]}).encode(), **blobs})
    return "sha256:" + digest(index), manifests, index


def add_sboms(build):
    """Turn a make_build candidate into one carrying the four SBOM documents."""
    _, artifacts, provenance, _ = build
    image, manifests, _ = index_archive(artifacts / "image.oci.tar")
    provenance["image_digest"] = image
    provenance["oci_archive_sha256"] = digest((artifacts / "image.oci.tar").read_bytes())
    receipt, version = provenance["candidate"], provenance["python_version"]
    directory = artifacts / sbom.SBOM_DIR
    directory.mkdir()
    for kind in ("wheel", "sdist"):
        (directory / f"{receipt[kind]}.spdx.json").write_bytes(package_doc(version))
    for platform, arch in PLATFORMS.items():
        (directory / f"image-linux-{arch}.spdx.json").write_bytes(image_doc(manifests[platform]))
    provenance["sboms"] = sbom.describe(
        directory, manifests=manifests, version=version,
        wheel=(receipt["wheel"], receipt["wheel_sha256"]),
        sdist=(receipt["sdist"], receipt["sdist_sha256"]))
    write(build)
    return manifests


def write(build):
    (build[1] / "release-provenance.json").write_text(json.dumps(build[2]))


@pytest.fixture
def build(tmp_path):
    return make_build(tmp_path)


def verify(build):
    repo, artifacts, _, event = build
    return stage.verify(artifacts, repo.path, event, main="main",
                        branch_ref=event["workflow_run"]["head_branch"])


# Stage one: the receipt entries.

def test_describe_binds_documents_to_subjects(build):
    manifests = add_sboms(build)
    entries = build[2]["sboms"]
    receipt = build[2]["candidate"]
    assert [(e["subject"], e.get("platform"), e["subject_digest"]) for e in entries] == [
        ("wheel", None, "sha256:" + receipt["wheel_sha256"]),
        ("sdist", None, "sha256:" + receipt["sdist_sha256"]),
        ("image", "linux/amd64", manifests["linux/amd64"]),
        ("image", "linux/arm64", manifests["linux/arm64"])]
    assert {e["predicate_type"] for e in entries} == {"https://spdx.dev/Document/v2.3"}
    for entry in entries:
        raw = (build[1] / "sbom" / entry["file"]).read_bytes()
        assert entry["sha256"] == digest(raw)


def test_embed_adds_the_receipt_to_finalized_provenance(build):
    add_sboms(build)
    _, artifacts, provenance, _ = build
    expected = provenance.pop("sboms")
    write(build)
    path = artifacts / "release-provenance.json"
    args = ["embed", "--provenance", str(path), "--sbom-dir", str(artifacts / "sbom"),
            "--oci-archive", str(artifacts / "image.oci.tar")]
    assert sbom.main(args) == 0
    assert json.loads(path.read_text())["sboms"] == expected
    # Never twice, and never for an archive other than the recorded one.
    assert sbom.main(args) == 1
    del provenance["candidate"]["wheel_sha256"]
    provenance["oci_archive_sha256"] = "0" * 64
    write(build)
    assert sbom.main(args) == 1


@pytest.mark.parametrize("damage", ["generator", "root-only", "image-digest", "version",
                                    "spdx-version", "not-json"])
def test_documents_failing_fixed_rules_are_refused(damage):
    manifest = "sha256:" + "a" * 64
    if damage == "generator":
        raw, kind = image_doc(manifest, generator="Tool: syft-0.0.1"), "image"
    elif damage == "root-only":
        raw, kind = image_doc(manifest, components=0), "image"
    elif damage == "image-digest":
        raw, kind = image_doc("sha256:" + "b" * 64), "image"
    elif damage == "version":
        raw, kind = package_doc("9.9.9"), "wheel"
    elif damage == "spdx-version":
        raw, kind = package_doc("1.0rc1").replace(b"SPDX-2.3", b"SPDX-2.2"), "wheel"
    else:
        raw, kind = b"{not json", "wheel"
    with pytest.raises(ValueError):
        sbom.validate_document(raw, kind=kind, subject_digest=manifest, version="1.0rc1")
    good = image_doc(manifest) if kind == "image" else package_doc("1.0rc1")
    sbom.validate_document(good, kind=kind, subject_digest=manifest, version="1.0rc1")


def test_index_must_hold_exactly_the_release_platforms(tmp_path):
    _, _, index = index_archive(tmp_path / "a.tar")
    data = json.loads(index)
    assert set(sbom.platform_manifests(index)) == set(PLATFORMS)
    data["manifests"] = data["manifests"][1:]
    with pytest.raises(ValueError, match="platforms differ"):
        sbom.platform_manifests(json.dumps(data).encode())


# Stage two: binding to the verified artifacts.

def test_stage_two_publishes_documents_as_bound_assets(build):
    add_sboms(build)
    verified = verify(build)
    names = {e["file"] for e in build[2]["sboms"]}
    assert names <= set(verified.assets)
    assert len(verified.sboms) == 4
    # The candidate tag binds the SBOM bytes like every other asset.
    for name in names:
        assert name in stage.tag_message(verified)
    outputs = dict(line.split("=", 1) for line in
                   stage.attestation_outputs(verified).splitlines())
    assert outputs["sbom"] == "true"
    assert outputs["image_amd64_digest"] == build[2]["sboms"][2]["subject_digest"]
    assert outputs["sbom_image_arm64"] == "image-linux-arm64.spdx.json"
    assert outputs["sbom_wheel"] == build[2]["candidate"]["wheel"] + ".spdx.json"


def test_candidate_without_sboms_publishes_but_emits_no_subjects(build):
    verified = verify(build)
    assert verified.sboms == ()
    assert stage.attestation_outputs(verified).endswith("sbom=false\n")


@pytest.mark.parametrize("damage", ["byte", "missing", "extra", "dir-without-receipt",
                                    "swapped-platforms", "wrong-wheel"])
def test_stage_two_refuses_unbound_documents(build, damage):
    add_sboms(build)
    directory = build[1] / "sbom"
    entries = build[2]["sboms"]
    if damage == "byte":
        path = directory / entries[2]["file"]
        path.write_bytes(path.read_bytes().replace(b'"x"', b'"y"'))
    elif damage == "missing":
        (directory / entries[0]["file"]).unlink()
    elif damage == "extra":
        (directory / "other.spdx.json").write_bytes(b"{}")
    elif damage == "dir-without-receipt":
        del build[2]["sboms"]
        write(build)
    elif damage == "swapped-platforms":
        entries[2]["subject_digest"], entries[3]["subject_digest"] = (
            entries[3]["subject_digest"], entries[2]["subject_digest"])
        write(build)
    else:
        entries[0]["subject_digest"] = "sha256:" + "0" * 64
        write(build)
    with pytest.raises(ValueError):
        verify(build)


def test_sbom_outputs_refuse_unsafe_values(build):
    add_sboms(build)
    verified = verify(build)
    bad = dict(verified.sboms[0], file="x\ny=z.spdx.json")
    with pytest.raises(ValueError):
        stage.attestation_outputs(replace(verified, sboms=(bad, *verified.sboms[1:])))


# Promotion: plan and attestation match.

def promotion_inputs(build, tmp_path):
    add_sboms(build)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    for path in [*(build[1] / "dist").iterdir(), *(build[1] / "sbom").iterdir()]:
        shutil.copy(path, candidate / path.name)
    with __import__("tarfile").open(build[1] / "image.oci.tar") as archive:
        index = archive.extractfile(
            "blobs/sha256/" + build[2]["image_digest"][7:]).read()
    return candidate, index


def test_plan_lists_every_subject_with_its_stored_document(build, tmp_path):
    candidate, index = promotion_inputs(build, tmp_path)
    lines = sbom.plan(build[2], candidate, index)
    entries = build[2]["sboms"]
    assert [(s, p.name, d) for s, p, d in lines] == [
        (str(candidate / build[2]["candidate"]["wheel"]), entries[0]["file"],
         entries[0]["subject_digest"]),
        (str(candidate / build[2]["candidate"]["sdist"]), entries[1]["file"],
         entries[1]["subject_digest"]),
        (f"oci://{sbom.IMAGE}@{entries[2]['subject_digest']}", entries[2]["file"],
         entries[2]["subject_digest"]),
        (f"oci://{sbom.IMAGE}@{entries[3]['subject_digest']}", entries[3]["file"],
         entries[3]["subject_digest"])]


@pytest.mark.parametrize("damage", ["no-receipt", "index", "stored-document", "missing"])
def test_plan_blocks_promotion(build, tmp_path, damage):
    candidate, index = promotion_inputs(build, tmp_path)
    provenance = build[2]
    if damage == "no-receipt":
        del provenance["sboms"]
    elif damage == "index":
        index = index.replace(b'"schemaVersion": 2', b'"schemaVersion":2')
    elif damage == "stored-document":
        path = candidate / provenance["sboms"][1]["file"]
        path.write_bytes(path.read_bytes() + b" ")
    else:
        (candidate / provenance["sboms"][3]["file"]).unlink()
    with pytest.raises((ValueError, OSError)):
        sbom.plan(provenance, candidate, index)


def statement(document: bytes, subject: str, predicate_type: str = sbom.PREDICATE_TYPE):
    return {"verificationResult": {"statement": {
        "predicateType": predicate_type, "subject": [{"digest": {"sha256": subject[7:]}}],
        "predicate": json.loads(document)}}}


def test_match_requires_the_signed_predicate_to_be_the_stored_document():
    subject = "sha256:" + "c" * 64
    document = image_doc(subject)
    sbom.match([statement(document, subject)], document, subject)
    other = image_doc(subject, components=2)
    for attestations in ([statement(other, subject)],
                         [statement(document, "sha256:" + "d" * 64)],
                         [statement(document, subject, "https://slsa.dev/provenance/v1")],
                         []):
        with pytest.raises(ValueError):
            sbom.match(attestations, document, subject)


# Release record: optional, additive SBOM references.

def test_record_validation_accepts_and_checks_sbom_references(build):
    add_sboms(build)
    provenance = build[2]
    provenance["candidate"] |= {"source_tree_sha256": "c" * 64, "lock_sha256": "d" * 64}
    identity = release_record.validate_candidate(
        provenance, head=provenance["H"], candidate_tag=provenance["candidate_tag"])
    assert identity.pypi_version == provenance["python_version"]
    provenance["sboms"][1]["subject_digest"] = "sha256:" + "e" * 64
    with pytest.raises(ValueError, match="sdist subject"):
        release_record.validate_candidate(
            provenance, head=provenance["H"], candidate_tag=provenance["candidate_tag"])


def test_release_record_carries_sbom_references_additively(tmp_path):
    from tests.test_release_record import Promotion
    promotion = Promotion(tmp_path)
    without = promotion.prove()
    receipt = promotion.provenance["candidate"]
    subjects = sbom.expected_subjects(
        wheel=(receipt["wheel"], receipt["wheel_sha256"]),
        sdist=(receipt["sdist"], receipt["sdist_sha256"]),
        manifests={"linux/amd64": "sha256:" + "1" * 64, "linux/arm64": "sha256:" + "2" * 64})
    entries = [{**s, "sha256": "9" * 64, "predicate_type": sbom.PREDICATE_TYPE,
                "generator": f"syft {sbom.SYFT_VERSION}"} for s in subjects]
    promotion.provenance["sboms"] = entries
    record = promotion.prove()
    assert record.pop("sboms") == entries
    # Same schema version and every other field unchanged: older readers keep working.
    assert record == without and record["schema_version"] == 1


# Workflow contracts.

def test_attest_check_refuses_tampered_or_misnamed_sbom_copies(tmp_path):
    import os
    import subprocess

    import yaml
    jobs = yaml.safe_load(stage_workflow().read_text())["jobs"]
    script = next(s["run"] for s in jobs["attest"]["steps"]
                  if "sha256sum --check" in s.get("run", ""))
    if subprocess.run(["bash", "-c", "command -v sha256sum"], check=False,
                      capture_output=True).returncode:
        pytest.skip("sha256sum not available")
    wheel, sdist = "data_olympus-1.4.3rc1-py3-none-any.whl", "data_olympus-1.4.3rc1.tar.gz"
    root = tmp_path / "to-delete" / "rc-input"
    (root / "dist").mkdir(parents=True)
    (root / "sbom").mkdir()
    (root / "dist" / wheel).write_bytes(b"wheel")
    (root / "dist" / sdist).write_bytes(b"sdist")
    docs = {"SBOM_WHEEL": wheel + ".spdx.json", "SBOM_SDIST": sdist + ".spdx.json",
            "SBOM_IMAGE_AMD64": "image-linux-amd64.spdx.json",
            "SBOM_IMAGE_ARM64": "image-linux-arm64.spdx.json"}
    env = {"PATH": os.environ["PATH"], "IMAGE_DIGEST": "sha256:" + "a" * 64,
           "WHEEL": wheel, "SDIST": sdist, "WHEEL_SHA256": digest(b"wheel"),
           "SDIST_SHA256": digest(b"sdist"), "SBOM": "true",
           "IMAGE_AMD64_DIGEST": "sha256:" + "b" * 64,
           "IMAGE_ARM64_DIGEST": "sha256:" + "c" * 64}
    for key, name in docs.items():
        (root / "sbom" / name).write_bytes(name.encode())
        env |= {key: name, key + "_SHA256": digest(name.encode())}

    def run(**overrides):
        for target in ("rc-attest", "rc-attest-sbom"):
            shutil.rmtree(tmp_path / "to-delete" / target, ignore_errors=True)
        return subprocess.run(["bash", "-c", script], cwd=tmp_path, env=env | overrides,
                              check=False, capture_output=True).returncode

    assert run() == 0
    assert (tmp_path / "to-delete" / "rc-attest-sbom" / docs["SBOM_IMAGE_ARM64"]).is_file()
    assert run(SBOM_IMAGE_AMD64_SHA256=digest(b"other")) != 0
    assert run(SBOM_WHEEL="../dist/" + wheel) != 0
    assert run(SBOM_IMAGE_ARM64="image-linux-arm64.spdx.json/../x") != 0
    assert run(IMAGE_ARM64_DIGEST="sha256:" + "c" * 63) != 0
    assert run(SBOM="yes") != 0
    assert run(SBOM="false", SBOM_WHEEL="anything") == 0


def stage_workflow():
    from pathlib import Path
    return Path(".github/workflows/rc-publish-stage.yml")


def test_promotion_verifies_sbom_attestations_before_any_publication():
    from pathlib import Path

    import yaml
    jobs = yaml.safe_load(Path(".github/workflows/promote-release.yml").read_text())["jobs"]
    steps = jobs["prove"]["steps"]
    load = next(s for s in steps if s.get("name", "").startswith("Validate dispatch"))
    assert "--pattern '*.spdx.json'" in load["run"]
    step = next(s for s in steps if s.get("name", "").startswith(
        "Verify stage-two SBOM attestations"))
    text = step["run"]
    assert "${{" not in text and text.startswith("set -euo pipefail")
    assert step["env"] == {"IMAGE_DIGEST": "${{ steps.proof.outputs.image_digest }}"}
    flags = " ".join(text[text.index("gh attestation verify"):
                          text.index("> \"to-delete/sbom-check")].replace("\\\n", " ").split())
    assert flags == ('gh attestation verify "$subject" --repo knaisoma/data-olympus '
                     "--signer-workflow knaisoma/data-olympus/.github/workflows/"
                     "rc-publish-stage.yml --source-ref refs/heads/main "
                     "--deny-self-hosted-runners --predicate-type "
                     "https://spdx.dev/Document/v2.3 --format json")
    for needle in ('skopeo inspect --raw "docker://ghcr.io/knaisoma/data-olympus@$IMAGE_DIGEST"',
                   "scripts/sbom_evidence.py plan", "scripts/sbom_evidence.py match",
                   'test "$count" -eq 4', "<&3", "3< to-delete/sbom-check/plan.txt"):
        assert needle in text, needle
    names = [s.get("name", "") for s in steps]
    upload = next(i for i, s in enumerate(steps) if "upload-artifact@" in s.get("uses", ""))
    assert (names.index("Verify stage-two attestations of the candidate files and image")
            < steps.index(step) < upload)
