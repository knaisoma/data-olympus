"""Append-only JSONL audit log of all writes."""
from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


class AuditLog:
    def __init__(self, *, log_path: str) -> None:
        self._path = log_path

    def append(self, event: dict[str, Any]) -> None:
        """Append a single JSON object as one line. Best-effort atomicity.

        The parent directory is created lazily on first append so that
        constructing an AuditLog with an unwritable default path (e.g. macOS
        tests that never trigger a write) does not crash startup.
        """
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        line = json.dumps(event, ensure_ascii=False)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def iter_filtered(
        self,
        *,
        since: float | None = None,
        agent: str | None = None,
        status: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield events matching the filters, most-recent first."""
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None and float(ev.get("ts", 0)) < since:
                continue
            if agent is not None and ev.get("agent_identity") != agent:
                continue
            if status is not None and ev.get("status") != status:
                continue
            yield ev
