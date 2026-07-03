"""data-olympus: governance-grade knowledge-base format, CLI, and MCP server."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: the installed distribution's version (declared in
    # pyproject [project].version). Reading it here keeps `data_olympus.__version__`
    # from drifting out of sync with the packaging metadata the release chain tags on.
    __version__ = _pkg_version("data-olympus")
except PackageNotFoundError:  # pragma: no cover - only when running from a raw tree
    __version__ = "0.0.0+unknown"
