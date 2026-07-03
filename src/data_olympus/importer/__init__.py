"""Importer: migrate existing agent-rule corpora into a governed draft bundle.

Public API:
- ``run_import`` — orchestrate an import, returning an ``ImportReport``.
- ``ImportReport`` / ``DraftDoc`` — result and per-doc models.
- ``ImportError_`` — raised on bad input or a refused re-run.
- ``KINDS`` — the supported source kinds.
"""

from .model import DraftDoc, ImportError_, ImportReport
from .run import KINDS, run_import

__all__ = ["run_import", "ImportReport", "DraftDoc", "ImportError_", "KINDS"]
