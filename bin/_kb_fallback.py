#!/usr/bin/env python3
"""Local-grep fallback for bin/kb when the data-olympus-mcp endpoint is unreachable.

Walks the local KB checkout, uses ripgrep (or grep) to satisfy search/get/list/outline/health,
and emits the same JSON shape as the REST endpoint with `degraded: true` set.

Used by bin/kb subcommands when curl to KB_ENDPOINT fails. Not intended for direct invocation
but works standalone too.

Usage:
    _kb_fallback.py search <query> [--limit N] [--tier T] [--category C]
    _kb_fallback.py get <id>
    _kb_fallback.py list <tier> [<category>]
    _kb_fallback.py outline
    _kb_fallback.py health

Environment:
    KB_LOCAL_PATH    Path to the local KB checkout (default: current directory)
    KB_ENDPOINT      The REST endpoint that was unreachable (for the warning message)
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_KB_LOCAL_PATH = Path.cwd()
DEFAULT_ENDPOINT = "http://localhost:8080"

_EXCLUDED = {".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".venv",
             "__pycache__", "node_modules", ".worktrees", "to-delete",
             "archive", "_archive", "data-olympus-mcp",
             "test-fixtures", "cli-fixtures"}

# Inline minimal front-matter parser (subset of markdown_parse for the package; bin/ has no deps).
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LIST_RE = re.compile(r"^\[(.*)\]$")

# Mirror of src/data_olympus/index.py _DEFAULT_PATH_RULES and its
# KB_TAXONOMY_PATH loader. If you update one, update the other. See SPEC.md.
_DEFAULT_PATH_RULES: list[tuple[str, str, str]] = [
    # T1 Universal, applies to every project, every stack.
    ("universal/foundation/",       "T1", "foundation"),
    ("universal/quality/",          "T1", "quality"),
    ("universal/security/",         "T1", "security"),
    ("universal/infrastructure/",   "T1", "infrastructure"),
    ("universal/database/",         "T1", "database"),
    ("universal/api/",              "T1", "api"),
    ("universal/services/",         "T1", "services"),

    # T2 Stack-specific, classified dynamically: tech-stacks/<stack>/...
    ("tech-stacks/",                 "T2", "stack"),

    # Meta tiers (kept distinct from T1-T4).
    ("decisions/",                   "decisions", "decisions"),
    ("workflows/",                   "workflows", "workflows"),
    ("memory/inbox/",                "memory",    "memory-inbox"),
    ("memory/accepted/",             "memory",    "memory-accepted"),
    ("memory/",                      "memory",    "memory"),
    ("tooling/",                     "tooling",   "tooling"),
    ("templates/",                   "templates", "templates"),

    # T3 / T4 catch-all (project tree). The classifier post-processes
    # this hit; see _classify for the T3 vs T4 distinction.
    ("projects/",                    "T3", "project"),
]


def _load_path_rules() -> list[tuple[str, str, str]]:
    """Active taxonomy: KB_TAXONOMY_PATH JSON if set, else the default.

    The JSON must be a list of [prefix, tier, category] triples; a malformed
    file raises ValueError rather than silently misclassifying every document.
    """
    path = os.environ.get("KB_TAXONOMY_PATH", "").strip()
    if not path:
        return _DEFAULT_PATH_RULES
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
        isinstance(r, (list, tuple)) and len(r) == 3 for r in data
    ):
        raise ValueError(
            f"KB_TAXONOMY_PATH={path!r} must be a JSON list of "
            f"[prefix, tier, category] triples"
        )
    return [(str(r[0]), str(r[1]), str(r[2])) for r in data]


def _classify(rel: str) -> tuple[str, str]:
    """Return (tier, category) inferred from the relative path.

    Returns ('meta', 'meta') if no rule matches.
    Within projects/, distinguishes T4 component paths from T3 project paths
    by looking for the literal 'components' segment after the project name.
    tech-stacks/<stack>/... is classified dynamically as stack:<stack>.
    """
    norm = rel.replace("\\", "/")
    for prefix, tier, category in _load_path_rules():
        if norm.startswith(prefix):
            if prefix == "projects/":
                parts = norm.split("/")
                # projects/<name>/components/<component>/<file>... -> T4
                # Requires len >= 5 so parts[3] is a real component DIRECTORY
                # (not a loose file directly inside components/).
                if len(parts) >= 5 and parts[2] == "components":
                    return "T4", f"component:{parts[1]}/{parts[3]}"
                # projects/<name>/... (incl. components/ with no component yet) -> T3
                if len(parts) >= 2:
                    # Strip .md so projects/index.md -> project:index
                    # (rather than project:index.md). For real project dirs
                    # like projects/example-project/ this is a no-op.
                    name = parts[1].removesuffix(".md")
                    return "T3", f"project:{name}"
            if prefix == "tech-stacks/":
                parts = norm.split("/")
                # tech-stacks/<stack>/<file>... -> stack:<stack>
                if len(parts) >= 2 and parts[1]:
                    return tier, f"{category}:{parts[1].removesuffix('.md')}"
            return tier, category
    return "meta", "meta"


def _parse_md(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    m = _FM_RE.match(text)
    fm: dict[str, object] = {}
    body = text
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            lm = _LIST_RE.match(v)
            if lm:
                fm[k] = [p.strip().strip("'\"") for p in lm.group(1).split(",") if p.strip()]
            else:
                fm[k] = v.strip("'\"")
        body = text[m.end():]
    return {"front_matter": fm, "body": body, "full_text": text}


def _iter_md_files(kb: Path) -> list[Path]:
    return sorted(
        p for p in kb.rglob("*.md")
        if not any(part in _EXCLUDED for part in p.relative_to(kb).parts)
    )


def _classify_doc(rel: str, fm: dict[str, object]) -> tuple[str, str, str, str]:
    """Returns (id, title, tier, category) with front-matter override on tier/category."""
    path_tier, path_category = _classify(rel)
    fm_id = fm.get("id") or "-".join(Path(rel).with_suffix("").parts)
    fm_title = fm.get("title") or ""
    fm_tier = fm.get("tier") or path_tier
    fm_category = fm.get("category") or path_category
    return str(fm_id), str(fm_title), str(fm_tier), str(fm_category)


def _mtime_iso(path: Path) -> str:
    mt = path.stat().st_mtime
    return datetime.datetime.fromtimestamp(mt, tz=datetime.UTC).isoformat()


def _warning(kb: Path, endpoint: str, ranker: str) -> str:
    return f"MCP unreachable at {endpoint}; using local {ranker} fallback over {kb}"


def _ranker() -> str:
    """Return 'rg' or 'grep' depending on which is on PATH."""
    if subprocess.run(["which", "rg"], capture_output=True).returncode == 0:
        return "rg"
    return "grep"


def cmd_health(*, kb_local_path: Path, endpoint: str) -> str:
    files = _iter_md_files(kb_local_path)
    out = {
        "kb_commit": "fallback",
        "index_built_at": None,
        "total_rules": len(files),
        "last_git_pull_at": None,
        "last_git_push_at": None,
        "staleness_seconds": None,
        "degraded": True,
        "db_size_bytes": 0,
        "pending_count": 0,
        "push_queue_size": 0,
        "last_index_build_status": "ok",
        "last_index_error": None,
        "last_index_error_at": None,
        "last_index_conflicts": [],
        "warning": _warning(kb_local_path, endpoint, _ranker()),
    }
    return json.dumps(out)


def cmd_outline(*, kb_local_path: Path, endpoint: str) -> str:
    tiers: dict[str, dict[str, int]] = {}
    for f in _iter_md_files(kb_local_path):
        rel = str(f.relative_to(kb_local_path))
        parsed = _parse_md(f)
        _, _, tier, category = _classify_doc(rel, parsed["front_matter"])
        tiers.setdefault(tier, {}).setdefault(category, 0)
        tiers[tier][category] += 1
    tier_list = [
        {"name": t, "categories": [{"name": c, "count": n} for c, n in cats.items()]}
        for t, cats in sorted(tiers.items())
    ]
    return json.dumps({
        "tiers": tier_list,
        "source_commit": "fallback",
        "degraded": True,
        "warning": _warning(kb_local_path, endpoint, _ranker()),
    })


def cmd_search(*, query: str, limit: int, kb_local_path: Path, endpoint: str,
               tier: str | None = None, category: str | None = None) -> str:
    ranker = _ranker()
    if ranker == "rg":
        result = subprocess.run(
            ["rg", "--files-with-matches", "--ignore-case", query, str(kb_local_path)],
            capture_output=True, text=True,
        )
    else:
        result = subprocess.run(
            ["grep", "-rilE", query, str(kb_local_path)],
            capture_output=True, text=True,
        )
    raw_paths = [Path(p) for p in result.stdout.splitlines() if p.endswith(".md")]
    hits = []
    for p in sorted(raw_paths):
        rel = str(p.relative_to(kb_local_path))
        if any(part in _EXCLUDED for part in p.relative_to(kb_local_path).parts):
            continue
        parsed = _parse_md(p)
        id_, title, t, c = _classify_doc(rel, parsed["front_matter"])
        if tier and t != tier:
            continue
        if category and c != category:
            continue
        # Tiny snippet: first 160 chars of body containing the query (case-insensitive)
        body = parsed["body"]
        idx = body.lower().find(query.lower())
        snippet = body[max(0, idx - 40):idx + 120] if idx >= 0 else body[:160]
        hits.append({
            "id": id_,
            "path": rel,
            "title": title,
            "snippet": snippet,
            "score": 0.0,  # fallback has no FTS5 ranking
        })
        if len(hits) >= limit:
            break
    return json.dumps({
        "query": query,
        "hits": hits,
        "source_commit": "fallback",
        "total_returned": len(hits),
        "degraded": True,
        "warning": _warning(kb_local_path, endpoint, ranker),
    })


def cmd_get(*, id: str, kb_local_path: Path, endpoint: str) -> str:
    # Linear scan; fallback is slow by design
    for f in _iter_md_files(kb_local_path):
        rel = str(f.relative_to(kb_local_path))
        parsed = _parse_md(f)
        doc_id, title, tier, category = _classify_doc(rel, parsed["front_matter"])
        if doc_id == id:
            tags_raw = parsed["front_matter"].get("tags") or []
            tags = list(tags_raw) if isinstance(tags_raw, list) else []
            return json.dumps({
                "id": id,
                "path": rel,
                "title": title,
                "tier": tier,
                "category": category,
                "tags": tags,
                "content_markdown": parsed["full_text"],
                "last_modified": _mtime_iso(f),
                "last_modified_source": "mtime-fallback",
                "source_commit": "fallback",
                "degraded": True,
                "warning": _warning(kb_local_path, endpoint, _ranker()),
            })
    return json.dumps({
        "error": "not_found",
        "message": f"no document with id={id!r}",
        "degraded": True,
        "warning": _warning(kb_local_path, endpoint, _ranker()),
    })


def cmd_list(*, tier: str, category: str | None, kb_local_path: Path, endpoint: str) -> str:
    entries = []
    for f in sorted(_iter_md_files(kb_local_path)):
        rel = str(f.relative_to(kb_local_path))
        parsed = _parse_md(f)
        doc_id, title, t, c = _classify_doc(rel, parsed["front_matter"])
        if t != tier:
            continue
        if category and c != category:
            continue
        entries.append({"id": doc_id, "title": title, "path": rel})
    entries.sort(key=lambda e: e["id"])
    return json.dumps({
        "tier": tier,
        "category": category,
        "entries": entries,
        "source_commit": "fallback",
        "total": len(entries),
        "degraded": True,
        "warning": _warning(kb_local_path, endpoint, _ranker()),
    })


def main() -> int:
    parser = argparse.ArgumentParser(prog="_kb_fallback.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--tier")
    p_search.add_argument("--category")
    p_get = sub.add_parser("get")
    p_get.add_argument("id")
    p_list = sub.add_parser("list")
    p_list.add_argument("tier")
    p_list.add_argument("category", nargs="?")
    sub.add_parser("outline")
    sub.add_parser("health")
    args = parser.parse_args()
    kb = Path(os.environ.get("KB_LOCAL_PATH", str(DEFAULT_KB_LOCAL_PATH)))
    endpoint = os.environ.get("KB_ENDPOINT", DEFAULT_ENDPOINT)
    if args.cmd == "search":
        print(cmd_search(query=args.query, limit=args.limit,
                         kb_local_path=kb, endpoint=endpoint,
                         tier=args.tier, category=args.category))
    elif args.cmd == "get":
        print(cmd_get(id=args.id, kb_local_path=kb, endpoint=endpoint))
    elif args.cmd == "list":
        print(cmd_list(tier=args.tier, category=args.category,
                       kb_local_path=kb, endpoint=endpoint))
    elif args.cmd == "outline":
        print(cmd_outline(kb_local_path=kb, endpoint=endpoint))
    elif args.cmd == "health":
        print(cmd_health(kb_local_path=kb, endpoint=endpoint))
    return 0


if __name__ == "__main__":
    sys.exit(main())
