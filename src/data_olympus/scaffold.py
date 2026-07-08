"""`data-olympus init`: scaffold a new knowledge bundle (issue #66).

Generates the tier directories, a root ``index.md`` carrying the format-version
frontmatter, an authoring ``template.md``, and one example concept document per
SPEC-supported ``type`` (including ``memory`` and ``reference``, which the
hand-authored ``example-bundle/`` historically lacked). The generated
supersession pair and ``applies_when`` metadata are real, not decorative: they
are exactly what `SPEC.md` section 4.2 documents and what `kb search` demos
depend on, so a freshly scaffolded bundle demonstrates those features without
any hand-authoring.

The generated bundle is lint-clean by construction (see `format/validate.py`):
every concept document carries all four required fields plus every
recommended field, so `data-olympus lint` reports zero errors and zero
warnings on a fresh scaffold.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from data_olympus.cli.indexgen import regenerate_indexes

if TYPE_CHECKING:
    from collections.abc import Sequence

SPEC_VERSION = "0.1"
OKF_VERSION = "0.1"

# The six top-level tier directories `--tiers` selects among (issue #66's
# sketch). Order is both the CLI default and the order tiers are scaffolded
# and listed in the root index.
ALL_TIERS: tuple[str, ...] = (
    "universal", "tech-stacks", "projects", "decisions", "workflows", "tooling",
)
DEFAULT_TIERS: tuple[str, ...] = ALL_TIERS


class ScaffoldError(Exception):
    """Raised for a refused or invalid `init` invocation. Message is user-facing."""


@dataclass(frozen=True)
class ScaffoldResult:
    root: Path
    tiers: tuple[str, ...]
    files_written: tuple[Path, ...]
    indexes_written: tuple[Path, ...]


def _validate_tiers(tiers: Sequence[str] | None) -> tuple[str, ...]:
    if tiers is None:
        return DEFAULT_TIERS
    selected: list[str] = []
    for raw in tiers:
        name = raw.strip()
        if not name:
            continue
        if name not in ALL_TIERS:
            raise ScaffoldError(
                f"unknown tier '{name}' (allowed: {', '.join(ALL_TIERS)})"
            )
        if name not in selected:
            selected.append(name)
    if not selected:
        raise ScaffoldError("--tiers produced an empty tier list")
    return tuple(selected)


def _prepare_dest(dest: Path) -> None:
    if dest.exists():
        if not dest.is_dir():
            raise ScaffoldError(f"{dest} exists and is not a directory")
        if any(dest.iterdir()):
            raise ScaffoldError(
                f"{dest} is not empty; refusing to scaffold into an existing "
                "directory (no --force in this slice)"
            )
    else:
        dest.mkdir(parents=True)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _doc(
    *,
    id_: str,
    type_: str,
    status: str,
    tier: str,
    title: str,
    description: str,
    tags: list[str],
    timestamp: str,
    body: str,
    applies_when: list[str] | None = None,
    supersedes: list[str] | None = None,
    superseded_by: str | None = None,
) -> str:
    lines = [
        "---",
        f"id: {id_}",
        f"type: {type_}",
        f"status: {status}",
        f"tier: {tier}",
        f"title: {title}",
        f"description: {description}",
        f"tags: [{', '.join(tags)}]",
        f'timestamp: "{timestamp}"',
    ]
    if supersedes:
        lines.append(f"supersedes: [{', '.join(supersedes)}]")
    if superseded_by:
        lines.append(f"superseded_by: {superseded_by}")
    if applies_when:
        lines.append("applies_when:")
        lines.extend(f'  - "{phrase}"' for phrase in applies_when)
    lines.append("---")
    return "\n".join(lines) + "\n" + body.rstrip() + "\n"


def _root_index(selected: tuple[str, ...]) -> str:
    tier_lines = "\n".join(f"- `{t}/`" for t in selected)
    return (
        "---\n"
        f'spec_version: "{SPEC_VERSION}"\n'
        f'okf_version: "{OKF_VERSION}"\n'
        "---\n"
        "# Knowledge bundle\n\n"
        "Scaffolded by `data-olympus init`. This bundle demonstrates every "
        "SPEC-supported concept `type` (`standard`, `decision`, `workflow`, "
        "`project`, `memory`, `reference`), a real `superseded` / "
        "`superseded_by` / `supersedes` chain, and `applies_when` retrieval "
        "trigger metadata, so `kb search` and `kb lint` both have something "
        "to demo against out of the box. Replace the example documents with "
        "your own content; delete the ones you don't need.\n\n"
        "Run `data-olympus lint .` after editing, and `data-olympus index .` "
        "to regenerate the per-directory `index.md` navigation files.\n\n"
        "## Tier directories in this bundle\n\n"
        f"{tier_lines}\n\n"
        "See [template.md](template.md) for the frontmatter fields a new "
        "concept document needs, and `SPEC.md` in the data-olympus repo for "
        "the full format specification.\n"
    )


def _template_doc() -> str:
    # template.md is a reserved filename (format/validate.py RESERVED): it is
    # exempt from the concept schema, so the placeholder values below are
    # never linted. It exists purely as an authoring aid.
    return (
        "---\n"
        "id: <STABLE-ID>                # e.g. STD-U-001, ADR-007; decoupled from path\n"
        "type: <standard|decision|workflow|project|memory|reference>\n"
        "status: <draft|active|deprecated|superseded|proposed|accepted|rejected>\n"
        "tier: <T1|T2|T3|T4|meta>\n"
        "title: <short human-readable name>\n"
        "description: <one or two sentences; used in generated indexes and search>\n"
        "tags: [<lowercase>, <keywords>]\n"
        'timestamp: "<YYYY-MM-DD>"\n'
        "applies_when:                  # recommended for standard/decision docs\n"
        '  - "<coding intent this document governs, e.g. writing a migration>"\n'
        "# supersedes: [<ID>]           # optional: IDs this document replaces\n"
        "# superseded_by: <ID>          # optional: set when status is superseded\n"
        "# owner: <team-or-individual>  # optional\n"
        "---\n\n"
        "# <Title>\n\n"
        "## Purpose\n\n"
        "What this concept governs and why it exists.\n\n"
        "## Rule / Content\n\n"
        "The actual guidance, decision, workflow steps, or reference material.\n\n"
        "## See also\n\n"
        "Links to related concepts, using bundle-relative paths.\n"
    )


def _write_universal_tier(dest: Path) -> list[Path]:
    base = dest / "universal" / "foundation"
    old = _doc(
        id_="STD-INIT-001", type_="standard", status="superseded", tier="T1",
        title="Example Standard (superseded)",
        description=(
            "Placeholder standard superseded by STD-INIT-002. Demonstrates the "
            "superseded/superseded_by chain; replace with your own content."
        ),
        tags=["example", "superseded"], timestamp="2026-01-01",
        superseded_by="STD-INIT-002",
        body=(
            "# Example superseded standard\n\n"
            "This document demonstrates the `superseded` / `superseded_by` chain "
            "`data-olympus` tracks (see `SPEC.md` section 4.2). It is superseded "
            "by [STD-INIT-002](STD-INIT-002-example-standard.md).\n\n"
            "Replace both documents with your own standards once you have real "
            "ones to record, or delete them.\n"
        ),
    )
    new = _doc(
        id_="STD-INIT-002", type_="standard", status="active", tier="T1",
        title="Example Standard (current)",
        description=(
            "Placeholder active standard that supersedes STD-INIT-001. "
            "Demonstrates applies_when trigger metadata; replace with your own content."
        ),
        tags=["example"], timestamp="2026-01-01",
        supersedes=["STD-INIT-001"],
        applies_when=[
            "writing a new standard or policy document",
            "deciding whether to supersede an existing standard",
        ],
        body=(
            "# Example active standard\n\n"
            "This document supersedes "
            "[STD-INIT-001](STD-INIT-001-example-standard.md) and demonstrates "
            "`applies_when`, the highest-weight indexed field in full-text "
            "search (see `SPEC.md` section 4.2). Replace this content with "
            "your team's actual standard.\n"
        ),
    )
    return [
        _write(base / "STD-INIT-001-example-standard.md", old),
        _write(base / "STD-INIT-002-example-standard.md", new),
    ]


def _write_tech_stacks_tier(dest: Path) -> list[Path]:
    base = dest / "tech-stacks" / "example-stack"
    doc = _doc(
        id_="STD-INIT-STACK-001", type_="standard", status="active", tier="T2",
        title="Example Stack Convention",
        description=(
            "Placeholder stack-specific convention; replace with a real "
            "tech-stack standard."
        ),
        tags=["example", "tech-stack"], timestamp="2026-01-01",
        body=(
            "# Example stack convention\n\n"
            "Stack-specific standards (tier `T2`) live under `tech-stacks/"
            "<stack-name>/`, one directory per stack. Replace this file with "
            "your stack's actual conventions.\n"
        ),
    )
    return [_write(base / "STD-INIT-STACK-001-example-convention.md", doc)]


def _write_projects_tier(dest: Path) -> list[Path]:
    base = dest / "projects" / "example-project"
    doc = _doc(
        id_="PROJ-INIT-example-project", type_="project", status="active", tier="T3",
        title="Example Project",
        description=(
            "Placeholder project-scoped concept; replace with your actual "
            "project's knowledge."
        ),
        tags=["example", "project"], timestamp="2026-01-01",
        body=(
            "# Example project\n\n"
            "Project-scoped concepts (tier `T3`) live under `projects/"
            "<project-name>/`; component-scoped concepts (tier `T4`) live under "
            "`projects/<project-name>/components/<component-name>/`. Replace "
            "this file with your actual project overview.\n"
        ),
    )
    return [_write(base / "README.md", doc)]


def _write_decisions_tier(dest: Path) -> list[Path]:
    base = dest / "decisions"
    doc = _doc(
        id_="ADR-INIT-001", type_="decision", status="accepted", tier="meta",
        title="Example Architectural Decision",
        description="Placeholder decision record; replace with a real ADR.",
        tags=["example", "architecture"], timestamp="2026-01-01",
        body=(
            "# Context\n\n"
            "Describe the problem that prompted this decision.\n\n"
            "# Decision\n\n"
            "State the decision made.\n\n"
            "# Consequences\n\n"
            "Describe the tradeoffs accepted. Replace this placeholder with a "
            "real ADR.\n"
        ),
    )
    return [_write(base / "ADR-INIT-001-example-decision.md", doc)]


def _write_workflows_tier(dest: Path) -> list[Path]:
    base = dest / "workflows"
    doc = _doc(
        id_="WF-INIT-001", type_="workflow", status="active", tier="meta",
        title="Example Workflow",
        description="Placeholder step-by-step process; replace with a real workflow.",
        tags=["example", "workflow"], timestamp="2026-01-01",
        body=(
            "# Purpose\n\n"
            "Describe what this workflow accomplishes.\n\n"
            "# Steps\n\n"
            "1. Replace this with the first real step.\n"
            "2. Add as many steps as needed.\n"
        ),
    )
    return [_write(base / "WF-INIT-001-example-workflow.md", doc)]


def _write_tooling_tier(dest: Path) -> list[Path]:
    base = dest / "tooling"
    reference = _doc(
        id_="REF-INIT-001", type_="reference", status="active", tier="meta",
        title="Example Reference",
        description=(
            "Placeholder lookup reference (current-state facts, not a governing "
            "rule); replace with real reference material."
        ),
        tags=["example", "reference"], timestamp="2026-01-01",
        body=(
            "# Example reference\n\n"
            "A `reference` document describes current state for lookup, not a "
            "rule that governs future work (contrast with `standard`). Replace "
            "this with a real lookup table, endpoint list, or similar.\n"
        ),
    )
    memory = _doc(
        id_="MEM-INIT-001", type_="memory", status="active", tier="meta",
        title="Example Memory",
        description=(
            "Placeholder recorded incident or one-off learning, not a general "
            "standard; replace with a real memory entry or delete it."
        ),
        tags=["example", "memory"], timestamp="2026-01-01",
        body=(
            "# What happened\n\n"
            "Record a specific incident or one-off learning here.\n\n"
            "# Why this is memory, not a standard\n\n"
            "A `memory` document records something true about one situation, "
            "not a general rule every project needs. If the same issue recurs "
            "across multiple contexts, promote it into a `standard` instead.\n"
        ),
    )
    return [
        _write(base / "REF-INIT-001-example-reference.md", reference),
        _write(base / "MEM-INIT-001-example-memory.md", memory),
    ]


_TIER_WRITERS = {
    "universal": _write_universal_tier,
    "tech-stacks": _write_tech_stacks_tier,
    "projects": _write_projects_tier,
    "decisions": _write_decisions_tier,
    "workflows": _write_workflows_tier,
    "tooling": _write_tooling_tier,
}


def scaffold_bundle(dest: Path, tiers: Sequence[str] | None = None) -> ScaffoldResult:
    """Scaffold a new knowledge bundle at `dest`.

    `dest` is created if it does not exist. Raises `ScaffoldError` if `dest`
    exists and is non-empty, if `dest` exists and is not a directory, or if
    `tiers` names an unknown tier. `tiers` defaults to `DEFAULT_TIERS` (all
    six tier directories).
    """
    dest = Path(dest)
    selected = _validate_tiers(tiers)
    _prepare_dest(dest)

    written: list[Path] = [
        _write(dest / "index.md", _root_index(selected)),
        _write(dest / "template.md", _template_doc()),
    ]
    for tier in selected:
        written.extend(_TIER_WRITERS[tier](dest))

    indexes = regenerate_indexes(dest)

    return ScaffoldResult(
        root=dest,
        tiers=selected,
        files_written=tuple(written),
        indexes_written=tuple(indexes),
    )
