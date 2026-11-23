"""kb_suggest_skills (issue #34): instructions that repeat across in-force
documents, so a maintainer can consider capturing them once as a reusable
skill or workflow.

Advisory and human-gated, like kb_curate: this tool RECOMMENDS. It never
proposes, edits, rewrites, promotes or demotes anything, and it has no
write-pipeline dependency at all. Issue #34 asks for suggestions, not an
automatic instruction rewrite.

Detection is deliberately simple and bounded: documents are parsed as
CommonMark, and an "instruction" is a line of paragraph or list-item text of at
least ``_MIN_WORDS`` words. Code (fenced or indented, in any quote or list),
HTML blocks and headings are never instructions. Lines are normalised (case,
markdown markers, spacing and punctuation, but not operators such as ``==`` or
``!=``) and counted once per document, so a document that repeats itself does
not count twice. Only the in-force set is scanned, through the same predicate
kb_search(in_force=True) uses.

The consultation telemetry that issue #34 mentions (issue #32) is not
implemented in this release, and this tool does not use it: repetition across
the corpus is the only signal.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt

from data_olympus.models import SkillSuggestion, SkillSuggestResponse

if TYPE_CHECKING:
    from data_olympus.index import Index

_DEFAULT_LIMIT = 20
_DEFAULT_MIN_DOCUMENTS = 3
_MIN_WORDS = 6
_MAX_PATHS = 10
_MAX_INSTRUCTION_CHARS = 300
# Bounds the work and memory one very large document can cost a call.
_MAX_INSTRUCTIONS_PER_DOCUMENT = 2000

# Only emphasis, quoting and bracket characters are dropped. Operators such as
# ``==``, ``!=``, ``<`` and ``>`` carry meaning ("only when x == y" is not
# "only when x != y"), so they stay in the comparison key.
_DROPPED = re.compile(r"[*`~\"'“”‘’()\[\]{}.,;:?]")
_TRAILING = re.compile(r"[!\s]+$")
_SPACES = re.compile(r"\s+")

# One shared, stateless parser. The CommonMark preset has no tables or other
# extensions, and it does not render anything: only block structure is read.
_MARKDOWN = MarkdownIt("commonmark")

_ADVICE = (
    "Consider capturing this once as a reusable skill or workflow and linking "
    "the documents to it. Review before acting; nothing was changed."
)


def _key(text: str) -> str:
    return _SPACES.sub(" ", _TRAILING.sub("", _DROPPED.sub("", text.lower()))).strip()


def _instructions(body: str) -> dict[str, str]:
    """Normalised key -> first-written text for the instruction lines of one
    document. A repeated line inside the same document yields one key.

    The document is parsed as CommonMark and only the text of paragraphs and
    list items is read, one line at a time. Fenced and indented code, HTML
    blocks and headings are never instructions, whatever quote or list they sit
    in. At most ``_MAX_INSTRUCTIONS_PER_DOCUMENT`` instructions are taken from
    one document."""
    found: dict[str, str] = {}
    tokens = _MARKDOWN.parse(body)
    for index, token in enumerate(tokens):
        if token.type != "inline" or tokens[index - 1].type == "heading_open":
            continue
        for line in token.content.splitlines():
            text = line.strip()
            if not text:
                continue
            key = _key(text)
            if len(key.split()) < _MIN_WORDS:
                continue
            found.setdefault(key, text)
            if len(found) >= _MAX_INSTRUCTIONS_PER_DOCUMENT:
                return found
    return found


def kb_suggest_skills_fn(
    *,
    idx: Index,
    today: str | None = None,
    min_documents: int = _DEFAULT_MIN_DOCUMENTS,
    limit: int = _DEFAULT_LIMIT,
) -> SkillSuggestResponse:
    """Instructions repeated in at least ``min_documents`` distinct in-force
    documents, most repeated first, ties by text. A valid empty result (never
    an error) for an empty corpus or when nothing repeats enough."""
    from data_olympus.format.validate import today_iso

    today = today if today is not None else today_iso()
    # Same clamp shape as kb_curate_fn and kb_search_fn: bound both ends so a
    # negative value cannot reach downstream slicing as "no limit".
    limit = max(1, min(limit, 100))
    min_documents = max(2, min(min_documents, 100))
    seen: dict[str, tuple[str, list[str]]] = {}
    for row in idx.suggest_candidates(today=today):
        path = str(row["path"])
        for key, text in _instructions(str(row["body"])).items():
            seen.setdefault(key, (text, []))[1].append(path)
    ranked = sorted(
        (
            (key, text, paths)
            for key, (text, paths) in seen.items()
            if len(paths) >= min_documents
        ),
        key=lambda item: (-len(item[2]), item[1]),
    )
    suggestions = [
        SkillSuggestion(
            instruction=text[:_MAX_INSTRUCTION_CHARS],
            documents=len(paths),
            paths=sorted(paths)[:_MAX_PATHS],
            suggestion=_ADVICE,
        )
        for _, text, paths in ranked[:limit]
    ]
    return SkillSuggestResponse(suggestions=suggestions, total=len(suggestions))
