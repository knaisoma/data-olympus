"""Build and verify deterministic provenance for committed benchmark results."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
RECEIPT_PATH = Path("benchmarks/results/receipt.json")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_LOCK_PACKAGES = {
    "data-olympus",
    "fastmcp",
    "numpy",
    "pyyaml",
    "sentence-transformers",
    "tiktoken",
}
_OUTPUT_PATHS = (
    "benchmarks/governance_results/ablation.json",
    "benchmarks/governance_results/ablation.md",
    "benchmarks/governance_results/embeddings/ablation.json",
    "benchmarks/governance_results/embeddings/ablation.md",
    "benchmarks/real_corpus/example_bundle_result.json",
    "benchmarks/results/report.md",
    "benchmarks/results/results.json",
)
_QUERY_PATHS = (
    "benchmarks/governance_queries.yaml",
    "benchmarks/queries.yaml",
    "benchmarks/real_corpus/example_bundle_queries.json",
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _relative_files(repo_root: Path, patterns: tuple[str, ...]) -> list[Path]:
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(path for path in repo_root.glob(pattern) if path.is_file())
    return sorted(paths, key=lambda path: path.relative_to(repo_root).as_posix())


def _file_group(repo_root: Path, paths: list[Path]) -> dict[str, Any]:
    digest = hashlib.sha256()
    files: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        content = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        files.append({"path": relative, "sha256": _sha256(content), "bytes": len(content)})
    return {"sha256": digest.hexdigest(), "files": files}


def _corpus_group(repo_root: Path) -> dict[str, Any]:
    return _file_group(
        repo_root,
        _relative_files(
            repo_root,
            (
                "benchmarks/corpus/**/*.md",
                "benchmarks/governance/**/*.md",
                "example-bundle/**/*.md",
            ),
        ),
    )


def _query_group(repo_root: Path) -> dict[str, Any]:
    return _file_group(
        repo_root,
        [repo_root / relative for relative in _QUERY_PATHS if (repo_root / relative).is_file()],
    )


def _source_group(repo_root: Path) -> dict[str, Any]:
    return _file_group(
        repo_root,
        _relative_files(repo_root, ("benchmarks/**/*.py", "src/data_olympus/**/*.py")),
    )


def _output_group(repo_root: Path) -> dict[str, Any]:
    return _file_group(
        repo_root,
        [repo_root / relative for relative in _OUTPUT_PATHS if (repo_root / relative).is_file()],
    )


def _dependency_lock(repo_root: Path) -> dict[str, Any]:
    lock_path = repo_root / "uv.lock"
    content = lock_path.read_bytes()
    parsed = tomllib.loads(content.decode("utf-8"))
    packages = sorted(
        (
            {"name": package["name"], "version": package["version"]}
            for package in parsed.get("package", [])
            if package.get("name") in _LOCK_PACKAGES and package.get("version")
        ),
        key=lambda package: package["name"],
    )
    return {
        "path": "uv.lock",
        "sha256": _sha256(content),
        "packages": packages,
    }


def _read_json(repo_root: Path, relative: str) -> dict[str, Any]:
    value = json.loads((repo_root / relative).read_text(encoding="utf-8"))
    return cast("dict[str, Any]", value)


def _matching_row(
    rows: list[dict[str, Any]], **criteria: str
) -> dict[str, Any] | None:
    return next(
        (row for row in rows if all(row.get(key) == value for key, value in criteria.items())),
        None,
    )


def _summary(repo_root: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    synthetic_path = repo_root / "benchmarks/results/results.json"
    if synthetic_path.is_file():
        result = _read_json(repo_root, "benchmarks/results/results.json")
        row = _matching_row(result.get("rows", []), method="data-olympus", category="ALL")
        if row:
            summary["synthetic"] = {
                "method": "data-olympus",
                "mean_tokens": row.get("mean_tokens"),
                "recall_at_k": row.get("recall"),
                "serves_stale": row.get("serves_stale"),
                "tokenizer": result.get("tokenizer"),
                "vector_rag_included": result.get("rag_included"),
            }

    governance_path = repo_root / "benchmarks/governance_results/ablation.json"
    if governance_path.is_file():
        result = _read_json(repo_root, "benchmarks/governance_results/ablation.json")
        row = _matching_row(result.get("rows", []), config="fts+applies_when", stratum="ALL")
        if row:
            summary["governance"] = {
                "config": "fts+applies_when",
                "recall_at_k": row.get("recall"),
                "mean_tokens": row.get("mean_tokens"),
                "n": row.get("n"),
            }

    embeddings_path = repo_root / "benchmarks/governance_results/embeddings/ablation.json"
    if embeddings_path.is_file():
        result = _read_json(
            repo_root, "benchmarks/governance_results/embeddings/ablation.json"
        )
        row = _matching_row(
            result.get("rows", []),
            config="lexical-stack+embeddings",
            stratum="paraphrase_uncovered",
        )
        if row:
            summary["embedding_ablation"] = {
                "config": "lexical-stack+embeddings",
                "stratum": "paraphrase_uncovered",
                "recall_at_k": row.get("recall"),
                "n": row.get("n"),
            }

    real_path = repo_root / "benchmarks/real_corpus/example_bundle_result.json"
    if real_path.is_file():
        result = _read_json(repo_root, "benchmarks/real_corpus/example_bundle_result.json")
        summary["example_bundle"] = {
            key: result.get(key) for key in ("recall@5", "recall@10", "mrr@5")
        }
    return summary


def build_receipt(repo_root: Path, source_commit: str) -> dict[str, object]:
    """Describe all committed benchmark evidence without volatile timestamps."""
    if not _SHA_PATTERN.fullmatch(source_commit):
        raise ValueError("source_commit must be a lowercase 40 character git SHA")
    root = repo_root.resolve()
    return {
        "schema_version": SCHEMA_VERSION,
        "source_commit": source_commit,
        "source_tree": _source_group(root),
        "inputs": {
            "corpora": _corpus_group(root),
            "queries": _query_group(root),
        },
        "dependency_lock": _dependency_lock(root),
        "environment": {
            "python": {
                "implementation": platform.python_implementation(),
                "version": platform.python_version(),
            },
            "platform": platform.platform(),
        },
        "retrieval": {
            "tokenizer": "benchmarks.tokenizer.SimpleTokenizer",
            "tokenizer_identity": "simple",
            "models": [
                {
                    "identity": "BAAI/bge-small-en-v1.5",
                    "scope": "optional embedding ablation only",
                }
            ],
        },
        "commands": [
            "uv run python -m benchmarks.generate_artifacts",
            "uv run python -m benchmarks.generate_governance_artifacts",
            (
                "KB_EMBEDDINGS_MODE=on uv run --extra embeddings python -m "
                "benchmarks.generate_embeddings_ablation"
            ),
            (
                "uv run python -m benchmarks.real_corpus_eval --corpus example-bundle "
                "--queries benchmarks/real_corpus/example_bundle_queries.json "
                "--lexical-only --out "
                "benchmarks/real_corpus/example_bundle_result.json"
            ),
            "uv run python -m benchmarks.docs_tables --write",
            f"uv run python -m benchmarks.receipt write --source-commit {source_commit}",
        ],
        "seeds": {
            "bootstrap": 12345,
            "governance_corpus": 0,
            "synthetic_corpus": 0,
            "token_curve_corpus": 42,
        },
        "outputs": _output_group(root),
        "summary": _summary(root),
    }


def _group_sha(receipt: Mapping[str, object], *path: str) -> str | None:
    value: object = receipt
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    if isinstance(value, Mapping):
        value = value.get("sha256")
    return value if isinstance(value, str) else None


def _verify_source_commit(
    receipt: Mapping[str, object], repo_root: Path, source_commit: str
) -> list[str]:
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return []
    exists = subprocess.run(
        ["git", "cat-file", "-e", f"{source_commit}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
    )
    if exists.returncode != 0:
        return [f"source_commit {source_commit} does not exist in this checkout"]

    source_tree = receipt.get("source_tree")
    files = source_tree.get("files") if isinstance(source_tree, Mapping) else None
    if not isinstance(files, list):
        return ["source_tree.files is missing from the receipt"]
    problems: list[str] = []
    for entry in files:
        if not isinstance(entry, Mapping):
            problems.append("source_tree.files contains an invalid entry")
            continue
        path = entry.get("path")
        expected_sha = entry.get("sha256")
        if not isinstance(path, str) or not isinstance(expected_sha, str):
            problems.append("source_tree.files contains an invalid path or sha256")
            continue
        content = subprocess.run(
            ["git", "show", f"{source_commit}:{path}"],
            cwd=repo_root,
            capture_output=True,
        )
        if content.returncode != 0:
            problems.append(f"source_commit does not contain {path}")
        elif _sha256(content.stdout) != expected_sha:
            problems.append(f"source_commit content differs for {path}")
    return problems


def verify_receipt(receipt: Mapping[str, object], repo_root: Path) -> list[str]:
    """Return every provenance mismatch instead of stopping at the first one."""
    root = repo_root.resolve()
    problems: list[str] = []
    if receipt.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    source_commit = receipt.get("source_commit")
    if not isinstance(source_commit, str) or not _SHA_PATTERN.fullmatch(source_commit):
        problems.append("source_commit must be a lowercase 40 character git SHA")
    else:
        problems.extend(_verify_source_commit(receipt, root, source_commit))

    groups = {
        ("source_tree",): _source_group(root),
        ("inputs", "corpora"): _corpus_group(root),
        ("inputs", "queries"): _query_group(root),
        ("outputs",): _output_group(root),
    }
    for path, current in groups.items():
        if _group_sha(receipt, *path) != current["sha256"]:
            problems.append(f"{'.'.join(path)} sha256 does not match the repository")

    expected_lock = receipt.get("dependency_lock")
    current_lock = _dependency_lock(root)
    if not isinstance(expected_lock, Mapping) or (
        expected_lock.get("sha256") != current_lock["sha256"]
        or expected_lock.get("packages") != current_lock["packages"]
    ):
        problems.append("dependency_lock does not match uv.lock")
    return problems


def write_receipt(repo_root: Path, source_commit: str) -> Path:
    path = repo_root / RECEIPT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_receipt(repo_root, source_commit), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def current_source_commit(repo_root: Path) -> str:
    """Return the exact source revision used for a benchmark execution."""
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    write_parser = subparsers.add_parser("write", help="write the deterministic receipt")
    write_parser.add_argument("--source-commit")
    verify_parser = subparsers.add_parser("verify", help="verify the committed receipt")
    verify_parser.add_argument("--receipt", type=Path, default=RECEIPT_PATH)
    args = parser.parse_args(argv)
    repo_root = Path(__file__).resolve().parent.parent

    if args.command == "write":
        path = write_receipt(
            repo_root, args.source_commit or current_source_commit(repo_root)
        )
        print(f"wrote {path.relative_to(repo_root)}")
        return 0

    receipt = json.loads((repo_root / args.receipt).read_text(encoding="utf-8"))
    problems = verify_receipt(receipt, repo_root)
    if problems:
        for problem in problems:
            print(f"benchmark receipt: {problem}", file=sys.stderr)
        return 1
    print("benchmark receipt: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
