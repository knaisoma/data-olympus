"""SQLite backend for Agora (stdlib ``sqlite3`` with FTS5).

Every operation opens its own connection in WAL mode with a 5 s busy timeout,
so reads from the anyio worker pool never share a connection across threads.
Writes are serialised by one lock per store, which keeps two writers in this
process from ever meeting SQLite's lock; the busy timeout covers a second
process. The database lives in its own folder; ``initialize`` creates that
folder and touches nothing else.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from data_olympus.agora.models import (
    CommitResult,
    Contact,
    Highlight,
    Membership,
    Message,
    Participant,
    ParticipantRecord,
    Profile,
    Scope,
    Subscription,
    Thread,
    TicketRef,
    Vote,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterator

SCHEMA_VERSION = 1


class _Unset:
    """Marker for "do not compare" in the expected-value parameters."""


UNSET: Any = _Unset()


def _direct_clause(direct_for: Collection[str] | None, params: list[Any],
                   prefix: str = "") -> str | None:
    """SQL for the direct-conversation rule: a participant-audience thread is
    visible only when one of its ends is in ``direct_for`` (the reader and,
    for a person, their agents). None means no restriction."""
    if direct_for is None:
        return None
    names = list(direct_for)
    if not names:
        return f"{prefix}audience_type != 'participant'"
    ends = ", ".join("?" for _ in names)
    params.extend(names)
    params.extend(names)
    return (f"({prefix}audience_type != 'participant' OR {prefix}originator IN ({ends}) "
            f"OR {prefix}audience_value IN ({ends}))")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS participants (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT,
    verified INTEGER NOT NULL, first_seen REAL NOT NULL, last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    name TEXT PRIMARY KEY, description TEXT NOT NULL, skills TEXT NOT NULL,
    tool TEXT NOT NULL, availability TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS scopes (
    id TEXT PRIMARY KEY, tier TEXT NOT NULL, owner TEXT, created_by TEXT,
    remote_url TEXT NOT NULL, has_kb_docs INTEGER NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    project TEXT NOT NULL, participant TEXT NOT NULL, role TEXT NOT NULL,
    state TEXT NOT NULL, invited_by TEXT, updated_at REAL NOT NULL,
    PRIMARY KEY (project, participant)
);
CREATE INDEX IF NOT EXISTS memberships_participant ON memberships(participant);
CREATE TABLE IF NOT EXISTS contacts (
    recipient TEXT NOT NULL, sender TEXT NOT NULL, state TEXT NOT NULL,
    note TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (recipient, sender)
);
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY, scope TEXT NOT NULL, audience_type TEXT NOT NULL,
    audience_value TEXT NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL,
    originator TEXT NOT NULL, state TEXT NOT NULL, tickets TEXT NOT NULL,
    payload TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    timeout_at REAL, topics TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS threads_due ON threads(kind, state, timeout_at);
CREATE INDEX IF NOT EXISTS threads_scope ON threads(scope, updated_at);
CREATE INDEX IF NOT EXISTS threads_updated ON threads(updated_at);
CREATE TABLE IF NOT EXISTS messages (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL, author TEXT NOT NULL, via TEXT NOT NULL,
    body TEXT NOT NULL, refs TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, seq);
CREATE TABLE IF NOT EXISTS votes (
    thread_id TEXT NOT NULL, voter TEXT NOT NULL, voter_kind TEXT NOT NULL,
    option_id TEXT NOT NULL, rationale TEXT NOT NULL, confidence REAL,
    status TEXT NOT NULL, reviewed_by TEXT, updated_at REAL NOT NULL,
    PRIMARY KEY (thread_id, voter)
);
CREATE TABLE IF NOT EXISTS seen (
    participant TEXT NOT NULL, thread_id TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (participant, thread_id)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    participant TEXT NOT NULL, scope TEXT NOT NULL, autonomy TEXT NOT NULL,
    kinds TEXT NOT NULL, PRIMARY KEY (participant, scope)
);
CREATE TABLE IF NOT EXISTS highlights (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL, thread_id TEXT, message_id TEXT, action TEXT NOT NULL,
    headline TEXT NOT NULL, actor TEXT NOT NULL, topics TEXT NOT NULL,
    weight INTEGER NOT NULL, pinned_by TEXT, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS highlights_scope ON highlights(scope, seq);
CREATE TABLE IF NOT EXISTS catchup (
    participant TEXT PRIMARY KEY, seq INTEGER NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(
    thread_id UNINDEXED, scope UNINDEXED, text
);
"""


def _in(column: str, values: Collection[Any], params: list[Any]) -> str:
    vals = list(values)
    params.extend(vals)
    return f"{column} IN ({', '.join('?' for _ in vals)})"


class SQLiteAgoraStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    # --- connections -------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One transaction under the store's write lock."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
            finally:
                conn.close()

    def initialize(self) -> None:
        folder = os.path.dirname(self._path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with self._write() as conn:
            for stmt in _SCHEMA.split(";"):
                if stmt.strip():
                    conn.execute(stmt)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                             (SCHEMA_VERSION,))
            elif int(row["version"]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Agora database {self._path} has schema version "
                    f"{row['version']}, newer than this server ({SCHEMA_VERSION})"
                )

    # --- participants and profiles ----------------------------------------
    def upsert_participant(self, p: Participant, now: float) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO participants (name, kind, owner, verified, first_seen, "
                "last_seen) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "kind=excluded.kind, owner=excluded.owner, verified=excluded.verified, "
                "last_seen=excluded.last_seen",
                (p.name, p.kind, p.owner, int(p.verified), now, now),
            )

    @staticmethod
    def _participant(r: sqlite3.Row) -> ParticipantRecord:
        return ParticipantRecord(r["name"], r["kind"], r["owner"], bool(r["verified"]),
                                 r["first_seen"], r["last_seen"])

    def get_participant(self, name: str) -> ParticipantRecord | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM participants WHERE name = ?",
                             (name,)).fetchone()
        return self._participant(r) if r else None

    def list_participants(self) -> list[ParticipantRecord]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM participants ORDER BY name").fetchall()
        return [self._participant(r) for r in rows]

    def get_profile(self, name: str) -> Profile | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()
        if r is None:
            return None
        return Profile(r["name"], r["description"], tuple(json.loads(r["skills"])),
                       r["tool"], r["availability"], r["updated_at"])

    def put_profile(self, profile: Profile) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO profiles (name, description, skills, tool, "
                "availability, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (profile.name, profile.description, json.dumps(list(profile.skills)),
                 profile.tool, profile.availability, profile.updated_at),
            )

    # --- scopes, memberships, contacts -------------------------------------
    @staticmethod
    def _scope(r: sqlite3.Row) -> Scope:
        return Scope(r["id"], r["tier"], r["owner"], r["created_by"], r["remote_url"],
                     bool(r["has_kb_docs"]), r["created_at"])

    def get_scope(self, id: str) -> Scope | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM scopes WHERE id = ?", (id,)).fetchone()
        return self._scope(r) if r else None

    def put_scope(self, scope: Scope) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scopes (id, tier, owner, created_by, remote_url, "
                "has_kb_docs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope.id, scope.tier, scope.owner, scope.created_by, scope.remote_url,
                 int(scope.has_kb_docs), scope.created_at),
            )

    def insert_scope(self, scope: Scope) -> bool:
        """Store ``scope`` only if its id is new; never overwrites (a seeding
        reader must not undo a concurrent claim). True when it was inserted."""
        with self._write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO scopes (id, tier, owner, created_by, remote_url, "
                "has_kb_docs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope.id, scope.tier, scope.owner, scope.created_by, scope.remote_url,
                 int(scope.has_kb_docs), scope.created_at),
            )
            return cur.rowcount == 1

    def claim_project(self, scope_id: str, owner: str, membership: Membership) -> bool:
        """Make ``owner`` the owner of a never-owned project, in one transaction
        with its owner membership. False when it has, or ever had, an owner:
        an orphaned project is reassigned by an admin, never re-claimed."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE scopes SET owner = ? WHERE id = ? AND owner IS NULL AND NOT "
                "EXISTS (SELECT 1 FROM memberships WHERE project = ? AND role = 'owner')",
                (owner, scope_id, scope_id))
            if cur.rowcount != 1:
                return False
            self._put_membership(conn, membership)
            return True

    def create_owned_scope(self, scope: Scope, membership: Membership) -> bool:
        """Insert a new scope and its owner membership as one unit. False,
        writing nothing, when the id already exists."""
        with self._write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO scopes (id, tier, owner, created_by, remote_url, "
                "has_kb_docs, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (scope.id, scope.tier, scope.owner, scope.created_by, scope.remote_url,
                 int(scope.has_kb_docs), scope.created_at),
            )
            if cur.rowcount != 1:
                return False
            self._put_membership(conn, membership)
            return True

    @staticmethod
    def _keeps_an_owner(conn: sqlite3.Connection, m: Membership) -> bool:
        """False when writing ``m`` would take the project's last active owner
        away (remove, block or demote). Read inside the write transaction, so
        two owners removing each other cannot both pass."""
        if m.project == "*" or (m.role == "owner" and m.state == "active"):
            return True
        row = conn.execute(
            "SELECT role, state FROM memberships WHERE project = ? AND participant = ?",
            (m.project, m.participant)).fetchone()
        if row is None or row["role"] != "owner" or row["state"] != "active":
            return True
        others = conn.execute(
            "SELECT COUNT(*) AS n FROM memberships WHERE project = ? AND role = 'owner' "
            "AND state = 'active' AND participant != ?",
            (m.project, m.participant)).fetchone()
        return int(others["n"]) > 0

    @staticmethod
    def _membership_matches(conn: sqlite3.Connection, m: Membership,
                            expected: Membership | None) -> bool:
        """True when the stored row is still the one the caller decided on
        (absent when ``expected`` is None)."""
        row = conn.execute(
            "SELECT * FROM memberships WHERE project = ? AND participant = ?",
            (m.project, m.participant)).fetchone()
        if expected is None:
            return row is None
        return row is not None and SQLiteAgoraStore._membership(row) == expected

    def update_membership(self, m: Membership, *, expected: Any = UNSET) -> bool:
        """Write ``m`` unless it would leave the project without an active
        owner, or, when ``expected`` is given, unless the stored row has
        changed since the caller read it. False (nothing written) otherwise."""
        with self._write() as conn:
            if expected is not UNSET and not self._membership_matches(conn, m, expected):
                return False
            if not self._keeps_an_owner(conn, m):
                return False
            self._put_membership(conn, m)
            return True

    def list_scopes(self) -> list[Scope]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM scopes ORDER BY id").fetchall()
        return [self._scope(r) for r in rows]

    @staticmethod
    def _membership(r: sqlite3.Row) -> Membership:
        return Membership(r["project"], r["participant"], r["role"], r["state"],
                          r["invited_by"], r["updated_at"])

    def get_membership(self, project: str, participant: str) -> Membership | None:
        with self._read() as conn:
            r = conn.execute(
                "SELECT * FROM memberships WHERE project = ? AND participant = ?",
                (project, participant)).fetchone()
        return self._membership(r) if r else None

    @staticmethod
    def _put_membership(conn: sqlite3.Connection, m: Membership) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO memberships (project, participant, role, state, "
            "invited_by, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (m.project, m.participant, m.role, m.state, m.invited_by, m.updated_at),
        )

    def put_membership(self, m: Membership) -> None:
        with self._write() as conn:
            self._put_membership(conn, m)

    def list_memberships(
        self, *, project: str | None = None, participant: str | None = None,
    ) -> list[Membership]:
        where: list[str] = []
        params: list[Any] = []
        if project is not None:
            where.append("project = ?")
            params.append(project)
        if participant is not None:
            where.append("participant = ?")
            params.append(participant)
        sql = "SELECT * FROM memberships"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._read() as conn:
            rows = conn.execute(sql + " ORDER BY project, participant", params).fetchall()
        return [self._membership(r) for r in rows]

    @staticmethod
    def _contact(r: sqlite3.Row) -> Contact:
        return Contact(r["recipient"], r["sender"], r["state"], r["note"], r["updated_at"])

    def get_contact(self, recipient: str, sender: str) -> Contact | None:
        with self._read() as conn:
            r = conn.execute(
                "SELECT * FROM contacts WHERE recipient = ? AND sender = ?",
                (recipient, sender)).fetchone()
        return self._contact(r) if r else None

    def put_contact(self, c: Contact) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO contacts (recipient, sender, state, note, "
                "updated_at) VALUES (?, ?, ?, ?, ?)",
                (c.recipient, c.sender, c.state, c.note, c.updated_at),
            )

    def list_contacts(
        self, *, recipient: str | None = None, sender: str | None = None,
        state: str | None = None,
    ) -> list[Contact]:
        where: list[str] = []
        params: list[Any] = []
        for col, val in (("recipient", recipient), ("sender", sender), ("state", state)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        sql = "SELECT * FROM contacts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._read() as conn:
            rows = conn.execute(sql + " ORDER BY updated_at", params).fetchall()
        return [self._contact(r) for r in rows]

    # --- threads and messages ----------------------------------------------
    @staticmethod
    def _thread(r: sqlite3.Row) -> Thread:
        tickets = tuple(TicketRef(**t) for t in json.loads(r["tickets"]))
        return Thread(
            id=r["id"], scope=r["scope"], audience_type=r["audience_type"],
            audience_value=r["audience_value"], kind=r["kind"], title=r["title"],
            originator=r["originator"], state=r["state"], tickets=tickets,
            payload=json.loads(r["payload"]), created_at=r["created_at"],
            updated_at=r["updated_at"], timeout_at=r["timeout_at"],
            topics=tuple(json.loads(r["topics"])), version=int(r["version"]),
        )

    @staticmethod
    def _tickets_json(thread: Thread) -> str:
        return json.dumps([
            {"system": t.system, "key": t.key, "url": t.url, "title": t.title}
            for t in thread.tickets
        ])

    @staticmethod
    def _insert_message(conn: sqlite3.Connection, m: Message, scope: str) -> None:
        conn.execute(
            "INSERT INTO messages (id, thread_id, author, via, body, refs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.thread_id, m.author, m.via, m.body, json.dumps(list(m.refs)),
             m.created_at),
        )
        if m.body:
            conn.execute("INSERT INTO text_fts (thread_id, scope, text) VALUES (?, ?, ?)",
                         (m.thread_id, scope, m.body))

    def _insert_thread(self, conn: sqlite3.Connection, thread: Thread) -> None:
        conn.execute(
            "INSERT INTO threads (id, scope, audience_type, audience_value, kind, "
            "title, originator, state, tickets, payload, created_at, updated_at, "
            "timeout_at, topics, version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread.id, thread.scope, thread.audience_type, thread.audience_value,
             thread.kind, thread.title, thread.originator, thread.state,
             self._tickets_json(thread), json.dumps(thread.payload),
             thread.created_at, thread.updated_at, thread.timeout_at,
             json.dumps(list(thread.topics)), thread.version),
        )
        conn.execute("INSERT INTO text_fts (thread_id, scope, text) VALUES (?, ?, ?)",
                     (thread.id, thread.scope, thread.title))

    @staticmethod
    def _insert_highlight(conn: sqlite3.Connection, h: Highlight) -> None:
        conn.execute(
            "INSERT INTO highlights (id, scope, thread_id, message_id, action, "
            "headline, actor, topics, weight, pinned_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (h.id, h.scope, h.thread_id, h.message_id, h.action, h.headline, h.actor,
             json.dumps(list(h.topics)), h.weight, h.pinned_by, h.created_at),
        )

    def create_thread(self, thread: Thread, first_message: Message | None,
                      *, highlight: Highlight | None = None) -> None:
        """Insert a new thread with its first message and, for an opening
        event, its headline, in one transaction."""
        with self._write() as conn:
            self._insert_thread(conn, thread)
            if first_message is not None:
                self._insert_message(conn, first_message, thread.scope)
            if highlight is not None:
                self._insert_highlight(conn, highlight)

    def get_thread(self, id: str) -> Thread | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM threads WHERE id = ?", (id,)).fetchone()
        return self._thread(r) if r else None

    @staticmethod
    def _store_thread(conn: sqlite3.Connection, thread: Thread, version: int) -> None:
        conn.execute(
            "UPDATE threads SET state = ?, title = ?, tickets = ?, payload = ?, "
            "updated_at = ?, timeout_at = ?, topics = ?, version = ? WHERE id = ?",
            (thread.state, thread.title, SQLiteAgoraStore._tickets_json(thread),
             json.dumps(thread.payload), thread.updated_at, thread.timeout_at,
             json.dumps(list(thread.topics)), version, thread.id),
        )

    def commit_event(
        self, *, thread: Thread | None = None, expected_version: int | None = None,
        message: Message | None = None, membership: Membership | None = None,
        highlight: Highlight | None = None, expected_membership: Any = UNSET,
    ) -> CommitResult:
        """One transaction under the write lock: a thread change (only when its
        stored version still equals ``expected_version``; stored with version
        + 1), its event message, a membership change (refused when it would
        leave a project without an active owner) and a headline. On any
        refusal nothing is written and ``ok`` is False."""
        with self._write() as conn:
            # Every check runs before any write, so a refusal writes nothing.
            stored: Thread | None = None
            if thread is not None:
                r = conn.execute("SELECT version FROM threads WHERE id = ?",
                                 (thread.id,)).fetchone()
                if r is None or int(r["version"]) != expected_version:
                    return CommitResult(False, None)
                updated_at = message.created_at if message is not None else thread.updated_at
                stored = replace(thread, version=int(r["version"]) + 1,
                                 updated_at=max(updated_at, thread.updated_at))
            if membership is not None:
                if (expected_membership is not UNSET and not self._membership_matches(
                        conn, membership, expected_membership)):
                    return CommitResult(False, None)
                if not self._keeps_an_owner(conn, membership):
                    return CommitResult(False, None)
            if stored is not None:
                self._store_thread(conn, stored, stored.version)
            if membership is not None:
                self._put_membership(conn, membership)
            if message is not None:
                scope_row = conn.execute("SELECT scope FROM threads WHERE id = ?",
                                         (message.thread_id,)).fetchone()
                if scope_row is None:
                    raise KeyError(message.thread_id)
                self._insert_message(conn, message, scope_row["scope"])
                if thread is None:
                    conn.execute(
                        "UPDATE threads SET updated_at = MAX(updated_at, ?), "
                        "version = version + 1 WHERE id = ?",
                        (message.created_at, message.thread_id))
            if highlight is not None:
                self._insert_highlight(conn, highlight)
            return CommitResult(True, stored)

    def expire_if_due(
        self, thread_id: str, now: float,
        build: Callable[[Thread], tuple[Message, Highlight | None]],
    ) -> Thread | None:
        """Read, check and expire in one transaction: an open decision whose
        timeout has passed becomes unresolved_advisory with the event message
        and headline ``build`` makes from the fresh thread. None when it is
        not (or no longer) due, so racing callers expire it once."""
        with self._write() as conn:
            r = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            if r is None:
                return None
            fresh = self._thread(r)
            if (fresh.kind != "decision" or fresh.state != "open"
                    or fresh.timeout_at is None or fresh.timeout_at > now):
                return None
            message, highlight = build(fresh)
            stored = replace(fresh, state="unresolved_advisory",
                             updated_at=max(now, message.created_at),
                             version=fresh.version + 1)
            self._store_thread(conn, stored, stored.version)
            self._insert_message(conn, message, fresh.scope)
            if highlight is not None:
                self._insert_highlight(conn, highlight)
            return stored

    def list_due_decisions(self, now: float) -> list[Thread]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM threads WHERE kind = 'decision' AND state = 'open' "
                "AND timeout_at IS NOT NULL AND timeout_at <= ? ORDER BY timeout_at",
                (now,)).fetchall()
        return [self._thread(r) for r in rows]

    def list_threads(
        self, *, scopes: Collection[str] | None = None,
        kinds: Collection[str] | None = None,
        states: Collection[str] | None = None,
        audiences: Collection[tuple[str, str]] | None = None,
        originators: Collection[str] | None = None,
        updated_after: float | None = None, limit: int = 100,
        direct_for: Collection[str] | None = None,
    ) -> list[Thread]:
        where: list[str] = []
        params: list[Any] = []
        direct = _direct_clause(direct_for, params)
        if direct is not None:
            where.append(direct)
        for column, values in (("scope", scopes), ("kind", kinds), ("state", states),
                               ("originator", originators)):
            if values is not None:
                if not values:
                    return []
                where.append(_in(column, values, params))
        if audiences is not None:
            if not audiences:
                return []
            parts = []
            for a_type, a_value in audiences:
                parts.append("(audience_type = ? AND audience_value = ?)")
                params.extend((a_type, a_value))
            where.append("(" + " OR ".join(parts) + ")")
        if updated_after is not None:
            where.append("updated_at > ?")
            params.append(updated_after)
        sql = "SELECT * FROM threads"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, id LIMIT ?"
        params.append(max(0, int(limit)))
        with self._read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._thread(r) for r in rows]

    def add_message(self, m: Message, *, refuse_states: Collection[str] = ()) -> bool:
        """A plain reply: stored with the thread's updated_at and version
        bumped, so a writer holding an older version gets a conflict. False,
        writing nothing, when the thread's current state is in
        ``refuse_states`` (checked inside the write)."""
        with self._write() as conn:
            r = conn.execute("SELECT scope, state FROM threads WHERE id = ?",
                             (m.thread_id,)).fetchone()
            if r is None:
                raise KeyError(m.thread_id)
            if r["state"] in set(refuse_states):
                return False
            self._insert_message(conn, m, r["scope"])
            conn.execute(
                "UPDATE threads SET updated_at = MAX(updated_at, ?), version = version + 1 "
                "WHERE id = ?", (m.created_at, m.thread_id))
            return True

    @staticmethod
    def _message(r: sqlite3.Row) -> Message:
        return Message(r["id"], r["thread_id"], r["author"], r["via"], r["body"],
                       tuple(json.loads(r["refs"])), r["created_at"])

    def get_message(self, id: str) -> Message | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM messages WHERE id = ?", (id,)).fetchone()
        return self._message(r) if r else None

    def list_messages_around(self, thread_id: str, message_id: str, *, before: int,
                             after: int) -> list[Message]:
        """The message ``message_id`` with up to ``before`` earlier and
        ``after`` later messages of the same thread, in order. Empty when the
        message is not in that thread."""
        with self._read() as conn:
            anchor = conn.execute(
                "SELECT seq FROM messages WHERE id = ? AND thread_id = ?",
                (message_id, thread_id)).fetchone()
            if anchor is None:
                return []
            seq = int(anchor["seq"])
            earlier = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND seq < ? "
                "ORDER BY seq DESC LIMIT ?", (thread_id, seq, max(0, before))).fetchall()
            rest = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? AND seq >= ? "
                "ORDER BY seq LIMIT ?", (thread_id, seq, max(0, after) + 1)).fetchall()
        return [self._message(r) for r in [*reversed(earlier), *rest]]

    def list_messages(self, thread_id: str, *, limit: int = 200) -> list[Message]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY seq LIMIT ?",
                (thread_id, max(0, int(limit)))).fetchall()
        return [self._message(r) for r in rows]

    # --- votes, seen, subscriptions, search --------------------------------
    def put_vote(self, v: Vote, *, thread_state: str | None = None,
                 expected: Any = UNSET) -> bool:
        """Store a vote (one per voter). Inside the write it is refused, with
        nothing written, when the thread is no longer in ``thread_state`` or,
        when ``expected`` is given, when the stored vote has changed since the
        caller read it (None: there must be no vote yet)."""
        with self._write() as conn:
            if thread_state is not None:
                r = conn.execute("SELECT state FROM threads WHERE id = ?",
                                 (v.thread_id,)).fetchone()
                if r is None or r["state"] != thread_state:
                    return False
            if expected is not UNSET:
                row = conn.execute(
                    "SELECT * FROM votes WHERE thread_id = ? AND voter = ?",
                    (v.thread_id, v.voter)).fetchone()
                current = None if row is None else self._vote(row)
                if current != expected:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO votes (thread_id, voter, voter_kind, option_id, "
                "rationale, confidence, status, reviewed_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (v.thread_id, v.voter, v.voter_kind, v.option_id, v.rationale,
                 v.confidence, v.status, v.reviewed_by, v.updated_at),
            )
            return True

    @staticmethod
    def _vote(r: sqlite3.Row) -> Vote:
        return Vote(r["thread_id"], r["voter"], r["voter_kind"], r["option_id"],
                    r["rationale"], r["confidence"], r["status"], r["reviewed_by"],
                    r["updated_at"])

    def list_votes(self, thread_id: str) -> list[Vote]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM votes WHERE thread_id = ? ORDER BY updated_at, voter",
                (thread_id,)).fetchall()
        return [self._vote(r) for r in rows]

    def mark_seen(self, participant: str, thread_id: str, ts: float) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO seen (participant, thread_id, ts) VALUES (?, ?, ?) "
                "ON CONFLICT(participant, thread_id) DO UPDATE SET "
                "ts = MAX(seen.ts, excluded.ts)",
                (participant, thread_id, ts),
            )

    def seen_map(self, participant: str, thread_ids: Collection[str]) -> dict[str, float]:
        ids = list(thread_ids)
        if not ids:
            return {}
        out: dict[str, float] = {}
        with self._read() as conn:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                params: list[Any] = [participant]
                clause = _in("thread_id", chunk, params)
                for r in conn.execute(
                    f"SELECT thread_id, ts FROM seen WHERE participant = ? AND {clause}",
                    params,
                ):
                    out[r["thread_id"]] = r["ts"]
        return out

    def put_subscription(self, s: Subscription) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions (participant, scope, autonomy, "
                "kinds) VALUES (?, ?, ?, ?)",
                (s.participant, s.scope, s.autonomy, json.dumps(list(s.kinds))),
            )

    def delete_subscription(self, participant: str, scope: str) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM subscriptions WHERE participant = ? AND scope = ?",
                         (participant, scope))

    def list_subscriptions(self, participant: str) -> list[Subscription]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM subscriptions WHERE participant = ? ORDER BY scope",
                (participant,)).fetchall()
        return [Subscription(r["participant"], r["scope"], r["autonomy"],
                             tuple(json.loads(r["kinds"]))) for r in rows]

    # --- headlines and catch-up markers ------------------------------------
    @staticmethod
    def _highlight(r: sqlite3.Row) -> Highlight:
        return Highlight(r["id"], int(r["seq"]), r["scope"], r["thread_id"],
                         r["message_id"], r["action"], r["headline"], r["actor"],
                         tuple(json.loads(r["topics"])), int(r["weight"]),
                         r["pinned_by"], r["created_at"])

    def get_highlight(self, id: str) -> Highlight | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM highlights WHERE id = ?", (id,)).fetchone()
        return self._highlight(r) if r else None

    def update_highlight(self, h: Highlight) -> None:
        """Change a headline's pin; its position (seq) never changes."""
        with self._write() as conn:
            conn.execute("UPDATE highlights SET pinned_by = ? WHERE id = ?",
                         (h.pinned_by, h.id))

    def max_seq(self) -> int:
        with self._read() as conn:
            r = conn.execute("SELECT COALESCE(MAX(seq), 0) AS m FROM highlights").fetchone()
        return int(r["m"])

    def list_highlights(
        self, *, after_seq: int, upto_seq: int, scopes: Collection[str],
        actions: Collection[str] | None = None, topics: Collection[str] | None = None,
        pinned_only: bool = False, since_ts: float | None = None,
    ) -> list[Highlight]:
        scope_list = list(scopes)
        if not scope_list:
            return []
        params: list[Any] = [after_seq, upto_seq]
        where = ["seq > ?", "seq <= ?", _in("scope", scope_list, params)]
        if actions:
            where.append(_in("action", actions, params))
        if pinned_only:
            where.append("pinned_by IS NOT NULL")
        if since_ts is not None:
            # History browsing by date keeps pinned headlines in view.
            where.append("(created_at >= ? OR pinned_by IS NOT NULL)")
            params.append(since_ts)
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM highlights WHERE {' AND '.join(where)} ORDER BY seq",
                params).fetchall()
        out = [self._highlight(r) for r in rows]
        if topics:
            wanted = set(topics)
            out = [h for h in out if wanted & set(h.topics)]
        return out

    def get_catchup(self, participant: str) -> int:
        with self._read() as conn:
            r = conn.execute("SELECT seq FROM catchup WHERE participant = ?",
                             (participant,)).fetchone()
        return int(r["seq"]) if r else 0

    def set_catchup(self, participant: str, seq: int) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO catchup (participant, seq) VALUES (?, ?) "
                "ON CONFLICT(participant) DO UPDATE SET seq = MAX(catchup.seq, excluded.seq)",
                (participant, int(seq)))

    def search(
        self, fts_query: str, *, scopes: Collection[str], limit: int,
        direct_for: Collection[str] | None = None,
    ) -> list[tuple[str, str]]:
        """Up to ``limit`` distinct threads, best match first. Scope and the
        direct-conversation rule are applied in SQL, before the limit; pages
        are read until ``limit`` distinct threads are found or matches run out."""
        scope_list = list(scopes)
        if not scope_list or limit <= 0:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        page = 200
        offset = 0
        with self._read() as conn:
            while len(out) < limit:
                params: list[Any] = [fts_query]
                clause = _in("scope", scope_list, params)
                sub_params: list[Any] = []
                direct = _direct_clause(direct_for, sub_params)
                extra = ""
                if direct is not None:
                    extra = f" AND thread_id IN (SELECT id FROM threads WHERE {direct})"
                    params.extend(sub_params)
                params.extend((page, offset))
                rows = conn.execute(
                    "SELECT thread_id, snippet(text_fts, 2, '[', ']', '...', 16) AS snip "
                    f"FROM text_fts WHERE text_fts MATCH ? AND {clause}{extra} "
                    "ORDER BY rank LIMIT ? OFFSET ?",
                    params,
                ).fetchall()
                for r in rows:
                    if r["thread_id"] in seen:
                        continue
                    seen.add(r["thread_id"])
                    out.append((r["thread_id"], r["snip"]))
                    if len(out) >= limit:
                        break
                if len(rows) < page:
                    break
                offset += page
        return out
