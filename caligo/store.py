"""Durable thread state, steering inbox, and replayable events."""

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

from .common import CaligoError, identifier, now, valid_id


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY, attempt TEXT NOT NULL, state TEXT NOT NULL,
                    backend TEXT NOT NULL, goal TEXT NOT NULL, backend_id TEXT,
                    continuous INTEGER NOT NULL, created_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS active_writer ON threads(attempt)
                    WHERE state NOT IN ('STOPPED', 'COMPLETED');
                DROP INDEX IF EXISTS writer;
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, thread TEXT NOT NULL,
                    type TEXT NOT NULL, data TEXT NOT NULL, at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS event_thread ON events(thread, seq);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, thread TEXT NOT NULL, text TEXT NOT NULL,
                    delivery TEXT NOT NULL, author TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run_updates (
                    thread TEXT NOT NULL, run TEXT NOT NULL, data TEXT NOT NULL,
                    notified INTEGER NOT NULL, PRIMARY KEY(thread,run));
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(threads)")}
            for column in ("image", "model"):
                if column not in columns:
                    db.execute("ALTER TABLE threads ADD COLUMN %s TEXT" % column)
            if "automatic_turns" not in columns:
                db.execute(
                    "ALTER TABLE threads ADD COLUMN automatic_turns INTEGER NOT NULL DEFAULT 0"
                )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
            if "origin" not in columns:
                db.execute(
                    "ALTER TABLE events ADD COLUMN origin TEXT NOT NULL DEFAULT 'caligo'"
                )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, thread, kind, data, origin="caligo"):
        db.execute(
            "INSERT INTO events(thread,type,data,at,origin) VALUES(?,?,?,?,?)",
            (thread, kind, json.dumps(data), now(), origin),
        )

    def append(self, thread, kind, data, origin="caligo"):
        with self.connect() as db:
            self.event(db, thread, kind, data, origin)

    def create(self, attempt, backend, goal, continuous=True, image=None, model=None):
        thread = identifier("thread-")
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO threads(id,attempt,state,backend,goal,backend_id,continuous,created_at,image,model) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        thread,
                        valid_id(attempt),
                        "STARTING",
                        backend,
                        goal,
                        None,
                        int(continuous),
                        now(),
                        image,
                        model,
                    ),
                )
                self.event(
                    db,
                    thread,
                    "thread.created",
                    {
                        "attempt": attempt,
                        "backend": backend,
                        "goal": goal,
                        "model": model,
                    },
                )
        except sqlite3.IntegrityError as error:
            raise CaligoError(
                "This attempt already has an active thread; resume or stop it first"
            ) from error
        return self.thread(thread)

    def thread(self, thread):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM threads WHERE id=?", (valid_id(thread),)
            ).fetchone()
        if row is None:
            raise CaligoError("Unknown thread: " + thread)
        return dict(row)

    def threads(self):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM threads ORDER BY created_at,id")
            ]

    def cursor(self, thread):
        with self.connect() as db:
            return db.execute(
                "SELECT COALESCE(MAX(seq),0) FROM events WHERE thread=?", (thread,)
            ).fetchone()[0]

    def transition(self, thread, expected, state):
        with self.connect() as db:
            changed = db.execute(
                "UPDATE threads SET state=? WHERE id=? AND state IN ("
                + ",".join("?" for _ in expected)
                + ")",
                (state, thread, *expected),
            ).rowcount
            if changed:
                self.event(db, thread, "thread.updated", {"state": state})
            return bool(changed)

    def update(self, thread, **values):
        if not values or set(values) - {"state", "backend_id", "automatic_turns"}:
            raise ValueError("Unsupported thread update")
        with self.connect() as db:
            db.execute(
                "UPDATE threads SET "
                + ",".join(key + "=?" for key in values)
                + " WHERE id=?",
                (*values.values(), thread),
            )
            self.event(db, thread, "thread.updated", values)

    def complete_goal(self, thread, summary):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE threads SET state='COMPLETING' WHERE id=? "
                "AND state='WAITING' AND NOT EXISTS "
                "(SELECT 1 FROM messages WHERE thread=? AND state IN ('QUEUED','SENDING'))",
                (thread, thread),
            ).rowcount
            if changed:
                self.event(db, thread, "goal.completed", {"summary": summary})
                self.event(db, thread, "thread.updated", {"state": "COMPLETING"})
            return bool(changed)

    def events(self, thread, after=0, limit=500):
        self.thread(thread)
        if int(after) < 0:
            raise CaligoError("Event cursor must be nonnegative")
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE thread=? AND seq>? ORDER BY seq LIMIT ?",
                (thread, int(after), min(1000, max(1, int(limit)))),
            ).fetchall()
        return [
            {**dict(row), "version": 1, "data": json.loads(row["data"])} for row in rows
        ]

    def enqueue(self, thread, text, delivery="queue", author="local", message_id=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 1024 * 1024:
            raise CaligoError("Message must contain 1 to 1048576 characters")
        if delivery not in {"queue", "steer"}:
            raise CaligoError("Delivery must be queue or steer")
        message_id = valid_id(message_id) if message_id else identifier("message-")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT * FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if existing:
                if any(
                    existing[key] != value
                    for key, value in dict(
                        thread=thread, text=text, delivery=delivery, author=author
                    ).items()
                ):
                    raise CaligoError(
                        "Message ID was already used with different content"
                    )
                return dict(existing)
            row = db.execute(
                "SELECT state FROM threads WHERE id=?", (thread,)
            ).fetchone()
            if row is None:
                raise CaligoError("Unknown thread: " + thread)
            if row["state"] in {"STOPPED", "COMPLETING", "COMPLETED"}:
                raise CaligoError("Thread is finished; create a new thread")
            db.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                (message_id, thread, text, delivery, author, "QUEUED", now()),
            )
            self.event(
                db,
                thread,
                "message.accepted",
                {
                    "id": message_id,
                    "text": text,
                    "delivery": delivery,
                    "author": author,
                },
            )
        return {"id": message_id, "state": "QUEUED"}

    def pending(self, thread):
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM messages WHERE thread=? AND state='QUEUED' ORDER BY rowid",
                    (thread,),
                )
            ]

    def message_state(self, thread, message_id, state):
        with self.connect() as db:
            db.execute(
                "UPDATE messages SET state=? WHERE id=? AND thread=?",
                (state, message_id, thread),
            )
            self.event(db, thread, "message." + state.lower(), {"id": message_id})

    def recover(self, thread):
        # A crash between write-to-backend and acknowledgement is ambiguous.
        # Preserve this evidence; never blindly replay an instruction.
        with self.connect() as db:
            db.execute(
                "UPDATE messages SET state='DELIVERY_UNKNOWN' WHERE thread=? AND state='SENDING'",
                (thread,),
            )
            self.event(db, thread, "runtime.recovered", {})

    def observe_run(self, thread, run, status, terminal):
        data = json.dumps(status, sort_keys=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM run_updates WHERE thread=? AND run=?", (thread, run)
            ).fetchone()
            if old and old["data"] == data:
                return
            notified = bool(old and old["notified"])
            self.event(db, thread, "run.updated", dict(id=run, **status))
            if terminal and not notified:
                message = identifier("result-")
                text = (
                    "Experiment "
                    + run
                    + " finished: "
                    + data
                    + ". Inspect its logs and artifacts before continuing."
                )
                db.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                    (message, thread, text, "queue", "scheduler", "QUEUED", now()),
                )
                self.event(
                    db,
                    thread,
                    "message.accepted",
                    {
                        "id": message,
                        "text": text,
                        "author": "scheduler",
                        "delivery": "queue",
                    },
                )
                notified = True
            db.execute(
                "INSERT OR REPLACE INTO run_updates VALUES(?,?,?,?)",
                (thread, run, data, int(notified)),
            )
