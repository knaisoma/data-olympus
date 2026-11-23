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
from typing import TYPE_CHECKING, Any

from data_olympus.agora.models import (
    Contact,
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
    from collections.abc import Collection, Iterator

SCHEMA_VERSION = 1

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
    timeout_at REAL
);
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
        """Make ``owner`` the owner of an unowned project, in one transaction
        with its owner membership. False when someone already owns it."""
        with self._write() as conn:
            cur = conn.execute(
                "UPDATE scopes SET owner = ? WHERE id = ? AND owner IS NULL",
                (owner, scope_id))
            if cur.rowcount != 1:
                return False
            self._put_membership(conn, membership)
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

    def create_thread(self, thread: Thread, first_message: Message | None) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT INTO threads (id, scope, audience_type, audience_value, kind, "
                "title, originator, state, tickets, payload, created_at, updated_at, "
                "timeout_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (thread.id, thread.scope, thread.audience_type, thread.audience_value,
                 thread.kind, thread.title, thread.originator, thread.state,
                 self._tickets_json(thread), json.dumps(thread.payload),
                 thread.created_at, thread.updated_at, thread.timeout_at),
            )
            conn.execute("INSERT INTO text_fts (thread_id, scope, text) VALUES (?, ?, ?)",
                         (thread.id, thread.scope, thread.title))
            if first_message is not None:
                self._insert_message(conn, first_message, thread.scope)

    def get_thread(self, id: str) -> Thread | None:
        with self._read() as conn:
            r = conn.execute("SELECT * FROM threads WHERE id = ?", (id,)).fetchone()
        return self._thread(r) if r else None

    def update_thread(self, thread: Thread) -> None:
        with self._write() as conn:
            conn.execute(
                "UPDATE threads SET state = ?, title = ?, tickets = ?, payload = ?, "
                "updated_at = ?, timeout_at = ? WHERE id = ?",
                (thread.state, thread.title, self._tickets_json(thread),
                 json.dumps(thread.payload), thread.updated_at, thread.timeout_at,
                 thread.id),
            )

    def list_threads(
        self, *, scopes: Collection[str] | None = None,
        kinds: Collection[str] | None = None,
        states: Collection[str] | None = None,
        audiences: Collection[tuple[str, str]] | None = None,
        originators: Collection[str] | None = None,
        updated_after: float | None = None, limit: int = 100,
    ) -> list[Thread]:
        where: list[str] = []
        params: list[Any] = []
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

    def add_message(self, m: Message) -> None:
        with self._write() as conn:
            r = conn.execute("SELECT scope FROM threads WHERE id = ?",
                             (m.thread_id,)).fetchone()
            if r is None:
                raise KeyError(m.thread_id)
            self._insert_message(conn, m, r["scope"])
            conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?",
                         (m.created_at, m.thread_id))

    @staticmethod
    def _message(r: sqlite3.Row) -> Message:
        return Message(r["id"], r["thread_id"], r["author"], r["via"], r["body"],
                       tuple(json.loads(r["refs"])), r["created_at"])

    def list_messages(self, thread_id: str, *, limit: int = 200) -> list[Message]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE thread_id = ? ORDER BY seq LIMIT ?",
                (thread_id, max(0, int(limit)))).fetchall()
        return [self._message(r) for r in rows]

    # --- votes, seen, subscriptions, search --------------------------------
    def put_vote(self, v: Vote) -> None:
        with self._write() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO votes (thread_id, voter, voter_kind, option_id, "
                "rationale, confidence, status, reviewed_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (v.thread_id, v.voter, v.voter_kind, v.option_id, v.rationale,
                 v.confidence, v.status, v.reviewed_by, v.updated_at),
            )

    def list_votes(self, thread_id: str) -> list[Vote]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM votes WHERE thread_id = ? ORDER BY updated_at, voter",
                (thread_id,)).fetchall()
        return [Vote(r["thread_id"], r["voter"], r["voter_kind"], r["option_id"],
                     r["rationale"], r["confidence"], r["status"], r["reviewed_by"],
                     r["updated_at"]) for r in rows]

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

    def search(
        self, fts_query: str, *, scopes: Collection[str], limit: int,
    ) -> list[tuple[str, str]]:
        scope_list = list(scopes)
        if not scope_list or limit <= 0:
            return []
        params: list[Any] = [fts_query]
        clause = _in("scope", scope_list, params)
        params.append(int(limit) * 5)
        with self._read() as conn:
            rows = conn.execute(
                "SELECT thread_id, snippet(text_fts, 2, '[', ']', '...', 16) AS snip "
                f"FROM text_fts WHERE text_fts MATCH ? AND {clause} "
                "ORDER BY rank LIMIT ?",
                params,
            ).fetchall()
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for r in rows:
            if r["thread_id"] in seen:
                continue
            seen.add(r["thread_id"])
            out.append((r["thread_id"], r["snip"]))
            if len(out) >= limit:
                break
        return out
