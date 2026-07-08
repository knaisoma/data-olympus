"""Walk a bundle directory and validate every concept document."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from .document import Document
from .validate import IN_FORCE_STATUSES, RESERVED, Finding, validate_document

# Directories whose contents are never KB concepts.  Kept in sync with
# _EXCLUDED_DIR_NAMES in src/data_olympus/index.py — if you add entries
# there, add them here too (and vice-versa).
_SKIP_DIRS = frozenset({
    # VCS / tooling
    ".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", "node_modules",
    # Repo-meta / CI
    ".github", ".worktrees",
    # Archival and scratch
    "archive", "_archive", "to-delete",
    # Test data (fixture trees contain intentional duplicates)
    "test-fixtures", "cli-fixtures",
})

# Well-known repo-meta files that may live at the bundle root without being KB
# concepts.  Only files DIRECTLY under the bundle root are skipped; the same
# filename nested inside any subdirectory is a legitimate concept document and
# MUST still be validated (e.g. projects/acme-app/README.md is a project doc).
_ROOT_META_FILES = frozenset({
    "README.md", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md", "SECURITY.md",
    "CHANGELOG.md", "NOTICE.md", "LICENSE.md", "AGENTS.md", "CLAUDE.md",
    "GEMINI.md",
})


def discover_bundle_files(root: str | Path) -> list[Path]:
    """Return the sorted '*.md' files under root that are subject to concept
    linting, i.e. everything `lint_bundle` would validate.

    This is the single source of truth for which files a bundle lints. The CLI
    uses the length of this list to report how many files were actually linted
    and to fail when a bundle has no concepts to lint (otherwise a broken walk
    would silently pass as "0 errors across 0 files").

    Returns only files subject to the concept schema. Skipped:
    - Files inside vendor/VCS/archival/meta directories (_SKIP_DIRS).
    - Well-known repo-meta filenames that sit DIRECTLY at the bundle root
      (_ROOT_META_FILES).  The same filename in a subdirectory is NOT skipped.
    - Reserved filenames (`index.md`, `log.md`, `template.md`), which
      `validate_document` exempts from the concept schema and so can never
      produce a finding.  Counting them as "linted" would let a bundle that has
      lost all its concept docs but kept its generated indexes still pass the
      zero-file guard.
    """
    root = Path(root)
    files: list[Path] = []
    for md in sorted(root.rglob("*.md")):
        # Match skip-dirs only among components INSIDE the bundle. Using the
        # absolute path here would skip the whole bundle whenever an ancestor
        # directory happens to be named like a skip-dir (e.g. a checkout under
        # `.worktrees/`), silently discovering zero files.
        if any(part in _SKIP_DIRS for part in md.relative_to(root).parts):
            continue
        if md.parent == root and md.name in _ROOT_META_FILES:
            continue
        if md.name in RESERVED:
            continue
        files.append(md)
    return files


def lint_files(files: list[Path]) -> dict[Path, list[Finding]]:
    """Validate an already-discovered list of concept files. Returns {path:
    findings} for any file that produced at least one finding.

    Pair with `discover_bundle_files` to lint a bundle in a single traversal.

    In addition to the per-file schema checks (`validate_document`), this
    builds an in-memory id map over `files` and cross-checks the typed
    lifecycle-relationship fields `supersedes` / `superseded_by` / `contradicts`
    (issue #110, slice 1). Cross-file findings only appear here (and via
    `lint_bundle`, which delegates to this function); single-file validation
    via `validate_document` is unaffected.
    """
    results: dict[Path, list[Finding]] = {}
    docs: dict[Path, Document] = {}
    for md in files:
        doc = Document.load(md)
        docs[md] = doc
        findings = validate_document(doc)
        if findings:
            results[md] = list(findings)

    for path, findings in _cross_file_lifecycle_findings(docs).items():
        results.setdefault(path, []).extend(findings)

    return results


# ---------------------------------------------------------------------------
# Cross-file lifecycle-relationship lint (issue #110, slice 1)
# ---------------------------------------------------------------------------
#
# `supersedes` / `superseded_by` / `contradicts` are governance extensions
# (SPEC.md section 4.2) whose targets are stable concept IDs, never paths.
# This pass builds an in-memory id -> path map over the discovered file list
# and cross-checks the raw frontmatter values directly (NOT the lenient,
# already-coerced `ParsedDoc` from `data_olympus.markdown_parse`, which the
# index build uses): lint needs to see the exact authored shape to flag a
# non-string list entry or a wrong container type, which the index's lenient
# coercion silently absorbs by design.


def _path_shaped(target: str) -> bool:
    return "/" in target or target.endswith(".md")


def _normalize_multi_ref(value: object) -> tuple[list[str], bool]:
    """Normalize `supersedes` / `contradicts`: a scalar ID string or a list of
    ID strings. Returns (values, malformed). `malformed` is True when the raw
    shape is anything else (a non-string entry in the list, or a value that is
    neither a string nor a list at all -- e.g. a mapping or a number)."""
    if value is None:
        return [], False
    if isinstance(value, str):
        return ([value], False) if value.strip() else ([], False)
    if isinstance(value, list):
        if all(isinstance(v, str) for v in value):
            return list(value), False
        return [v for v in value if isinstance(v, str)], True
    return [], True


def _normalize_single_ref(value: object) -> tuple[str | None, bool]:
    """Normalize `superseded_by`: a scalar ID string only (never a list).
    Returns (value, malformed)."""
    if value is None:
        return None, False
    if isinstance(value, str):
        return (value, False) if value.strip() else (None, False)
    return None, True


def _cross_file_lifecycle_findings(docs: dict[Path, Document]) -> dict[Path, list[Finding]]:
    findings: dict[Path, list[Finding]] = defaultdict(list)

    # id -> path, only for docs with a usable (non-empty string) id. Docs
    # without one still get shape/dangling/path-shaped checks on their own
    # fields below; they just can't be a cross-reference TARGET or carry the
    # id-keyed relational checks (self-supersession, asymmetry, cycles,
    # contradiction pairs, status warnings).
    id_to_path: dict[str, Path] = {}
    for path, doc in docs.items():
        doc_id = doc.id
        if isinstance(doc_id, str) and doc_id:
            id_to_path.setdefault(doc_id, path)

    supersedes_by_id: dict[str, list[str]] = {}
    superseded_by_by_id: dict[str, str] = {}
    contradicts_by_id: dict[str, list[str]] = {}
    status_by_id: dict[str, str] = {}

    for path, doc in docs.items():
        fm = doc.frontmatter
        supersedes, supersedes_bad = _normalize_multi_ref(fm.get("supersedes"))
        superseded_by, superseded_by_bad = _normalize_single_ref(fm.get("superseded_by"))
        contradicts, contradicts_bad = _normalize_multi_ref(fm.get("contradicts"))

        if supersedes_bad:
            findings[path].append(
                Finding(
                    "error", "supersedes",
                    "malformed 'supersedes' value: expected a concept id string "
                    "or a list of concept id strings",
                )
            )
        if superseded_by_bad:
            findings[path].append(
                Finding(
                    "error", "superseded_by",
                    "malformed 'superseded_by' value: expected a single concept id string",
                )
            )
        if contradicts_bad:
            findings[path].append(
                Finding(
                    "error", "contradicts",
                    "malformed 'contradicts' value: expected a concept id string "
                    "or a list of concept id strings",
                )
            )

        for field, targets in (
            ("supersedes", supersedes),
            ("superseded_by", [superseded_by] if superseded_by else []),
            ("contradicts", contradicts),
        ):
            for target in targets:
                if _path_shaped(target):
                    findings[path].append(
                        Finding(
                            "warning", field,
                            f"'{field}' value {target!r} looks like a file path, not a "
                            "stable concept id; use the target document's `id` instead",
                        )
                    )
                elif target not in id_to_path:
                    findings[path].append(
                        Finding(
                            "warning", field,
                            f"'{field}' references unknown id {target!r} "
                            "(not found in this bundle)",
                        )
                    )

        doc_id = doc.id
        if isinstance(doc_id, str) and doc_id:
            supersedes_by_id[doc_id] = supersedes
            if superseded_by:
                superseded_by_by_id[doc_id] = superseded_by
            contradicts_by_id[doc_id] = contradicts
            status_by_id[doc_id] = str(doc.status or "").strip()

    # --- self-supersession (error) ------------------------------------------
    for doc_id, targets in supersedes_by_id.items():
        if doc_id in targets:
            findings[id_to_path[doc_id]].append(
                Finding("error", "supersedes", f"'{doc_id}' cannot supersede itself")
            )
    for doc_id, target in superseded_by_by_id.items():
        if target == doc_id:
            findings[id_to_path[doc_id]].append(
                Finding("error", "superseded_by", f"'{doc_id}' cannot be superseded by itself")
            )

    # --- supersession cycles (error) ----------------------------------------
    # Merge both fields into one "A supersedes B" directed graph: a
    # `superseded_by` on B naming A is the mirror of an implicit "A supersedes
    # B" edge. Self-edges are excluded (reported above instead) and targets
    # not present in the bundle are excluded (a dead end can't be part of a
    # cycle, and it's already reported as dangling above).
    graph: dict[str, set[str]] = defaultdict(set)
    for doc_id, targets in supersedes_by_id.items():
        for target in targets:
            if target != doc_id and target in id_to_path:
                graph[doc_id].add(target)
    for doc_id, target in superseded_by_by_id.items():
        if target != doc_id and target in id_to_path:
            graph[target].add(doc_id)

    for cycle in _find_cycles(graph):
        member_ids = cycle[:-1]
        chain = " -> ".join(cycle)
        for member_id in member_ids:
            findings[id_to_path[member_id]].append(
                Finding("error", "supersedes", f"supersession cycle detected: {chain}")
            )

    # --- asymmetric pairs (warning) ------------------------------------------
    for doc_id, targets in supersedes_by_id.items():
        for target in targets:
            if target == doc_id or target not in id_to_path:
                continue
            if superseded_by_by_id.get(target) != doc_id:
                findings[id_to_path[doc_id]].append(
                    Finding(
                        "warning", "supersedes",
                        f"'{doc_id}' supersedes '{target}' but '{target}' does not "
                        f"list 'superseded_by: {doc_id}'",
                    )
                )
    for doc_id, target in superseded_by_by_id.items():
        if target == doc_id or target not in id_to_path:
            continue
        if doc_id not in supersedes_by_id.get(target, []):
            findings[id_to_path[doc_id]].append(
                Finding(
                    "warning", "superseded_by",
                    f"'{doc_id}' is superseded_by '{target}' but '{target}' does not "
                    f"list 'supersedes: {doc_id}'",
                )
            )

    # --- status consistency (warning) ---------------------------------------
    for doc_id in superseded_by_by_id:
        status = status_by_id.get(doc_id, "")
        if status.casefold() in IN_FORCE_STATUSES:
            findings[id_to_path[doc_id]].append(
                Finding(
                    "warning", "superseded_by",
                    f"'{doc_id}' has 'superseded_by' set but status {status!r} is "
                    "in-force",
                )
            )
    for doc_id, status in status_by_id.items():
        if status.casefold() == "superseded" and doc_id not in superseded_by_by_id:
            findings[id_to_path[doc_id]].append(
                Finding(
                    "warning", "status",
                    f"'{doc_id}' has status 'superseded' but no 'superseded_by' is set",
                )
            )

    # --- in-force contradiction pairs (warning) ------------------------------
    reported_pairs: set[frozenset[str]] = set()
    for doc_id, targets in contradicts_by_id.items():
        if status_by_id.get(doc_id, "").casefold() not in IN_FORCE_STATUSES:
            continue
        for target in targets:
            if target == doc_id or target not in id_to_path:
                continue
            if status_by_id.get(target, "").casefold() not in IN_FORCE_STATUSES:
                continue
            pair = frozenset({doc_id, target})
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)
            findings[id_to_path[doc_id]].append(
                Finding("warning", "contradicts", f"'{doc_id}' contradicts in-force doc '{target}'")
            )
            if id_to_path[target] != id_to_path[doc_id]:
                findings[id_to_path[target]].append(
                    Finding(
                        "warning", "contradicts",
                        f"'{target}' is contradicted by in-force doc '{doc_id}'",
                    )
                )

    return findings


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Return every cycle found in `graph` (a directed "A supersedes B" graph)
    as a list of node ids from the cycle's start back to itself, e.g.
    ``["A", "B", "A"]``. Standard white/gray/black DFS cycle detection."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)  # defaults to WHITE (0)
    stack: list[str] = []
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for neighbor in sorted(graph.get(node, ())):
            if color[neighbor] == WHITE:
                visit(neighbor)
            elif color[neighbor] == GRAY:
                idx = stack.index(neighbor)
                cycles.append([*stack[idx:], neighbor])
        stack.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color[node] == WHITE:
            visit(node)
    return cycles


def lint_bundle(root: str | Path) -> dict[Path, list[Finding]]:
    """Validate every concept '*.md' under root. Returns {path: findings} for any
    file that produced at least one finding.

    File discovery (which files are validated vs skipped) is delegated to
    `discover_bundle_files`.
    """
    return lint_files(discover_bundle_files(root))
