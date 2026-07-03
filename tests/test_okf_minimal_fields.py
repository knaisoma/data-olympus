"""Structural assertion of OKF's minimal required fields on example-bundle.

Context (see WHY.md "How this relates to OKF" and the tracking issue linked
from README.md / SPEC.md section 11): data-olympus claims to be an
OKF-compatible profile, but has no executable conformance suite against the
real OKF reference tooling. That is a genuine gap and this test does not
close it.

What this test DOES prove: every concept document in example-bundle carries
non-empty `id` and `type` values (OKF's per-document minimal fields), and the
bundle-root index.md declares an `okf_version`, both structurally, by reading
the actual frontmatter rather than trusting documentation prose.

What this test does NOT prove:
- That an OKF reference consumer can actually parse and accept this bundle.
- That `type` values used here are within whatever vocabulary (if any) the
  real OKF spec expects at the wire level (data-olympus's `type` vocabulary is
  its own controlled extension; OKF is documented as leaving `type` open).
- Anything about OKF's `spec_version` field specifically: data-olympus splits
  this into two bundle-root fields (`spec_version` for this format,
  `okf_version` for the OKF target), a data-olympus-specific convention, not
  a directly-checked OKF field.
- Round-trip fidelity, link resolution, or any behavioral conformance.

This is a cheap structural floor, not a conformance test. See the tracking
issue for the real conformance check against OKF reference tooling.
"""
from __future__ import annotations

from pathlib import Path

from data_olympus.format import discover_bundle_files
from data_olympus.format.frontmatter import parse_frontmatter

_BUNDLE_ROOT = Path(__file__).resolve().parent.parent / "example-bundle"


def test_every_concept_doc_has_nonempty_id_and_type() -> None:
    files = discover_bundle_files(_BUNDLE_ROOT)
    assert files, "example-bundle must have concept files to make this assertion meaningful"
    missing: list[str] = []
    for path in files:
        fm, _body = parse_frontmatter(path.read_text(encoding="utf-8"))
        doc_id = fm.get("id")
        doc_type = fm.get("type")
        if not (isinstance(doc_id, str) and doc_id.strip()):
            missing.append(f"{path.relative_to(_BUNDLE_ROOT)}: missing/empty id")
        if not (isinstance(doc_type, str) and doc_type.strip()):
            missing.append(f"{path.relative_to(_BUNDLE_ROOT)}: missing/empty type")
    assert not missing, "\n".join(missing)


def test_bundle_root_index_declares_okf_version() -> None:
    root_index = _BUNDLE_ROOT / "index.md"
    fm, _body = parse_frontmatter(root_index.read_text(encoding="utf-8"))
    okf_version = fm.get("okf_version")
    assert isinstance(okf_version, str) and okf_version.strip(), (
        "example-bundle/index.md must declare a non-empty okf_version "
        "(the OKF spec version this bundle targets) for the OKF-compatibility "
        "claim to have even a structural floor"
    )
