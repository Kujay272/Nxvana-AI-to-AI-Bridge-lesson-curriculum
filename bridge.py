#!/usr/bin/env python3
"""
nxa-bridge — a minimal, dependency-free bridge for AI-to-AI teaching.

The bridge is a message bus with a state machine. One agent pushes a lesson,
another claims it, reads it, saves it as a skill, and signs off. Every
hand-off is timestamped and auditable.

    new  ->  delivered  ->  read  ->  acknowledged

No shared memory, no shared process — just a table and four states.

Dependency-free: Python 3.10+ standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

__version__ = "2.0.0"

# ── configuration ────────────────────────────────────────────────────────────

#: the four states, in order
STATES = ("new", "delivered", "read", "acknowledged")

#: the only legal state changes. anything else is refused, not silently applied.
TRANSITIONS = {
    "new": "delivered",
    "delivered": "read",
    "read": "acknowledged",
}

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nxa_bridge.db")

_db_path = os.environ.get("NXA_BRIDGE_DB") or _DEFAULT_DB

#: serialises writes so concurrent agents can't interleave a read-modify-write
_write_lock = threading.Lock()


def configure(db_path: str) -> None:
    """Point the bridge at a different database file (used by tests and the demo)."""
    global _db_path
    _db_path = db_path


def get_db_path() -> str:
    """Where the bridge is currently storing its data."""
    return _db_path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── storage ──────────────────────────────────────────────────────────────────

@contextmanager
def _connect():
    """Open a connection, commit on success, roll back on failure, always close.

    Note: ``with sqlite3.connect(...)`` alone does *not* close the handle, it
    only commits. Long-running agents leak file descriptors that way.
    """
    conn = sqlite3.connect(_db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


_DDL = """
CREATE TABLE IF NOT EXISTS bridge (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id    TEXT UNIQUE,
    sender       TEXT NOT NULL,
    recipient    TEXT NOT NULL,
    content      TEXT NOT NULL,
    topic        TEXT NOT NULL DEFAULT 'general',
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'delivered', 'read', 'acknowledged')),
    created_at   TEXT NOT NULL,
    delivered_at TEXT,
    read_at      TEXT,
    acked_at     TEXT,
    ack_text     TEXT,
    applied      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_bridge_recipient_status
    ON bridge (recipient, status);

CREATE TABLE IF NOT EXISTS skill_memory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id  TEXT UNIQUE,
    topic      TEXT NOT NULL,
    content    TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    source     TEXT NOT NULL DEFAULT 'bridge',
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    """Create the schema if it isn't already there. Safe to call repeatedly."""
    with _connect() as conn:
        conn.executescript(_DDL)


def reset_db() -> None:
    """Drop every row. Destructive — for tests and the demo."""
    with _write_lock, _connect() as conn:
        conn.executescript("DROP TABLE IF EXISTS bridge; DROP TABLE IF EXISTS skill_memory;")
        conn.executescript(_DDL)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["applied"] = bool(d.get("applied"))
    return d


def _fetch_ids(conn: sqlite3.Connection, ids: list) -> list:
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM bridge WHERE id IN ({marks}) ORDER BY id ASC", ids
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── state machine ────────────────────────────────────────────────────────────

def send_message(
    sender: str,
    recipient: str,
    content: str,
    topic: str = "general",
    lesson_id: str | None = None,
):
    """Push a lesson into the bridge as ``new``.

    Returns the new row id, or ``None`` if this ``lesson_id`` has already been
    sent (idempotent — the same lesson is never queued twice).

    Raises on any other database error rather than disguising it as a duplicate.
    """
    init_db()

    if not sender or not str(sender).strip():
        raise ValueError("sender is required")
    if not recipient or not str(recipient).strip():
        raise ValueError("recipient is required")
    if not content or not str(content).strip():
        raise ValueError("content is required")

    recipient = recipient.strip().lower()
    now = _now()

    # The duplicate check *is* the insert: one transaction, one lock. A separate
    # SELECT-then-INSERT would let two senders slip the same lesson past each other.
    with _write_lock, _connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO bridge "
                "(lesson_id, sender, recipient, content, topic, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'new', ?)",
                (lesson_id, sender.strip(), recipient, content, topic, now),
            )
        except sqlite3.IntegrityError:
            # UNIQUE violation on lesson_id — already queued, nothing to do
            return None
        return cur.lastrowid


def claim_messages(recipient: str, limit: int = 10) -> list:
    """Claim new lessons: ``new`` -> ``delivered``. Stamps ``delivered_at``.

    This is the distinct event the earlier implementation implied but never
    wrote: a lesson being *handed over* is not the same as it being *read*.
    """
    init_db()
    recipient = (recipient or "").strip().lower()
    now = _now()

    with _write_lock, _connect() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM bridge WHERE recipient = ? AND status = 'new' "
                "ORDER BY id ASC LIMIT ?",
                (recipient, limit),
            ).fetchall()
        ]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE bridge SET status = 'delivered', delivered_at = ? "
            f"WHERE id IN ({marks})",
            [now, *ids],
        )
        return _fetch_ids(conn, ids)


def read_messages(recipient: str, limit: int = 10) -> list:
    """Mark delivered lessons as read: ``delivered`` -> ``read``. Stamps ``read_at``."""
    init_db()
    recipient = (recipient or "").strip().lower()
    now = _now()

    with _write_lock, _connect() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM bridge WHERE recipient = ? AND status = 'delivered' "
                "ORDER BY id ASC LIMIT ?",
                (recipient, limit),
            ).fetchall()
        ]
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE bridge SET status = 'read', read_at = ? WHERE id IN ({marks})",
            [now, *ids],
        )
        return _fetch_ids(conn, ids)


def acknowledge_message(message_id: int, ack_text: str, confidence: float = 0.6) -> bool:
    """Acknowledge a read lesson: ``read`` -> ``acknowledged``.

    Writes the lesson into ``skill_memory`` — this is the point of the bridge.
    Returns ``False`` if the lesson wasn't in ``read``; an illegal transition is
    refused rather than applied.
    """
    init_db()
    now = _now()

    with _write_lock, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM bridge WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None or row["status"] != "read":
            return False

        conn.execute(
            "UPDATE bridge SET status = 'acknowledged', ack_text = ?, acked_at = ? "
            "WHERE id = ?",
            (ack_text, now, message_id),
        )
        conn.execute(
            "INSERT OR REPLACE INTO skill_memory "
            "(lesson_id, topic, content, confidence, source, created_at) "
            "VALUES (?, ?, ?, ?, 'bridge', ?)",
            (row["lesson_id"], row["topic"], row["content"], confidence, now),
        )
        return True


def mark_applied(message_id: int) -> bool:
    """Flag that a lesson was actually *used*, not merely acknowledged."""
    with _write_lock, _connect() as conn:
        cur = conn.execute("UPDATE bridge SET applied = 1 WHERE id = ?", (message_id,))
        return cur.rowcount > 0


# ── reads ────────────────────────────────────────────────────────────────────

def get_lessons(recipient: str, states=("new", "delivered", "read"), limit: int = 50) -> list:
    """Lessons addressed to ``recipient`` currently sitting in any of ``states``."""
    init_db()
    recipient = (recipient or "").strip().lower()
    states = tuple(states)
    if not states:
        return []
    placeholders = ",".join("?" for _ in states)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM bridge WHERE recipient = ? AND status IN ({placeholders}) "
            f"ORDER BY id ASC LIMIT ?",
            (recipient, *states, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_pending_lessons(recipient: str = "nxvana") -> list:
    """Everything not yet acknowledged — the sender's back-pressure check."""
    return get_lessons(recipient, states=("new", "delivered", "read"))


def get_acknowledged_lessons(recipient: str = "nxvana", limit: int = 20) -> list:
    """Lessons the recipient has signed off on, newest first."""
    init_db()
    recipient = (recipient or "").strip().lower()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bridge WHERE recipient = ? AND status = 'acknowledged' "
            "ORDER BY id DESC LIMIT ?",
            (recipient, limit),
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def get_skills(topic: str | None = None) -> list:
    """What the recipient actually learned, highest confidence first."""
    init_db()
    with _connect() as conn:
        if topic:
            rows = conn.execute(
                "SELECT * FROM skill_memory WHERE topic = ? ORDER BY confidence DESC",
                (topic,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skill_memory ORDER BY confidence DESC, id ASC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_conversation_history(limit: int = 20) -> list:
    """Recent bridge traffic, both directions, oldest first."""
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bridge ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in reversed(rows)]


def status_counts(recipient: str | None = None) -> dict:
    """How many lessons sit in each state."""
    init_db()
    with _connect() as conn:
        if recipient:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM bridge WHERE recipient = ? GROUP BY status",
                ((recipient or "").strip().lower(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM bridge GROUP BY status"
            ).fetchall()

    counts = {s: 0 for s in STATES}
    for status, n in rows:
        counts[status] = n
    return counts


# ── CLI ──────────────────────────────────────────────────────────────────────

def _states_line(recipient=None) -> str:
    counts = status_counts(recipient)
    return "  ".join(f"{s}={counts[s]}" for s in STATES)


def demo() -> int:
    """End-to-end walkthrough: push, claim, read, acknowledge."""
    import tempfile

    print(f"nxa-bridge v{__version__} — demo")
    print("=" * 62)

    with tempfile.TemporaryDirectory() as tmp:
        configure(os.path.join(tmp, "demo.db"))
        reset_db()

        print("\n1. creator pushes a lesson")
        send_message(
            "creator", "nxvana",
            "Enable WAL mode on every SQLite connection: it allows concurrent "
            "readers while a write is in flight.",
            topic="sqlite_concurrency", lesson_id="l-001",
        )
        print("   [", _states_line("nxvana"), "]")

        print("\n2. pushing the same lesson_id again is a no-op (idempotent)")
        again = send_message(
            "creator", "nxvana", "duplicate payload",
            topic="sqlite_concurrency", lesson_id="l-001",
        )
        print(f"   returned: {again}  (None == already queued)")
        print("   [", _states_line("nxvana"), "]")

        print("\n3. nxvana claims it  ->  delivered")
        claimed = claim_messages("nxvana")
        print(f"   claimed id={claimed[0]['id']}  delivered_at={claimed[0]['delivered_at']}")
        print("   [", _states_line("nxvana"), "]")

        print("\n4. nxvana reads it  ->  read")
        read = read_messages("nxvana")
        print(f"   read id={read[0]['id']}  read_at={read[0]['read_at']}")
        print("   [", _states_line("nxvana"), "]")

        print("\n5. acknowledging out of order is refused (state machine holds)")
        second = send_message("creator", "nxvana", "second lesson", lesson_id="l-002")
        print(f"   queued a second lesson as id={second} (still 'new')")
        print(f"   acknowledge(id={second}) while 'new'  ->  "
              f"{acknowledge_message(second, 'premature')}  (False = refused)")
        print("   [", _states_line("nxvana"), "]")

        print("\n6. nxvana acknowledges it  ->  acknowledged, and it becomes a skill")
        ok = acknowledge_message(
            claimed[0]["id"], "Learned: WAL enables concurrent reads.", confidence=0.8
        )
        print(f"   acknowledge -> {ok}")
        print("   [", _states_line("nxvana"), "]")

        print("\n7. skill memory")
        for s in get_skills():
            print(f"   {s['topic']:<24} confidence={s['confidence']}  lesson={s['lesson_id']}")

        print("\n8. full audit trail")
        row = get_conversation_history()[0]
        print(f"   created_at   {row['created_at']}")
        print(f"   delivered_at {row['delivered_at']}")
        print(f"   read_at      {row['read_at']}")
        print(f"   acked_at     {row['acked_at']}")

        print("\n9. back-pressure check (get_pending_lessons)")
        pending = get_pending_lessons("nxvana")
        print(f"   {len(pending)} still in flight: "
              f"{[(p['id'], p['status']) for p in pending]}")

        print("\n" + "=" * 62)
        print("four states, four timestamps, zero dependencies.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="bridge", description="nxa-bridge — AI-to-AI lesson bridge"
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("demo", help="run an end-to-end walkthrough")

    sp = sub.add_parser("status", help="counts per state")
    sp.add_argument("--recipient", default=None)

    sp = sub.add_parser("push", help="push a lesson (new)")
    sp.add_argument("--sender", required=True)
    sp.add_argument("--recipient", required=True)
    sp.add_argument("--content", required=True)
    sp.add_argument("--topic", default="general")
    sp.add_argument("--lesson-id", default=None)

    for name, helptext in (("claim", "new -> delivered"), ("read", "delivered -> read")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--recipient", required=True)
        sp.add_argument("--limit", type=int, default=10)

    sp = sub.add_parser("ack", help="read -> acknowledged")
    sp.add_argument("--id", type=int, required=True, dest="message_id")
    sp.add_argument("--text", required=True)
    sp.add_argument("--confidence", type=float, default=0.6)

    sub.add_parser("skills", help="list accumulated skills")

    sp = sub.add_parser("history", help="recent bridge traffic")
    sp.add_argument("--limit", type=int, default=20)

    args = p.parse_args(argv)

    if args.cmd == "demo":
        return demo()

    if args.cmd == "status":
        print(json.dumps(status_counts(args.recipient), indent=2))
        return 0

    if args.cmd == "push":
        new_id = send_message(
            args.sender, args.recipient, args.content,
            topic=args.topic, lesson_id=args.lesson_id,
        )
        if new_id is None:
            print("duplicate — lesson_id already queued")
            return 1
        print(f"queued id={new_id}")
        return 0

    if args.cmd in ("claim", "read"):
        fn = claim_messages if args.cmd == "claim" else read_messages
        rows = fn(args.recipient, limit=args.limit)
        print(json.dumps(rows, indent=2) if rows else "nothing to move")
        return 0

    if args.cmd == "ack":
        ok = acknowledge_message(args.message_id, args.text, confidence=args.confidence)
        print("acknowledged" if ok else "refused — lesson is not in 'read'")
        return 0 if ok else 1

    if args.cmd == "skills":
        print(json.dumps(get_skills(), indent=2))
        return 0

    if args.cmd == "history":
        print(json.dumps(get_conversation_history(args.limit), indent=2))
        return 0

    p.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())



