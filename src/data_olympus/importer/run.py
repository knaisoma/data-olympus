"""Importer orchestrator: dispatch by kind, write drafts, lint, build report.

The importer NEVER touches git. It writes files into the output bundle dir and
returns an ImportReport. Re-run safety: by default it REFUSES to write into an
output dir that already contains importer-authored drafts (a marker file records
prior runs); ``force=True`` overwrites. This makes re-runs deterministic and
never silently duplicates or clobbers.
"""

from __future__ import annotations

from pathlib import Path

from data_olympus.format import discover_bundle_files, lint_files
from data_olympus.format.frontmatter import parse_frontmatter

from . import adr as adr_mod
from . import flat as flat_mod
from . import okf as okf_mod
from .model import DraftDoc, ImportError_, ImportReport, LintFinding, SkippedSection
from .stamp import (
    IdAllocator,
    assert_known_vocab,
    build_frontmatter,
    first_sentence,
    normalize_tier,
    render_document,
    slugify,
    tags_from_text,
    title_from_heading,
)

KINDS = ("claude-md", "agents-md", "gemini-md", "cursorrules", "adr", "okf")

# Marker written into an output dir the importer created/wrote to. Its presence
# is what "refuse-on-rerun" keys off of, so a re-run is detected even if the
# operator deletes the drafts but keeps the dir.
_MARKER = ".data-olympus-import"

# Default id prefixes per kind (overridable with --id-prefix).
_DEFAULT_PREFIX = {
    "claude-md": "CLAUDE",
    "agents-md": "AGENTS",
    "gemini-md": "GEMINI",
    "cursorrules": "CURSOR",
    "okf": "OKF",
}

_DEDUP_NEXT_STEPS = (
    "Review each draft, then activate with a status change once vetted.",
    "To converge with an EXISTING knowledge base instead of duplicating it, run "
    "the dedup pass: call the kb_cleanup_plan MCP tool (or POST "
    "/api/v1/onboarding/cleanup-plan) with these files as local_files. It "
    "classifies each draft as imported_duplicate / partial_overlap / unique and "
    "proposes thin-pointer replacements for exact duplicates.",
    "The importer wrote drafts only; nothing was committed to git and nothing "
    "was activated.",
)


def _existing_ids(out_dir: Path) -> set[str]:
    """Collect ids already present under ``out_dir`` so we never collide."""
    ids: set[str] = set()
    if not out_dir.is_dir():
        return ids
    for md in out_dir.rglob("*.md"):
        try:
            fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        doc_id = fm.get("id")
        if isinstance(doc_id, str) and doc_id:
            ids.add(doc_id)
    return ids


def _check_rerun(out_dir: Path, *, force: bool) -> None:
    """Enforce refuse-on-rerun unless ``force``.

    Refuses when the marker exists OR the dir already holds any front-mattered
    ``.md`` file (so importing into a hand-authored bundle without --force is
    also refused, protecting existing content from silent clobber)."""
    if force:
        return
    marker = out_dir / _MARKER
    if marker.exists():
        raise ImportError_(
            f"refusing to re-import: {out_dir} was already used as an import target "
            f"(marker {_MARKER!r} present). Re-run with --force to overwrite, or "
            f"choose a fresh --out directory."
        )
    if out_dir.is_dir():
        for md in out_dir.rglob("*.md"):
            try:
                fm, _ = parse_frontmatter(md.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if fm:
                raise ImportError_(
                    f"refusing to write into non-empty bundle {out_dir} "
                    f"(found governed file {md.name!r}). Use a fresh --out directory "
                    f"or pass --force to overwrite."
                )


def _default_out(source: Path, kind: str) -> Path:
    """Derive a default output dir next to the source when --out is omitted."""
    base = source.parent if source.is_file() else source
    return base / f"imported-{kind}"


def _flat_drafts(
    source: Path, *, kind: str, tier: str, category: str | None, id_prefix: str, existing: set[str]
) -> tuple[list[DraftDoc], list[SkippedSection]]:
    text = source.read_text(encoding="utf-8")
    sections = flat_mod.split_flat(text)
    alloc = IdAllocator(id_prefix, existing)
    drafts: list[DraftDoc] = []
    skipped: list[SkippedSection] = []
    used_names: set[str] = set()
    for sec in sections:
        if sec.body_len < flat_mod.MIN_BODY_CHARS:
            skipped.append(
                SkippedSection(
                    heading=sec.heading,
                    reason=f"body too short ({sec.body_len} < {flat_mod.MIN_BODY_CHARS} chars)",
                )
            )
            continue
        doc_id = alloc.next()
        title = title_from_heading(sec.heading, fallback=doc_id)
        description = first_sentence(sec.body) or title
        tags = tags_from_text(sec.heading + "\n" + sec.body, fallback=kind.replace("-md", ""))
        fm = build_frontmatter(
            doc_id=doc_id,
            doc_type="standard",
            status="draft",
            tier=tier,
            title=title,
            description=description,
            tags=tags,
            category=category,
        )
        stem = _unique_stem(slugify(title, fallback=doc_id.lower()), used_names)
        drafts.append(
            DraftDoc(
                filename=f"{stem}.md",
                frontmatter=fm,
                body=sec.body,
                inferences=[f"{doc_id}: title from heading {sec.heading!r}"],
            )
        )
    return drafts, skipped


def _adr_drafts(
    source: Path, *, tier: str, category: str | None, existing: set[str]
) -> tuple[list[DraftDoc], list[SkippedSection]]:
    files = adr_mod.discover_adr_files(source)
    if not files:
        raise ImportError_(
            f"no adr-tools files (NNNN-title.md) found under {source}. "
            f"Point --kind adr at a doc/adr directory."
        )
    alloc = IdAllocator("ADR", existing)
    drafts: list[DraftDoc] = []
    used_names: set[str] = set()
    for f in files:
        parsed = adr_mod.parse_adr_file(f)
        if parsed is None:  # pragma: no cover - discover already filtered
            continue
        alloc.reserve(parsed.doc_id)
        extra: dict[str, object] = {}
        if parsed.supersedes:
            extra["supersedes"] = (
                parsed.supersedes if len(parsed.supersedes) > 1 else parsed.supersedes[0]
            )
        if parsed.superseded_by:
            extra["superseded_by"] = parsed.superseded_by
        fm = build_frontmatter(
            doc_id=parsed.doc_id,
            doc_type="decision",
            status=parsed.status,
            tier=tier,
            title=parsed.title,
            description=adr_mod.description_for(parsed),
            tags=tags_from_text(parsed.title + "\n" + parsed.body, fallback="decision"),
            category=category,
            extra=extra or None,
        )
        stem = _unique_stem(f"{parsed.doc_id.lower()}-{parsed.slug}", used_names)
        drafts.append(
            DraftDoc(
                filename=f"{stem}.md",
                frontmatter=fm,
                body=parsed.body,
                inferences=parsed.inferences,
                needs_review=parsed.needs_review,
            )
        )
    return drafts, []


def _okf_drafts(
    source: Path, *, tier: str, category: str | None, existing: set[str]
) -> tuple[list[DraftDoc], list[SkippedSection]]:
    files = okf_mod.discover_okf_files(source)
    if not files:
        raise ImportError_(f"no markdown files to normalize under {source}")
    drafts: list[DraftDoc] = []
    used_names: set[str] = set()
    seen_ids: set[str] = set(existing)
    for f in files:
        norm = okf_mod.normalize_okf_doc(f, default_tier=tier, category=category)
        doc_id = str(norm.frontmatter["id"])
        needs_review = list(norm.needs_review)
        if doc_id in seen_ids:
            needs_review.append(
                f"{f.name}: id {doc_id!r} collides with another imported/existing doc; "
                f"resolve before activation"
            )
        seen_ids.add(doc_id)
        stem = _unique_stem(slugify(doc_id, fallback=f.stem), used_names)
        drafts.append(
            DraftDoc(
                filename=f"{stem}.md",
                frontmatter=norm.frontmatter,
                body=norm.body,
                inferences=[f"{f.name}: {note}" for note in norm.inferences],
                needs_review=needs_review,
            )
        )
    return drafts, []


def _unique_stem(stem: str, used: set[str]) -> str:
    """Ensure a filename stem is unique within this run."""
    candidate = stem or "concept"
    n = 1
    while candidate in used:
        n += 1
        candidate = f"{stem}-{n}"
    used.add(candidate)
    return candidate


def run_import(
    *,
    source: str | Path,
    kind: str,
    tier: str,
    out: str | Path | None = None,
    category: str | None = None,
    id_prefix: str | None = None,
    force: bool = False,
) -> ImportReport:
    """Import ``source`` of the given ``kind`` into a governed draft bundle.

    Returns an ImportReport. Raises ImportError_ on bad input or a refused
    re-run. Never commits to git.
    """
    assert_known_vocab()
    if kind not in KINDS:
        raise ImportError_(f"unknown --kind {kind!r} (allowed: {', '.join(KINDS)})")
    try:
        tier = normalize_tier(tier)
    except ValueError as exc:
        raise ImportError_(str(exc)) from exc

    source_path = Path(source)
    if not source_path.exists():
        raise ImportError_(f"source not found: {source_path}")

    out_dir = Path(out) if out else _default_out(source_path, kind)
    _check_rerun(out_dir, force=force)

    existing = _existing_ids(out_dir)

    if kind in ("claude-md", "agents-md", "gemini-md", "cursorrules"):
        if not source_path.is_file():
            raise ImportError_(f"--kind {kind} expects a file, got directory {source_path}")
        prefix = id_prefix or _DEFAULT_PREFIX[kind]
        drafts, skipped = _flat_drafts(
            source_path, kind=kind, tier=tier, category=category,
            id_prefix=prefix, existing=existing,
        )
    elif kind == "adr":
        drafts, skipped = _adr_drafts(
            source_path, tier=tier, category=category, existing=existing
        )
    else:  # okf
        drafts, skipped = _okf_drafts(
            source_path, tier=tier, category=category, existing=existing
        )

    if not drafts:
        raise ImportError_(
            f"no importable concepts found in {source_path} (every candidate section "
            f"was too short or empty). Nothing written."
        )

    report = ImportReport(
        kind=kind, source=str(source_path), out_dir=str(out_dir), skipped=skipped
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for draft in drafts:
        dest = out_dir / draft.filename
        dest.write_text(render_document(draft.frontmatter, draft.body), encoding="utf-8")
        report.created.append(draft.filename)
        for note in draft.inferences:
            report.inferences.append(note)
        for note in draft.needs_review:
            report.needs_review.append(note)
        # Flag any non-draft status (ADR-derived) for human attention.
        status = draft.frontmatter.get("status")
        if status and status != "draft":
            report.needs_review.append(
                f"{draft.filename}: landed with status {status!r} (not draft); "
                f"verify before activation"
            )

    # Record the marker so a future re-run without --force is refused.
    (out_dir / _MARKER).write_text(
        f"imported kind={kind} source={source_path}\n", encoding="utf-8"
    )

    # Lint the output over exactly the files we wrote (plus any pre-existing).
    linted = discover_bundle_files(out_dir)
    findings = lint_files(linted)
    for path in sorted(findings):
        rel = path.relative_to(out_dir)
        for f in findings[path]:
            report.lint.append(
                LintFinding(path=str(rel), severity=f.severity, field=f.field, message=f.message)
            )

    report.next_steps = list(_DEDUP_NEXT_STEPS)
    return report
