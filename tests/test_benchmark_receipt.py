"""Executable contract for committed benchmark provenance receipts."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

_SOURCE_COMMIT = "1" * 40


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _benchmark_repo(root: Path) -> Path:
    _write(
        root / "uv.lock",
        """version = 1

[[package]]
name = "data-olympus"
version = "0.6.0"

[[package]]
name = "fastmcp"
version = "3.4.4"

[[package]]
name = "numpy"
version = "2.5.1"

[[package]]
name = "sentence-transformers"
version = "5.6.0"

[[package]]
name = "tiktoken"
version = "0.13.0"
""",
    )
    _write(root / "benchmarks" / "run.py", "K = 5\n")
    # The guard hashes product source as well as benchmark source, so the
    # fixture carries one shipped module. Without it, no test here would
    # exercise the src/ half of the pattern set.
    _write(root / "src" / "data_olympus" / "write_gate.py", "SCAN = True\n")
    _write(root / "benchmarks" / "tokenizer.py", "class SimpleTokenizer: pass\n")
    _write(root / "benchmarks" / "corpus" / "z.md", "z corpus\n")
    _write(root / "benchmarks" / "corpus" / "a.md", "a corpus\n")
    _write(root / "benchmarks" / "governance" / "rule.md", "rule corpus\n")
    _write(root / "example-bundle" / "universal" / "example.md", "real corpus\n")
    _write(root / "benchmarks" / "queries.yaml", "- text: first\n")
    _write(root / "benchmarks" / "governance_queries.yaml", "- text: governed\n")
    _write(
        root / "benchmarks" / "real_corpus" / "example_bundle_queries.json",
        "[]\n",
    )
    _write(
        root / "benchmarks" / "results" / "results.json",
        json.dumps(
            {
                "tokenizer": "simple",
                "rag_included": False,
                "rows": [
                    {
                        "method": "data-olympus",
                        "category": "ALL",
                        "mean_tokens": 100.0,
                        "recall": 0.75,
                        "serves_stale": 0.0,
                    }
                ],
            }
        )
        + "\n",
    )
    _write(root / "benchmarks" / "results" / "report.md", "# Report\n")
    _write(
        root / "benchmarks" / "governance_results" / "ablation.json",
        json.dumps({"k": 5, "rows": []}) + "\n",
    )
    _write(
        root / "benchmarks" / "governance_results" / "ablation.md",
        "# Governance\n",
    )
    _write(
        root / "benchmarks" / "governance_results" / "embeddings" / "ablation.json",
        json.dumps({"k": 5, "rows": []}) + "\n",
    )
    _write(
        root / "benchmarks" / "governance_results" / "embeddings" / "ablation.md",
        "# Embeddings\n",
    )
    _write(
        root / "benchmarks" / "real_corpus" / "example_bundle_result.json",
        json.dumps({"recall@5": 0.8, "recall@10": 0.9, "mrr@5": 0.7}) + "\n",
    )
    return root


def _commit_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Benchmark Test",
            "-c",
            "user.email=benchmark@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=root,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_build_receipt_is_deterministic_and_complete(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt

    root = _benchmark_repo(tmp_path)
    first = build_receipt(root, _SOURCE_COMMIT)
    second = build_receipt(root, _SOURCE_COMMIT)

    assert first == second
    assert first["schema_version"] == 1
    assert first["source_commit"] == _SOURCE_COMMIT
    assert set(first) == {
        "schema_version",
        "source_commit",
        "source_tree",
        "inputs",
        "dependency_lock",
        "environment",
        "retrieval",
        "commands",
        "seeds",
        "outputs",
        "summary",
    }
    assert first["inputs"]["corpora"]["files"] == sorted(
        first["inputs"]["corpora"]["files"], key=lambda item: item["path"]
    )
    assert first["inputs"]["queries"]["files"] == sorted(
        first["inputs"]["queries"]["files"], key=lambda item: item["path"]
    )
    assert first["dependency_lock"]["path"] == "uv.lock"
    assert first["dependency_lock"]["packages"] == [
        {"name": "data-olympus", "version": "0.6.0"},
        {"name": "fastmcp", "version": "3.4.4"},
        {"name": "numpy", "version": "2.5.1"},
        {"name": "sentence-transformers", "version": "5.6.0"},
        {"name": "tiktoken", "version": "0.13.0"},
    ]
    assert first["retrieval"] == {
        "tokenizer": "benchmarks.tokenizer.SimpleTokenizer",
        "tokenizer_identity": "simple",
        "models": [
            {
                "identity": "BAAI/bge-small-en-v1.5",
                "scope": "optional embedding ablation only",
            }
        ],
    }
    assert first["seeds"] == {
        "bootstrap": 12345,
        "governance_corpus": 0,
        "synthetic_corpus": 0,
        "token_curve_corpus": 42,
    }
    assert first["commands"][-1].startswith("uv run python -m benchmarks.receipt write")
    assert first["summary"]["synthetic"]["recall_at_k"] == 0.75
    assert len(first["outputs"]["files"]) == 7


def test_verify_receipt_detects_input_output_and_lock_drift(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt, verify_receipt

    root = _benchmark_repo(tmp_path)
    receipt = build_receipt(root, _commit_repo(root))
    assert verify_receipt(receipt, root) == []

    corpus = root / "benchmarks" / "corpus" / "a.md"
    corpus.write_text("mutated corpus\n", encoding="utf-8")
    assert any("inputs.corpora" in problem for problem in verify_receipt(receipt, root))
    corpus.write_text("a corpus\n", encoding="utf-8")

    result = root / "benchmarks" / "results" / "results.json"
    result.write_text("{}\n", encoding="utf-8")
    assert any("outputs" in problem for problem in verify_receipt(receipt, root))

    _write(root / "uv.lock", "version = 1\n")
    assert any("dependency_lock" in problem for problem in verify_receipt(receipt, root))


def test_verify_receipt_rejects_schema_and_source_commit_tampering(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt, verify_receipt

    root = _benchmark_repo(tmp_path)
    receipt = build_receipt(root, _commit_repo(root))

    receipt["schema_version"] = 2
    receipt["source_commit"] = "not-a-sha"
    problems = verify_receipt(receipt, root)
    assert any("schema_version" in problem for problem in problems)
    assert any("source_commit" in problem for problem in problems)


def test_verify_receipt_rejects_unknown_commit_in_a_git_checkout(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt, verify_receipt

    root = _benchmark_repo(tmp_path)
    source_commit = _commit_repo(root)
    receipt = build_receipt(root, source_commit)
    assert verify_receipt(receipt, root) == []

    receipt["source_commit"] = "f" * 40
    assert any("does not exist" in problem for problem in verify_receipt(receipt, root))


def test_verify_receipt_rejects_unverifiable_commit_outside_git(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt, verify_receipt

    root = _benchmark_repo(tmp_path)
    receipt = build_receipt(root, _SOURCE_COMMIT)

    assert any("not a git checkout" in problem for problem in verify_receipt(receipt, root))


def test_verify_receipt_recomputes_summary(tmp_path: Path) -> None:
    from benchmarks.receipt import build_receipt, verify_receipt

    root = _benchmark_repo(tmp_path)
    receipt = build_receipt(root, _commit_repo(root))
    receipt["summary"]["synthetic"]["recall_at_k"] = 0.999

    assert any("summary" in problem for problem in verify_receipt(receipt, root))


def test_artifact_generator_writes_receipt_for_current_head(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    from benchmarks import generate_artifacts, receipt

    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(receipt, "current_source_commit", lambda _root: _SOURCE_COMMIT)
    monkeypatch.setattr(
        receipt,
        "write_receipt",
        lambda root, source_commit: calls.append((root, source_commit)) or tmp_path,
    )

    generate_artifacts.write_provenance_receipt(tmp_path)

    assert calls == [(tmp_path, _SOURCE_COMMIT)]


def test_docs_guard_surfaces_receipt_drift(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    """Every verify_receipt failure other than the current-lock comparison is
    passed through untouched."""
    from benchmarks import receipt
    from scripts import check_benchmark_docs

    receipt_path = tmp_path / receipt.RECEIPT_PATH
    _write(receipt_path, "{}\n")
    monkeypatch.setattr(
        receipt,
        "verify_receipt",
        lambda _document, _root: ["outputs sha256 does not match the repository"],
    )

    # The empty document has neither dependency_lock nor source_tree to bind, so
    # both historical checks contribute their own problem alongside the
    # preserved one, and it is not usable as a comparison reference either.
    assert check_benchmark_docs.receipt_problems(tmp_path) == [
        "outputs sha256 does not match the repository",
        "receipt cannot be used as a comparison reference: reference receipt is "
        "missing required field 'schema_version'",
        "dependency_lock is missing from the receipt",
        "source_tree is missing from the receipt",
    ]


def _committed_benchmark_repo(tmp_path: Path) -> tuple[Path, dict]:
    """A fixture repo whose receipt binds to a real commit in its own history."""
    from benchmarks.receipt import build_receipt

    root = _benchmark_repo(tmp_path)
    _write(
        root / "pyproject.toml",
        '[project]\nname = "data-olympus"\nversion = "0.6.0"\n',
    )
    receipt = build_receipt(root, _commit_repo(root))
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )
    return root, receipt


def _bump_current_lock(root: Path, old: str, new: str) -> None:
    lock_path = root / "uv.lock"
    lock_path.write_text(
        lock_path.read_text(encoding="utf-8").replace(old, new, 1),
        encoding="utf-8",
    )


def test_docs_guard_allows_current_lock_drift_for_any_package(
    tmp_path: Path,
) -> None:
    """A dependency update does not invalidate a past measurement.

    This is the whole point of binding the receipt to its measurement commit
    instead of the working tree: previously *any* uv.lock change failed this
    guard, which stalled security updates. It holds for a benchmark-relevant
    package too, because the receipt still describes the environment the
    numbers were measured in, not the one shipping today.
    """
    from benchmarks.receipt import verify_receipt
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    _bump_current_lock(
        root,
        'name = "fastmcp"\nversion = "3.4.4"',
        'name = "fastmcp"\nversion = "3.4.6"',
    )

    assert verify_receipt(receipt, root) == ["dependency_lock does not match uv.lock"]
    assert check_benchmark_docs.receipt_problems(root) == []


def test_docs_guard_allows_release_version_and_dependency_drift_together(
    tmp_path: Path,
) -> None:
    """The old guard accepted a lone root-version bump through a special case.
    Under the measurement binding that is simply one more current-lock change,
    and it composes with a real dependency bump instead of being exclusive."""
    from scripts import check_benchmark_docs

    root, _ = _committed_benchmark_repo(tmp_path)
    _bump_current_lock(
        root,
        'name = "data-olympus"\nversion = "0.6.0"',
        'name = "data-olympus"\nversion = "0.7.0"',
    )
    _bump_current_lock(
        root,
        'name = "numpy"\nversion = "2.5.1"',
        'name = "numpy"\nversion = "2.5.2"',
    )
    _write(
        root / "pyproject.toml",
        '[project]\nname = "data-olympus"\nversion = "0.7.0"\n',
    )

    assert check_benchmark_docs.receipt_problems(root) == []


def test_docs_guard_rejects_tampered_measured_lock_digest(tmp_path: Path) -> None:
    """The measured environment stays tamper-evident byte for byte."""
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["dependency_lock"]["sha256"] = "0" * 64
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.receipt_problems(root)
    assert problems == [
        "dependency_lock does not match uv.lock at source_commit "
        f"{receipt['source_commit']}"
    ]


def test_docs_guard_rejects_tampered_measured_package_summary(
    tmp_path: Path,
) -> None:
    """A correct digest with an edited package summary is still a lie about the
    measured environment, and the digest alone would not catch it."""
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["dependency_lock"]["packages"] = [
        {"name": "fastmcp", "version": "9.9.9"}
    ]
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.receipt_problems(root)
    assert problems == [
        "dependency_lock packages do not match uv.lock at source_commit "
        f"{receipt['source_commit']}"
    ]


def test_docs_guard_rejects_missing_measurement_history(
    tmp_path: Path,
) -> None:
    """Missing measurement history must fail loudly rather than skip the binding.

    A shallow clone produces the same unreachable-object condition; this covers
    the condition, not the clone depth that can cause it.
    """
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    absent = "1" * 40
    receipt["source_commit"] = absent
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.receipt_problems(root)
    assert (
        "measured uv.lock is unreachable at source_commit "
        f"{absent}; a full-history checkout is required"
    ) in problems


def test_docs_guard_preserves_other_failures_alongside_current_lock_drift(
    tmp_path: Path,
) -> None:
    """The filtering boundary itself: dropping the current-lock comparison must
    not drop anything reported alongside it. Drift the current lock *and* tamper
    an input, so the suppressed error and a preserved error occur together."""
    from benchmarks.receipt import verify_receipt
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    _bump_current_lock(
        root,
        'name = "numpy"\nversion = "2.5.1"',
        'name = "numpy"\nversion = "2.5.2"',
    )
    _write(root / "benchmarks" / "corpus" / "a.md", "tampered corpus\n")

    raw = verify_receipt(receipt, root)
    assert "dependency_lock does not match uv.lock" in raw
    assert "inputs.corpora sha256 does not match the repository" in raw

    problems = check_benchmark_docs.receipt_problems(root)
    assert problems == ["inputs.corpora sha256 does not match the repository"]


def test_docs_guard_still_rejects_changed_benchmark_inputs(tmp_path: Path) -> None:
    """Relaxing the current-lock comparison must not relax anything else."""
    from scripts import check_benchmark_docs

    root, _ = _committed_benchmark_repo(tmp_path)
    _write(root / "benchmarks" / "corpus" / "a.md", "tampered corpus\n")

    problems = check_benchmark_docs.receipt_problems(root)
    assert "inputs.corpora sha256 does not match the repository" in problems


def test_public_claims_carry_independent_reproduction_label() -> None:
    root = Path(__file__).resolve().parent.parent
    label = "Maintainer-produced; not independently reproduced."
    for relative in ("WHY.md", "docs/comparison.md", "benchmarks/README.md"):
        content = (root / relative).read_text(encoding="utf-8")
        assert label in content, f"missing benchmark honesty label in {relative}"


def test_ci_fetches_receipt_source_commit_history() -> None:
    import yaml

    root = Path(__file__).resolve().parent.parent
    workflow = yaml.safe_load((root / ".github/workflows/ci.yaml").read_text())
    checkout = next(
        step
        for step in workflow["jobs"]["test"]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )

    assert checkout["with"]["fetch-depth"] == 0


# --- source_tree binds to the measurement commit, not the working tree -------
# Same defect as the dependency_lock half fixed earlier: the recorded digest was
# compared against whatever is in the tree today, so every edit under
# src/data_olympus/ or benchmarks/ failed the guard. A past measurement is not
# invalidated by later source changes.


def _edit_product_source(root: Path, content: str) -> None:
    _write(root / "src" / "data_olympus" / "write_gate.py", content)


def test_docs_guard_allows_current_product_source_drift(tmp_path: Path) -> None:
    """Editing shipped source must not fail the benchmark guard.

    This blocked every product-source change, so an ordinary bug fix could not
    go green at all. The receipt still describes the source the numbers were
    measured against, which is unchanged in history.
    """
    from benchmarks.receipt import verify_receipt
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    _edit_product_source(root, "SCAN = True\nGOOGLE = 1\n")

    assert verify_receipt(receipt, root) == [
        "source_tree sha256 does not match the repository"
    ]
    assert check_benchmark_docs.receipt_problems(root) == []


def test_docs_guard_allows_current_benchmark_source_drift(tmp_path: Path) -> None:
    """The same holds for benchmark source, which shares the pattern set."""
    from scripts import check_benchmark_docs

    root, _ = _committed_benchmark_repo(tmp_path)
    _write(root / "benchmarks" / "run.py", "K = 7\n")

    assert check_benchmark_docs.receipt_problems(root) == []


def test_docs_guard_rejects_tampered_measured_source_digest(tmp_path: Path) -> None:
    """Relaxing the working-tree comparison must not relax tamper evidence."""
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["source_tree"]["sha256"] = "0" * 64
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.receipt_problems(root)
    assert any("source_tree does not match the source at source_commit" in p for p in problems)


def test_docs_guard_rejects_a_source_file_dropped_from_the_receipt(
    tmp_path: Path,
) -> None:
    """The per-file check walks the recorded list, so it cannot see an omission.

    Only an aggregate recomputed from the measurement commit catches a file that
    existed when the benchmark ran but was left out of the receipt. Without this,
    dropping an inconvenient module from the record would verify clean.
    """
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["source_tree"]["files"] = [
        entry
        for entry in receipt["source_tree"]["files"]
        if not entry["path"].startswith("src/data_olympus/")
    ]
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.receipt_problems(root)
    assert any(
        "source_tree file list does not match the source at source_commit" in p
        for p in problems
    )


def test_docs_guard_rejects_missing_source_measurement_history(
    tmp_path: Path,
) -> None:
    """A shallow checkout must fail loudly, never skip the check.

    Asserts the *new* diagnostic specifically. An earlier version of this test
    asserted `does not exist in this checkout`, which the pre-existing per-file
    walk already emits, so it passed even with this check disabled entirely and
    proved nothing about it.
    """
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["source_commit"] = "b" * 40
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.historical_source_tree_problems(receipt, root)
    assert problems == [
        "measured source is unreachable at source_commit "
        + "b" * 40
        + "; a full-history checkout is required"
    ]


def test_docs_guard_rejects_a_receipt_recording_no_source(tmp_path: Path) -> None:
    """An empty recorded group must not verify against an empty selection.

    Without this, a receipt whose source_tree was emptied would agree with a
    commit containing no matching files, and the comparison would pass by
    describing nothing.
    """
    from scripts import check_benchmark_docs

    root, receipt = _committed_benchmark_repo(tmp_path)
    receipt["source_tree"] = {"sha256": hashlib.sha256().hexdigest(), "files": []}
    _write(
        root / "benchmarks" / "results" / "receipt.json",
        json.dumps(receipt) + "\n",
    )

    problems = check_benchmark_docs.historical_source_tree_problems(receipt, root)
    assert problems == ["source_tree records no measured source files"]


# --- reference comparison (issue #158) ---------------------------------------


def _reference_receipt(tmp_path: Path) -> dict:
    from benchmarks.receipt import build_receipt

    return build_receipt(_benchmark_repo(tmp_path / "ref"), _SOURCE_COMMIT)


def test_compare_reports_both_levels_matched_for_an_exact_reproduction(
    tmp_path: Path,
) -> None:
    from benchmarks.receipt import compare_receipts

    reference = _reference_receipt(tmp_path)
    report = compare_receipts(reference=reference, candidate=dict(reference))

    assert report["inputs"]["status"] == "matched"
    assert report["results"]["status"] == "matched"
    assert report["reproduction_status"] == "reproduced"


def test_compare_reports_inputs_mismatched_and_names_the_field(
    tmp_path: Path,
) -> None:
    """A different source commit is an input difference, and the report has to
    say WHICH input differed or a discrepancy report is not actionable."""
    from benchmarks.receipt import compare_receipts

    reference = _reference_receipt(tmp_path)
    candidate = dict(reference)
    candidate["source_commit"] = "b" * 40
    report = compare_receipts(reference=reference, candidate=candidate)

    assert report["inputs"]["status"] == "mismatched"
    assert report["results"]["status"] == "matched"
    assert report["reproduction_status"] == "inputs_differ_results_matched"
    assert any(d["field"] == "source_commit" for d in report["inputs"]["differences"])


def test_compare_reports_inputs_matched_with_results_differing(
    tmp_path: Path,
) -> None:
    """The interesting discrepancy: the same declared inputs produced different
    artifacts. The two levels fail independently, which is the whole point of
    reporting them separately."""
    from benchmarks.receipt import compare_receipts

    reference = _reference_receipt(tmp_path)
    candidate = json.loads(json.dumps(reference))
    candidate["outputs"]["sha256"] = "0" * 64
    report = compare_receipts(reference=reference, candidate=candidate)

    assert report["inputs"]["status"] == "matched"
    assert report["results"]["status"] == "mismatched"
    assert report["reproduction_status"] == "inputs_matched_results_differ"
    assert any(d["field"] == "outputs.sha256" for d in report["results"]["differences"])


def test_compare_reports_both_levels_mismatched(tmp_path: Path) -> None:
    from benchmarks.receipt import compare_receipts

    reference = _reference_receipt(tmp_path)
    candidate = json.loads(json.dumps(reference))
    candidate["source_commit"] = "c" * 40
    candidate["outputs"]["sha256"] = "0" * 64
    report = compare_receipts(reference=reference, candidate=candidate)

    assert report["reproduction_status"] == "unreproduced"


def test_compare_rejects_a_receipt_missing_a_required_field(
    tmp_path: Path,
) -> None:
    """Two omissions are not agreement. A receipt that cannot be compared is an
    error naming the field, never a match."""
    from benchmarks.receipt import ReceiptFieldMissingError, compare_receipts

    reference = _reference_receipt(tmp_path)
    candidate = json.loads(json.dumps(reference))
    del candidate["seeds"]
    try:
        compare_receipts(reference=reference, candidate=candidate)
    except ReceiptFieldMissingError as exc:
        assert "seeds" in str(exc)
        assert "candidate" in str(exc)
    else:
        raise AssertionError("a missing required field must be rejected")

    stripped = json.loads(json.dumps(reference))
    del stripped["seeds"]
    del stripped["outputs"]
    try:
        compare_receipts(reference=stripped, candidate=stripped)
    except ReceiptFieldMissingError:
        pass
    else:
        raise AssertionError("two receipts both missing a field must not match")


def test_compare_report_never_claims_independent_validation(
    tmp_path: Path,
) -> None:
    """The comparison is over RECORDED inputs and RECORDED results. receipt.py
    does not execute or observe the benchmark run, so the report must not read
    as execution attestation or as independent validation."""
    from benchmarks.receipt import compare_receipts

    reference = _reference_receipt(tmp_path)
    report = compare_receipts(reference=reference, candidate=dict(reference))
    text = json.dumps(report).lower()

    assert "recorded" in report["provenance"].lower()
    assert "independent" not in text or "not independent" in text
    assert "attest" not in text


def test_compare_cli_exits_nonzero_on_a_discrepancy(tmp_path: Path) -> None:
    from benchmarks import receipt as receipt_module

    reference = _reference_receipt(tmp_path)
    candidate = json.loads(json.dumps(reference))
    candidate["outputs"]["sha256"] = "0" * 64
    ref_path = tmp_path / "reference.json"
    cand_path = tmp_path / "candidate.json"
    ref_path.write_text(json.dumps(reference), encoding="utf-8")
    cand_path.write_text(json.dumps(candidate), encoding="utf-8")

    assert receipt_module.main(
        ["compare", "--reference", str(ref_path), "--candidate", str(ref_path)]
    ) == 0
    assert receipt_module.main(
        ["compare", "--reference", str(ref_path), "--candidate", str(cand_path)]
    ) == 1


def test_docs_guard_rejects_a_receipt_that_cannot_be_compared(
    tmp_path: Path, monkeypatch,  # noqa: ANN001
) -> None:
    """The committed receipt is the reference a third party compares against, so
    it has to stay comparable. A schema change that drops a field the comparison
    needs must fail CI here rather than surfacing as a rejected reproduction."""
    from benchmarks import receipt
    from scripts import check_benchmark_docs

    reference = build_committed_receipt_fixture(tmp_path)
    del reference["seeds"]
    receipt_path = tmp_path / receipt.RECEIPT_PATH
    _write(receipt_path, json.dumps(reference) + "\n")
    monkeypatch.setattr(receipt, "verify_receipt", lambda _document, _root: [])

    problems = check_benchmark_docs.receipt_problems(tmp_path)
    assert any("seeds" in problem for problem in problems), problems


def build_committed_receipt_fixture(tmp_path: Path) -> dict:
    from benchmarks.receipt import build_receipt

    return build_receipt(_benchmark_repo(tmp_path / "fixture"), _SOURCE_COMMIT)
