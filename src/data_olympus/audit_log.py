"""Append-only JSONL audit log of all writes, with a tamper-evident hash chain.

Each appended event carries an ``event_id``, the ``prev_hash`` of the previous
chained event, and its own ``hash`` over the canonical event body (which includes
``prev_hash``). Any later edit/deletion/reordering of an event breaks the chain
and is detected by :meth:`verify`. With ``KB_AUDIT_HMAC_KEY`` set the digest is a
keyed HMAC-SHA256, so an attacker who can write the log file still cannot forge a
valid chain without the key. Legacy lines without a ``hash`` are tolerated: the
chain validates the hashed lines in order and ignores unhashed ones, so enabling
the feature on an existing log does not retroactively flag it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

GENESIS = ""


class AuditLog:
    def __init__(self, *, log_path: str, hmac_key: str = "") -> None:
        self._path = log_path
        self._hmac_key = hmac_key or ""
        # append() is a read-modify-write on _last_hash plus a file write. Once
        # handlers are offloaded to the anyio threadpool, appends can run
        # concurrently; without this lock two threads read the same _last_hash
        # and produce sibling events that break the hash chain. Reads (verify /
        # iter_filtered) take the same lock so they never observe a torn append.
        self._lock = threading.Lock()
        self._last_hash = self._load_last_hash()

    # --- hashing helpers ---------------------------------------------------
    def _digest(self, payload: str) -> str:
        if self._hmac_key:
            return hmac.new(
                self._hmac_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical(event: dict[str, Any]) -> str:
        """Deterministic serialization of the event body excluding its own hash."""
        body = {k: v for k, v in event.items() if k != "hash"}
        return json.dumps(body, sort_keys=True, ensure_ascii=False)

    def _load_last_hash(self) -> str:
        if not os.path.exists(self._path):
            return GENESIS
        last = GENESIS
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("hash"):
                    last = ev["hash"]
        return last

    # --- public API --------------------------------------------------------
    def append(self, event: dict[str, Any]) -> None:
        """Append a single JSON object as one line, chained to the previous event.

        The parent directory is created lazily on first append so that
        constructing an AuditLog with an unwritable default path (e.g. macOS
        tests that never trigger a write) does not crash startup.
        """
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with self._lock:
            chained = dict(event)
            chained["event_id"] = uuid.uuid4().hex
            chained["prev_hash"] = self._last_hash
            chained["hash"] = self._digest(self._canonical(chained))
            line = json.dumps(chained, ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._last_hash = chained["hash"]

    def verify(self) -> tuple[bool, int]:
        """Recompute the hash chain over the log.

        Returns ``(True, -1)`` when intact (or empty / all-legacy), else
        ``(False, line_index)`` for the 0-based index of the first offending line.

        Unhashed legacy lines are tolerated ONLY as a prefix before the first
        hashed event (the pre-chaining migration window). Once the chain has
        started, any unhashed JSON line is treated as tampering and breaks
        verification, so an attacker cannot append a legacy-shaped record to forge
        an event while keeping verify green.
        """
        if not os.path.exists(self._path):
            return (True, -1)
        # Snapshot the file under the lock (short hold), then verify in memory so
        # a concurrent append cannot make us read a torn final line as tampering.
        with self._lock, open(self._path, encoding="utf-8") as f:
            raw_lines = f.readlines()
        prev = GENESIS
        seen_hashed = False
        for i, raw in enumerate(raw_lines):
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                return (False, i)
            if not (isinstance(ev, dict) and ev.get("hash")):
                if seen_hashed:
                    return (False, i)  # unhashed line after the chain started
                continue  # legacy prefix, tolerated
            if ev.get("prev_hash") != prev:
                return (False, i)
            if self._digest(self._canonical(ev)) != ev["hash"]:
                return (False, i)
            prev = ev["hash"]
            seen_hashed = True
        return (True, -1)

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
        # Read under the lock so we never see a torn append, then yield from the
        # in-memory snapshot (holding the lock across yields would block appends
        # for as long as the caller iterates).
        with self._lock, open(self._path, encoding="utf-8") as f:
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
