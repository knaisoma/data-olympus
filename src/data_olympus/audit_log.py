"""Append-only JSONL audit log of all writes, with a tamper-evident hash chain.

Each appended event carries an ``event_id``, the ``prev_hash`` of the previous
chained event, and its own ``hash`` over the canonical event body (which includes
``prev_hash``). Any later edit/deletion/reordering of an event breaks the chain
and is detected by :meth:`verify`. With ``KB_AUDIT_HMAC_KEY`` set the digest is a
keyed HMAC-SHA256, so an attacker who can write the log file still cannot forge a
valid chain without the key. Legacy lines without a ``hash`` are tolerated: the
chain validates the hashed lines in order and ignores unhashed ones, so enabling
the feature on an existing log does not retroactively flag it.

Rotation (WP3a). When the live file grows past ``max_bytes`` a fresh append
rotates it: the live file is renamed to ``<stem>-<UTC-timestamp><suffix>`` and a
new live file is started. The hash chain carries across the boundary because
``_last_hash`` is retained in memory across the rename, so the first event of the
new file links to the last hash of the rotated one. :meth:`verify` replays every
rotated segment in chronological order followed by the live file, so an intact
chain validates across rotations and any break at the boundary is caught. Reads
(:meth:`iter_filtered`) default to the live file only (cheap, matches the pre-
rotation behaviour) but can walk rotated segments newest-first when
``include_rotated=True`` so a ``since``-filtered query still sees history that has
rotated out of the live file.
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

GENESIS = ""


class AuditLog:
    def __init__(
        self, *, log_path: str, hmac_key: str = "", max_bytes: int = 0,
    ) -> None:
        self._path = log_path
        self._hmac_key = hmac_key or ""
        # Size-based rotation threshold in bytes. 0 (the default) disables
        # rotation entirely, so an operator who never sets KB_AUDIT_MAX_BYTES keeps
        # the single-file behaviour and existing logs are untouched.
        self._max_bytes = max_bytes if max_bytes and max_bytes > 0 else 0
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
        """The last chained hash across ALL segments (rotated + live).

        Reads rotated segments in chronological order then the live file so a
        process that restarts after a rotation still chains its first append to
        the true tail of the chain rather than to the (now-rotated-out) live
        file's older content.
        """
        last = GENESIS
        for path in [*self._rotated_paths(), self._path]:
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
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

    # --- rotation helpers --------------------------------------------------
    def _rotated_paths(self) -> list[str]:
        """Rotated segment paths, oldest-first.

        Rotated files are named ``<stem>-<UTC-timestamp><suffix>`` next to the
        live file. Sorting the glob lexically orders them chronologically because
        the timestamp is fixed-width ``YYYYmmddTHHMMSSffffff``. The live file
        itself is excluded (its name has no timestamp segment)."""
        stem, suffix = os.path.splitext(self._path)
        pattern = f"{glob.escape(stem)}-*{suffix}"
        return sorted(p for p in glob.glob(pattern) if p != self._path)

    def _rotated_path_for(self, when: float) -> str:
        stem, suffix = os.path.splitext(self._path)
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime(when))
        micros = int((when % 1) * 1_000_000)
        candidate = f"{stem}-{ts}{micros:06d}{suffix}"
        # Extremely unlikely, but never clobber an existing rotated segment.
        n = 1
        while os.path.exists(candidate):
            candidate = f"{stem}-{ts}{micros:06d}-{n}{suffix}"
            n += 1
        return candidate

    def _maybe_rotate_locked(self) -> None:
        """Rotate the live file when it exceeds the size threshold. Caller holds
        the lock. The in-memory ``_last_hash`` is deliberately NOT reset, so the
        first event written to the fresh live file links to the last hash of the
        rotated segment and the chain is continuous across the boundary."""
        if self._max_bytes <= 0 or not os.path.exists(self._path):
            return
        try:
            size = os.path.getsize(self._path)
        except OSError:  # pragma: no cover - defensive
            return
        if size < self._max_bytes:
            return
        os.rename(self._path, self._rotated_path_for(time.time()))

    # --- public API --------------------------------------------------------
    def append(self, event: dict[str, Any]) -> None:
        """Append a single JSON object as one line, chained to the previous event.

        The parent directory is created lazily on first append so that
        constructing an AuditLog with an unwritable default path (e.g. macOS
        tests that never trigger a write) does not crash startup. When rotation
        is enabled and the live file has passed the size threshold, it is rotated
        BEFORE this event is written so the new event starts the fresh segment.
        """
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with self._lock:
            self._maybe_rotate_locked()
            chained = dict(event)
            chained["event_id"] = uuid.uuid4().hex
            chained["prev_hash"] = self._last_hash
            chained["hash"] = self._digest(self._canonical(chained))
            line = json.dumps(chained, ensure_ascii=False)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._last_hash = chained["hash"]

    def verify(self) -> tuple[bool, int]:
        """Recompute the hash chain over the whole log (all rotated segments +
        the live file, in chronological order).

        Returns ``(True, -1)`` when intact (or empty / all-legacy), else
        ``(False, line_index)`` for the 0-based index of the first offending line
        counted across the concatenation of all segments (rotated oldest-first,
        then live). So a single-file log verifies exactly as before, and a
        rotated log verifies across the boundary: the first event of a new segment
        must carry the last hash of the previous segment as its ``prev_hash``.

        Unhashed legacy lines are tolerated ONLY as a prefix before the first
        hashed event (the pre-chaining migration window). Once the chain has
        started, any unhashed JSON line is treated as tampering and breaks
        verification, so an attacker cannot append a legacy-shaped record to forge
        an event while keeping verify green.
        """
        # Snapshot every segment under the lock (short hold), then verify in
        # memory so a concurrent append cannot make us read a torn final line as
        # tampering.
        with self._lock:
            segments: list[list[str]] = []
            for path in [*self._rotated_paths(), self._path]:
                if not os.path.exists(path):
                    continue
                with open(path, encoding="utf-8") as f:
                    segments.append(f.readlines())
        prev = GENESIS
        seen_hashed = False
        global_index = 0
        for raw_lines in segments:
            for raw in raw_lines:
                i = global_index
                global_index += 1
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
        include_rotated: bool = False,
        max_scan_events: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield events matching the filters, most-recent first.

        By default only the live file is read (cheap; matches the pre-rotation
        behaviour). When ``include_rotated`` is True the rotated segments are also
        walked, newest-first, AFTER the live file so results stay strictly most-
        recent-first across the rotation boundary. Callers that pass a ``since``
        filter should set ``include_rotated=True`` to see history that has already
        rotated out of the live file.

        ``max_scan_events`` bounds the reverse scan: once that many raw events
        have been examined (across all scanned segments) iteration stops even if
        more match. This caps the memory/time cost of a query against a large
        rotated history; callers that only need "the most recent N" pass their N
        so the scan does not walk the entire archive.
        """
        scanned = 0
        # Live file first (newest events live here), then rotated newest-first.
        paths = [self._path]
        if include_rotated:
            paths.extend(reversed(self._rotated_paths()))
        for path in paths:
            if not os.path.exists(path):
                continue
            # Read under the lock so we never see a torn append, then yield from
            # the in-memory snapshot (holding the lock across yields would block
            # appends for as long as the caller iterates).
            with self._lock, open(path, encoding="utf-8") as f:
                lines = f.readlines()
            for line in reversed(lines):
                if max_scan_events is not None and scanned >= max_scan_events:
                    return
                line = line.strip()
                if not line:
                    continue
                scanned += 1
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since is not None and float(ev.get("ts", 0)) < since:
                    # Events are appended in time order, so within a segment a
                    # reverse scan that reaches an older-than-``since`` event has
                    # no newer matches left below it; and every rotated segment is
                    # strictly older than the live file we scan first. So the first
                    # sub-floor event ends the whole scan. (A clock that ran
                    # backwards mid-run could in theory hide a straggler; the audit
                    # ts is server wall-clock and this is a best-effort recency
                    # query, so the bound is worth the rare miss.)
                    return
                if agent is not None and ev.get("agent_identity") != agent:
                    continue
                if status is not None and ev.get("status") != status:
                    continue
                yield ev
