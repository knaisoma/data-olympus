"""Walk a bundle directory and validate every concept document."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from .document import Document
from .validate import (
    IN_FORCE_STATUSES,
    RESERVED,
    RETIRED_STATUSES,
    TIERS,
    Finding,
    is_expired,
    is_inbox_path,
    is_retired,
    is_upcoming,
    normalize_validity_date,
    today_iso,
    validate_document,
)

if TYPE_CHECKING:
    from collections.abc import Collection

# Directories whose contents are never KB concepts.  Kept in sync with
# _EXCLUDED_DIR_NAMES in src/data_olympus/index.py: if you add entries
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


def lint_files(
    files: list[Path],
    *,
    resolve_ids: Collection[str] = (),
    unresolved_severity: str = "error",
    root: str | Path | None = None,
    path_rules: tuple[tuple[str, str, str], ...] | None = None,
    today: str | None = None,
) -> dict[Path, list[Finding]]:
    """Validate an already-discovered list of concept files. Returns {path:
    findings} for any file that produced at least one finding.

    Pair with `discover_bundle_files` to lint a bundle in a single traversal.

    In addition to the per-file schema checks (`validate_document`), this
    builds an in-memory id map over `files` and cross-checks the typed
    lifecycle-relationship fields `supersedes` / `superseded_by` / `contradicts`
    (issue #110, slice 1) and `derived_from` (issue #300). Cross-file
    findings only appear here (and via `lint_bundle`, which delegates to this
    function); single-file validation
    via `validate_document` is unaffected.

    ``resolve_ids`` (issue #259) adds ids that count as existing for
    `supersedes` / `superseded_by` / `derived_from` targets without
    contributing any findings or relationship context (``--resolve-root``).
    ``unresolved_severity`` is ``"error"`` (format 0.4) or ``"warn"``
    (transitional) for an unresolved `supersedes` / `superseded_by` /
    `derived_from` target only.

    ``root`` (issue #304) is the bundle root the files were discovered under.
    When given, a declared ``tier`` or ``category`` that disagrees with the
    path taxonomy is reported as a warning. ``path_rules`` is the taxonomy to
    use; ``None`` loads the active one (``KB_TAXONOMY_PATH`` or the default).

    ``today`` (ISO date, issue #300) drives every wall-clock check, defaulting
    to the real date. Those checks are always warnings.
    """
    today = today if today is not None else today_iso()
    results: dict[Path, list[Finding]] = {}
    docs: dict[Path, Document] = {}
    for md in files:
        doc = Document.load(md)
        docs[md] = doc
        findings = validate_document(doc, today=today)
        if findings:
            results[md] = list(findings)

    for path, findings in _cross_file_lifecycle_findings(
        docs, resolve_ids=resolve_ids, unresolved_severity=unresolved_severity,
        today=today, root=Path(root) if root is not None else None,
    ).items():
        results.setdefault(path, []).extend(findings)

    if root is not None:
        for path, findings in _taxonomy_findings(docs, Path(root), path_rules).items():
            results.setdefault(path, []).extend(findings)

    return results


# ---------------------------------------------------------------------------
# Frontmatter tier/category vs path taxonomy (issue #304)
# ---------------------------------------------------------------------------
#
# The index honours a declared `tier` / `category` over the path-derived value
# (an intentional override), but the write blocklist (KB_WRITE_BLOCK_TIERS)
# classifies a target by path alone. A disagreement is therefore reported, as
# a warning only, so authors see which tier governs writes to the document.
# The path taxonomy also assigns meta tiers (`decisions`, `memory`, ...) that
# the frontmatter vocabulary can only spell as `meta`, so `meta` satisfies any
# path tier outside that vocabulary.


def _taxonomy_findings(
    docs: dict[Path, Document],
    root: Path,
    path_rules: tuple[tuple[str, str, str], ...] | None,
) -> dict[Path, list[Finding]]:
    # Lazy import: the index module imports this package.
    from data_olympus.index import _classify_by_path, _load_path_rules

    rules = path_rules if path_rules is not None else _load_path_rules()
    findings: dict[Path, list[Finding]] = defaultdict(list)
    for path, doc in docs.items():
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        path_tier, path_category = _classify_by_path(rel, rules)
        tier = doc.frontmatter.get("tier")
        # An invalid tier is already an enum error; do not pile on.
        if isinstance(tier, str) and tier in TIERS:
            expected = path_tier if path_tier in TIERS else "meta"
            if tier != expected:
                findings[path].append(Finding(
                    "warning", "tier",
                    f"frontmatter tier {tier!r} disagrees with the path taxonomy, "
                    f"which implies tier {path_tier!r}; the index uses {tier!r}, but "
                    "the write blocklist (KB_WRITE_BLOCK_TIERS) classifies by path, "
                    f"so writes to this document are governed as {path_tier!r}",
                ))
        category = doc.frontmatter.get("category")
        if isinstance(category, str) and category and category != path_category:
            findings[path].append(Finding(
                "warning", "category",
                f"frontmatter category {category!r} disagrees with the path "
                f"taxonomy, which implies category {path_category!r}; the index "
                f"uses {category!r}",
            ))
    return findings


# ---------------------------------------------------------------------------
# Cross-file lifecycle-relationship lint (issue #110, slice 1)
# ---------------------------------------------------------------------------
#
# `supersedes` / `superseded_by` / `contradicts` / `derived_from` are
# governance extensions (SPEC.md section 4.2) whose targets are stable concept IDs, never paths.
# This pass builds an in-memory id -> path map over the discovered file list
# and cross-checks the raw frontmatter values directly (NOT the lenient,
# already-coerced `ParsedDoc` from `data_olympus.markdown_parse`, which the
# index build uses): lint needs to see the exact authored shape to flag a
# non-string list entry or a wrong container type, which the index's lenient
# coercion silently absorbs by design.


def _path_shaped(target: str) -> bool:
    return "/" in target or target.endswith(".md")


def _normalize_multi_ref(value: object) -> tuple[list[str], bool]:
    """Normalize `supersedes` / `contradicts` / `derived_from`: a scalar ID
    string or a list of
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


def _cross_file_lifecycle_findings(
    docs: dict[Path, Document],
    *,
    resolve_ids: Collection[str] = (),
    unresolved_severity: str = "error",
    today: str | None = None,
    root: Path | None = None,
) -> dict[Path, list[Finding]]:
    findings: dict[Path, list[Finding]] = defaultdict(list)
    external = set(resolve_ids)
    today = today if today is not None else today_iso()

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
    derived_from_by_id: dict[str, list[str]] = {}
    status_by_id: dict[str, str] = {}
    in_force_by_id: dict[str, bool] = {}
    valid_until_by_id: dict[str, str] = {}

    for path, doc in docs.items():
        fm = doc.frontmatter
        supersedes, supersedes_bad = _normalize_multi_ref(fm.get("supersedes"))
        superseded_by, superseded_by_bad = _normalize_single_ref(fm.get("superseded_by"))
        contradicts, contradicts_bad = _normalize_multi_ref(fm.get("contradicts"))
        derived_from, derived_from_bad = _normalize_multi_ref(fm.get("derived_from"))

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
        if derived_from_bad:
            findings[path].append(
                Finding(
                    "error", "derived_from",
                    "malformed 'derived_from' value: expected a concept id string "
                    "or a list of concept id strings",
                )
            )

        for field, targets in (
            ("supersedes", supersedes),
            ("superseded_by", [superseded_by] if superseded_by else []),
            ("contradicts", contradicts),
            ("derived_from", derived_from),
        ):
            for target in targets:
                # External ids (--resolve-root) count for supersession and
                # `derived_from` (issue #300); `contradicts` resolution is
                # unchanged (issue #259 scope).
                resolves = target in id_to_path or (
                    field != "contradicts" and target in external)
                shaped = _path_shaped(target)
                if resolves:
                    if shaped:
                        findings[path].append(Finding(
                            "warning", field,
                            f"'{field}' value {target!r} looks like a file path, not a "
                            "stable concept id; use the target document's `id` instead",
                        ))
                    continue
                if field == "contradicts":
                    findings[path].append(Finding(
                        "warning", field,
                        (f"'{field}' value {target!r} looks like a file path, not a "
                         "stable concept id; use the target document's `id` instead")
                        if shaped else
                        f"'{field}' references unknown id {target!r} (not found in this bundle)",
                    ))
                    continue
                # Issue #259 (format 0.4): an unresolved supersession target
                # retires nothing, so it is an error unless downgraded. An
                # unresolved `derived_from` target tracks nothing and follows
                # the same rule (issue #300, format 0.5).
                severity: Literal["error", "warning"] = (
                    "warning" if unresolved_severity == "warn" else "error")
                hint = ("; it looks like a file path, use the target document's `id`"
                        if shaped else "")
                findings[path].append(Finding(
                    severity, field,
                    f"'{field}' references unknown id {target!r} (not found in this "
                    f"bundle{hint}; pass --resolve-root to resolve against a larger "
                    "bundle, or --unresolved-targets warn while fixing a backlog)",
                ))

        doc_id = doc.id
        if isinstance(doc_id, str) and doc_id:
            supersedes_by_id[doc_id] = supersedes
            if superseded_by:
                superseded_by_by_id[doc_id] = superseded_by
            contradicts_by_id[doc_id] = contradicts
            derived_from_by_id[doc_id] = derived_from
            status_by_id[doc_id] = str(doc.status or "").strip()
            valid_from, valid_until = _lint_window(fm.get("validity"))
            valid_until_by_id[doc_id] = valid_until
            in_force_by_id[doc_id] = _lint_in_force(
                status_by_id[doc_id], valid_from, valid_until, today,
                inbox=root is not None and _under_inbox(path, root),
            )

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

    _derived_from_findings(
        findings, id_to_path,
        derived_from_by_id=derived_from_by_id,
        supersedes_by_id=supersedes_by_id,
        superseded_by_by_id=superseded_by_by_id,
        status_by_id=status_by_id,
        in_force_by_id=in_force_by_id,
        valid_until_by_id=valid_until_by_id,
        today=today,
    )
    return findings


def _lint_window(validity: object) -> tuple[str, str]:
    """``(valid_from, valid_until)`` as ISO dates, ``""`` when absent. A
    malformed ``validity`` block is treated as absent, as the index does (it
    already carries its own warning)."""
    if not isinstance(validity, dict):
        return "", ""
    valid_from, bad_from = normalize_validity_date(validity.get("valid_from"))
    valid_until, bad_until = normalize_validity_date(validity.get("valid_until"))
    if bad_from or bad_until:
        return "", ""
    return valid_from, valid_until


def _under_inbox(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return False
    return is_inbox_path(rel)


def _lint_in_force(
    status: str, valid_from: str, valid_until: str, today: str, *, inbox: bool,
) -> bool:
    """Lint's view of "in force" for `derived_from` surfacing (issue #300):
    status class plus validity window, with the memory-inbox floor applied
    when the bundle root is known. Graph exclusion is applied separately by
    the caller (an in-force superseder retires its target)."""
    if inbox or status.casefold() not in IN_FORCE_STATUSES:
        return False
    return not is_expired(valid_until, today) and not is_upcoming(valid_from, today)


def _derived_from_findings(
    findings: dict[Path, list[Finding]],
    id_to_path: dict[str, Path],
    *,
    derived_from_by_id: dict[str, list[str]],
    supersedes_by_id: dict[str, list[str]],
    superseded_by_by_id: dict[str, str],
    status_by_id: dict[str, str],
    in_force_by_id: dict[str, bool],
    valid_until_by_id: dict[str, str],
    today: str,
) -> None:
    """Relational `derived_from` checks (issue #300).

    Errors: self-reference, a derivation cycle (its own graph, never merged
    with supersession: the relations mean different things), and deriving from
    a document this one supersedes (its own `supersedes`, or the target's
    `superseded_by` naming it). Warnings, on both ends: an in-force document
    deriving from a retired one. The retired test is
    :func:`format.validate.is_retired`; lint learns graph exclusion from a
    `supersedes` edge whose source lint considers in force. An id resolved only
    through ``--resolve-root`` has no status here and produces no finding.
    These warnings are wall-clock relative (expiry), so they are never errors.
    """
    # --- self-reference (error) ----------------------------------------------
    for doc_id, targets in derived_from_by_id.items():
        if doc_id in targets:
            findings[id_to_path[doc_id]].append(
                Finding("error", "derived_from", f"'{doc_id}' cannot derive from itself")
            )

    # --- derivation cycles (error) -------------------------------------------
    graph: dict[str, set[str]] = defaultdict(set)
    for doc_id, targets in derived_from_by_id.items():
        for target in targets:
            if target != doc_id and target in id_to_path:
                graph[doc_id].add(target)
    for cycle in _find_cycles(graph):
        chain = " -> ".join(cycle)
        for member_id in cycle[:-1]:
            findings[id_to_path[member_id]].append(
                Finding("error", "derived_from", f"derivation cycle detected: {chain}")
            )

    # --- deriving from a document this one supersedes (error) ---------------
    mixed: set[tuple[str, str]] = set()
    for doc_id, targets in derived_from_by_id.items():
        for target in sorted(set(targets)):
            if target == doc_id:
                continue
            if target in supersedes_by_id.get(doc_id, []) or (
                    superseded_by_by_id.get(target) == doc_id):
                mixed.add((doc_id, target))
                findings[id_to_path[doc_id]].append(Finding(
                    "error", "derived_from",
                    f"'{doc_id}' derives from '{target}' but also supersedes it; a "
                    "successor is not a dependent of the document it retires, so "
                    f"remove '{target}' from 'derived_from'",
                ))

    # --- in-force dependents of retired sources (warning, both ends) --------
    in_force_superseders: dict[str, list[str]] = defaultdict(list)
    for doc_id, targets in supersedes_by_id.items():
        if not in_force_by_id.get(doc_id, False):
            continue
        for target in targets:
            if target != doc_id and target in id_to_path:
                in_force_superseders[target].append(doc_id)

    def retirement_reason(source: str) -> str | None:
        raw_status = status_by_id.get(source, "")
        status = raw_status.casefold()
        superseders = sorted(in_force_superseders.get(source, []))
        valid_until = valid_until_by_id.get(source, "")
        if not is_retired(status, valid_until, today, graph_excluded=bool(superseders)):
            return None
        if status in RETIRED_STATUSES:
            return f"status '{raw_status}'"
        if superseders:
            return f"superseded by in-force '{superseders[0]}'"
        return f"expired: valid_until {valid_until} is before {today}"

    dependents_by_source: dict[str, set[str]] = defaultdict(set)
    for doc_id, targets in sorted(derived_from_by_id.items()):
        if not in_force_by_id.get(doc_id, False) or doc_id in in_force_superseders:
            continue
        for source in sorted(set(targets)):
            if source == doc_id or source not in id_to_path or (doc_id, source) in mixed:
                continue
            reason = retirement_reason(source)
            if reason is None:
                continue
            dependents_by_source[source].add(doc_id)
            findings[id_to_path[doc_id]].append(Finding(
                "warning", "derived_from",
                f"'{doc_id}' is in force but derives from '{source}', which is no "
                f"longer in force ({reason}); decide whether '{doc_id}' still holds, "
                "needs rewording, or should be retired",
            ))
    for source, dependents in sorted(dependents_by_source.items()):
        named = ", ".join(f"'{d}'" for d in sorted(dependents))
        findings[id_to_path[source]].append(Finding(
            "warning", "derived_from",
            f"'{source}' is no longer in force ({retirement_reason(source)}) but "
            f"in-force documents derive from it: {named}; review them",
        ))


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


def lint_bundle(
    root: str | Path,
    *,
    resolve_ids: Collection[str] = (),
    unresolved_severity: str = "error",
) -> dict[Path, list[Finding]]:
    """Validate every concept '*.md' under root. Returns {path: findings} for any
    file that produced at least one finding.

    File discovery (which files are validated vs skipped) is delegated to
    `discover_bundle_files`. The path-taxonomy check (issue #304) runs against
    the active taxonomy; a malformed ``KB_TAXONOMY_PATH`` raises ValueError.
    """
    return lint_files(discover_bundle_files(root), resolve_ids=resolve_ids,
                      unresolved_severity=unresolved_severity, root=root)


def collect_ids(root: str | Path) -> set[str]:
    """Authored ids of every concept file discovered under ``root`` (issue #259).

    Used for existence-only resolution (``--resolve-root``): these files add no
    findings and no relationship context to a lint run."""
    ids: set[str] = set()
    for md in discover_bundle_files(root):
        try:
            doc_id = Document.load(md).id
        except (OSError, ValueError):
            # An unreadable external document cannot vouch for any id; it is
            # outside the linted set, so it is skipped rather than reported.
            continue
        if isinstance(doc_id, str) and doc_id:
            ids.add(doc_id)
    return ids
