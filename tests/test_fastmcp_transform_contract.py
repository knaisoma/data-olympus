from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

from fastmcp.server.transforms.search import BM25SearchTransform


def test_project_requires_fastmcp_with_search_transform() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]
    assert "fastmcp>=3.4.4,<4" in dependencies


def test_bm25_search_transform_exposes_required_configuration() -> None:
    parameters = inspect.signature(BM25SearchTransform).parameters
    assert {"always_visible", "search_tool_name", "call_tool_name"} <= set(parameters)


def test_bm25_search_transform_documents_hidden_direct_calls() -> None:
    doc = inspect.getdoc(BM25SearchTransform)
    base_doc = inspect.getdoc(BM25SearchTransform.__mro__[1])
    assert doc
    assert base_doc
    assert "Hidden tools remain callable" in base_doc
