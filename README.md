# nxa-bridge

A minimal, dependency-free bridge for **AI-to-AI teaching**.

One agent pushes a lesson. Another claims it, reads it, saves it as a skill, and
signs off — with every hand-off timestamped. No shared memory, no shared
process, no framework. Just a SQLite table and four states.

```
new ──▶ delivered ──▶ read ──▶ acknowledged
```

## Why

Agents can call tools. Agents can talk to humans. Agents generally **cannot
teach each other** — knowledge transfer between agents is ad-hoc, lossy, and
invisible.

`nxa-bridge` makes the hand-off explicit and auditable. Each state is a distinct
event with its own timestamp:

| state | meaning | timestamp |
|---|---|---|
| `new` | a lesson has been pushed | `created_at` |
| `delivered` | the recipient claimed it | `delivered_at` |
| `read` | the recipient parsed it | `read_at` |
| `acknowledged` | the recipient signed off — it is now a skill | `acked_at` |

Transitions are **guarded**. An illegal jump is refused, not silently applied.
The `status` column carries a `CHECK` constraint, so a typo can't invent a state
that nothing reads.

## Install

Nothing to install. Python 3.10+ and the standard library:

```bash
git clone https://github.com/Kujay272/Nxvana-AI-to-AI-Bridge-lesson-curriculum.git
cd Nxvana-AI-to-AI-Bridge-lesson-curriculum
python bridge.py demo
```

No `pip install`. No `requirements.txt`. `sqlite3` ships with Python.

## Quickstart

```bash
python bridge.py demo
```

A full four-state walkthrough on a throwaway database:

```
nxa-bridge v2.0.0 — demo
==============================================================

1. creator pushes a lesson
   [ new=1  delivered=0  read=0  acknowledged=0 ]

2. pushing the same lesson_id again is a no-op (idempotent)
   returned: None  (None == already queued)
   [ new=1  delivered=0  read=0  acknowledged=0 ]

3. nxvana claims it  ->  delivered
   [ new=0  delivered=1  read=0  acknowledged=0 ]

4. nxvana reads it  ->  read
   [ new=0  delivered=0  read=1  acknowledged=0 ]

5. acknowledging out of order is refused (state machine holds)
   queued a second lesson as id=2 (still 'new')
   acknowledge(id=2) while 'new'  ->  False  (False = refused)

6. nxvana acknowledges it  ->  acknowledged, and it becomes a skill
   [ new=0  delivered=0  read=0  acknowledged=1 ]

7. skill memory
   sqlite_concurrency       confidence=0.8  lesson=l-001

8. full audit trail
   created_at   2026-09-28T19:09:23+00:00
   delivered_at 2026-09-28T19:09:23+00:00
   read_at      2026-09-28T19:09:23+00:00
   acked_at     2026-09-28T19:09:23+00:00

9. back-pressure check (get_pending_lessons)
   1 still in flight: [(2, 'new')]

==============================================================
four states, four timestamps, zero dependencies.
```

### As a library

```python
from bridge import (
    send_message, claim_messages, read_messages, acknowledge_message, get_skills,
)

send_message("teacher", "student", "Enable WAL mode on every connection.",
             topic="sqlite_concurrency", lesson_id="l-001")

lesson = claim_messages("student")[0]     # new -> delivered
read_messages("student")                  # delivered -> read
acknowledge_message(lesson["id"], "Learned: WAL allows concurrent readers.",
                    confidence=0.8)       # read -> acknowledged
print(get_skills())
```

## API

| function | transition | notes |
|---|---|---|
| `send_message(sender, recipient, content, topic, lesson_id)` | → `new` | idempotent on `lesson_id`; returns new id or `None` |
| `claim_messages(recipient, limit)` | `new` → `delivered` | stamps `delivered_at` |
| `read_messages(recipient, limit)` | `delivered` → `read` | stamps `read_at` |
| `acknowledge_message(id, ack_text, confidence)` | `read` → `acknowledged` | writes `skill_memory`; `False` if illegal |
| `mark_applied(id)` | — | flags the lesson as actually used |
| `get_lessons` / `get_pending_lessons` | — | read-only |
| `get_acknowledged_lessons` / `get_skills` | — | read-only |
| `get_conversation_history(limit)` | — | both directions, oldest first |
| `status_counts(recipient)` | — | `{state: count}` |

CLI subcommands mirror the API: `demo`, `status`, `push`, `claim`, `read`,
`ack`, `skills`, `history`.

## Schema

```sql
bridge (
  id, lesson_id UNIQUE, sender, recipient, content, topic,
  status CHECK (status IN ('new','delivered','read','acknowledged')),
  created_at, delivered_at, read_at, acked_at, ack_text, applied
)

skill_memory (
  id, lesson_id UNIQUE, topic, content, confidence, source, created_at
)
```

Storage path defaults to `nxa_bridge.db` beside the module. Override with the
`NXA_BRIDGE_DB` environment variable or `bridge.configure(path)`.

## Design notes

**The duplicate check is the insert.** `send_message` does not
`SELECT`-then-`INSERT`; it inserts inside one lock and one transaction and treats
a `UNIQUE` violation as "already queued". Two senders racing the same
`lesson_id` cannot both win.

**Only `IntegrityError` means duplicate.** Every other database failure raises
and is reported. A bridge that swallows errors and calls them duplicates will
eventually hide a full disk.

**Connections are closed.** `with sqlite3.connect(...)` commits but does *not*
close. The context manager here wraps both.

## Provenance

This is the standalone edition of the lesson bridge that runs inside
**Nxvana**, where it has carried the Creator → agent curriculum in production.

The in-agent original grew under time pressure and left two things unfinished:
it *read* a `delivered` state but never wrote one, and it enforced its state
machine only by convention. This edition closes both — `claim_messages` makes
`delivered` a real, timestamped transition, and the `CHECK` constraint plus
guarded updates make illegal transitions impossible rather than merely
unlikely.

No third-party code. Original work, extracted and hardened.

## License

MIT — see [LICENSE](LICENSE).
