"""Tests for kb_suggest_skills (issue #34): instructions repeated across
in-force documents, reported as advice only. Like kb_curate, the tool
recommends and never proposes, edits, promotes or demotes anything.
"""
from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest

from data_olympus.index import Index
from data_olympus.tools_suggest import kb_suggest_skills_fn

if TYPE_CHECKING:
    from pathlib import Path

TODAY = "2026-07-08"
RULE = "Always run the full test suite before you open a pull request"


def _write(kb: Path, rel: str, *, id_: str, body: str, status: str = "active") -> None:
    p = kb / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\nid: {id_}\ntype: standard\nstatus: {status}\ntier: T1\n"
        f"category: foundation\ntitle: {id_}\n---\n# {id_}\n\n{body}\n",
        encoding="utf-8",
    )


def _idx(tmp_path: Path, tmp_index_path: Path, builder) -> Index:
    kb = tmp_path / "kb"
    kb.mkdir()
    builder(kb)
    idx = Index(tmp_index_path)
    idx.build(kb, source_commit="test")
    return idx


def test_empty_index_returns_empty_result_not_error(tmp_index_path) -> None:
    resp = kb_suggest_skills_fn(idx=Index(tmp_index_path), today=TODAY)
    assert resp.suggestions == []
    assert resp.total == 0


def test_repeated_instruction_is_suggested_despite_formatting(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        _write(kb, "a.md", id_="DOC-A", body=f"- {RULE}.")
        _write(kb, "b.md", id_="DOC-B", body=f"1. {RULE.lower()}")
        _write(kb, "c.md", id_="DOC-C", body=f"> **{RULE}**!")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY)
    assert resp.total == 1
    entry = resp.suggestions[0]
    assert entry.documents == 3
    assert entry.paths == ["a.md", "b.md", "c.md"]
    assert entry.instruction.lower().startswith("always run the full test suite")
    assert "nothing was changed" in entry.suggestion


def test_below_the_document_threshold_is_not_suggested(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        _write(kb, "a.md", id_="DOC-A", body=f"- {RULE}")
        _write(kb, "b.md", id_="DOC-B", body=f"- {RULE}")

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []
    assert kb_suggest_skills_fn(idx=idx, today=TODAY, min_documents=2).total == 1


def test_one_document_repeating_itself_counts_once(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        _write(kb, "a.md", id_="DOC-A", body=f"- {RULE}\n- {RULE}\n- {RULE}")
        _write(kb, "b.md", id_="DOC-B", body=f"- {RULE}")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY, min_documents=2)
    assert resp.suggestions[0].documents == 2


def test_code_fences_headings_and_short_lines_are_ignored(tmp_path, tmp_index_path) -> None:
    body = f"## {RULE}\n\n```\n{RULE}\n```\n\n- Run the tests\n"

    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []


@pytest.mark.parametrize(
    "body",
    [
        f"````\n```\n{RULE}\n```\n````\n",
        f"~~~~\n```\n{RULE}\n```\n~~~~\n",
        f"```\n~~~\n{RULE}\n~~~\n```\n",
        f"```\n{RULE}\n",
    ],
    ids=["longer-backtick", "tilde-around-backticks", "backtick-around-tildes", "unterminated"],
)
def test_fenced_contents_are_never_suggested(tmp_path, tmp_index_path, body) -> None:
    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []


@pytest.mark.parametrize(
    "body",
    [
        f"> ```\n> {RULE}\n> ```\n",
        f"- ```\n  {RULE}\n  ```\n",
        f"1. ```text\n   {RULE}\n   ```\n",
        f"- item\n  - ~~~~\n    {RULE}\n    ~~~~\n",
        f"Some text.\n\n    {RULE}\n",
        f"Some text.\n\n\t{RULE}\n",
    ],
    ids=["quote-fence", "list-fence", "numbered-fence", "nested-tilde-fence", "indented", "tab"],
)
def test_code_inside_containers_and_indented_code_are_never_suggested(
    tmp_path, tmp_index_path, body,
) -> None:
    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []


@pytest.mark.parametrize(
    "body",
    [
        f">     {RULE}\n",
        f"- item\n\n      {RULE}\n",
        f"Some text.\n\n    - {RULE}\n",
        f"```\n- ```\n{RULE}\n```\n",
        f"```\n> ```\n{RULE}\n```\n",
        f"<div>\n{RULE}\n</div>\n",
        f"# {RULE}\n",
    ],
    ids=[
        "indented-in-quote", "indented-in-list", "indented-bullet",
        "literal-list-fence-inside-fence", "literal-quote-fence-inside-fence",
        "html-block", "heading",
    ],
)
def test_markdown_edge_cases_are_never_suggested(tmp_path, tmp_index_path, body) -> None:
    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []


def test_an_instruction_after_a_quoted_fence_ends_is_still_found(tmp_path, tmp_index_path) -> None:
    body = f"> ```\n> code\n\n{RULE}\n"

    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).total == 1


def test_deeply_nested_list_items_are_still_instructions(tmp_path, tmp_index_path) -> None:
    body = f"- one\n    - two\n        - {RULE}\n"

    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).total == 1


def test_text_after_a_closed_fence_is_scanned_again(tmp_path, tmp_index_path) -> None:
    body = f"````\ncode\n````\n\n- {RULE}\n"

    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).total == 1


def test_opposite_comparisons_are_not_conflated(tmp_path, tmp_index_path) -> None:
    same = "Deploy only when the validation result is == success"
    other = "Deploy only when the validation result is != success"

    def builder(kb):
        _write(kb, "a.md", id_="DOC-A", body=f"- {same}")
        _write(kb, "b.md", id_="DOC-B", body=f"- {other}")

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY, min_documents=2).suggestions == []


def test_out_of_force_documents_are_never_reported(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        _write(kb, "a.md", id_="DOC-A", body=f"- {RULE}")
        _write(kb, "b.md", id_="DOC-B", body=f"- {RULE}")
        _write(kb, "old.md", id_="DOC-OLD", body=f"- {RULE}", status="superseded")

    idx = _idx(tmp_path, tmp_index_path, builder)
    assert kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions == []
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY, min_documents=2)
    assert resp.suggestions[0].paths == ["a.md", "b.md"]


def test_most_repeated_first_and_limit_applies(tmp_path, tmp_index_path) -> None:
    other = "Review the changelog entry with the maintainers before every release"

    def builder(kb):
        for name in ("a", "b", "c", "d"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=f"- {RULE}")
        for name in ("e", "f", "g"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=f"- {other}")

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY)
    assert [s.documents for s in resp.suggestions] == [4, 3]
    assert kb_suggest_skills_fn(idx=idx, today=TODAY, limit=1).total == 1
    assert kb_suggest_skills_fn(idx=idx, today=TODAY, limit=-5).total == 1


def test_paths_are_capped_at_ten(tmp_path, tmp_index_path) -> None:
    def builder(kb):
        for n in range(12):
            _write(kb, f"d{n:02d}.md", id_=f"DOC-{n:02d}", body=f"- {RULE}")

    idx = _idx(tmp_path, tmp_index_path, builder)
    entry = kb_suggest_skills_fn(idx=idx, today=TODAY).suggestions[0]
    assert entry.documents == 12
    assert len(entry.paths) == 10


def test_one_huge_document_is_bounded(tmp_path, tmp_index_path) -> None:
    from data_olympus.tools_suggest import _MAX_INSTRUCTIONS_PER_DOCUMENT, _instructions

    lines = (f"- instruction number {n} must be followed in every case" for n in range(2500))
    body = "\n".join(lines)
    assert len(_instructions(body)) == _MAX_INSTRUCTIONS_PER_DOCUMENT

    def builder(kb):
        for name in ("a", "b", "c"):
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY, limit=100)
    assert resp.total == 100


def test_document_text_beyond_the_character_cap_is_not_read(tmp_path, tmp_index_path) -> None:
    from data_olympus.index import SUGGEST_BODY_CHARS

    padding = "x " * (SUGGEST_BODY_CHARS // 2)
    LATE = "Never skip the review step when the change is large"

    def builder(kb):
        for name in ("a", "b", "c"):
            body = f"- {RULE}\n\n```\n{padding}\n```\n\n- {LATE}"
            _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=body)

    idx = _idx(tmp_path, tmp_index_path, builder)
    rows = idx.suggest_candidates(today=TODAY)
    assert rows
    assert all(len(str(r["body"])) <= SUGGEST_BODY_CHARS for r in rows)
    resp = kb_suggest_skills_fn(idx=idx, today=TODAY)
    assert [s.instruction for s in resp.suggestions] == [RULE]  # the later rule is past the cap


def test_fn_signature_has_no_write_dependency() -> None:
    params = set(inspect.signature(kb_suggest_skills_fn).parameters)
    assert not (params & {"pending", "worktrees", "push_queue", "audit_log"})


@pytest.mark.asyncio
async def test_tool_round_trips_through_the_server(tmp_path: Path) -> None:
    from fastmcp import Client

    from data_olympus.server import build_app

    kb = tmp_path / "kb"
    kb.mkdir()
    for name in ("a", "b", "c"):
        _write(kb, f"{name}.md", id_=f"DOC-{name.upper()}", body=f"- {RULE}")
    app = build_app(
        kb_main_path=kb, kb_index_path=tmp_path / "idx.db", sync_interval_sec=60,
        staleness_degraded_sec=600, bootstrap_now=True,
    )
    async with Client(app) as client:
        result = await client.call_tool("kb_suggest_skills", {})
    assert result.data["total"] == 1
    assert result.data["suggestions"][0]["documents"] == 3
