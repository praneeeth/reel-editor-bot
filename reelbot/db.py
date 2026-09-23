"""SQLite job state + per-step usage metrics."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .states import TERMINAL, Event, JobState, next_state

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    instruction TEXT,
    feedback TEXT,                 -- latest pending feedback for plan/revise
    clips TEXT NOT NULL DEFAULT '[]',
    session_id TEXT,               -- agent session (only used when LEAN_MODE=false)
    status_message_id INTEGER,
    revision INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    delivered_at REAL,
    cleaned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    step TEXT NOT NULL,
    backend TEXT NOT NULL,
    ok INTEGER NOT NULL,
    turns INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cost_usd REAL,
    wall_s REAL NOT NULL,
    detail TEXT,
    created_at REAL NOT NULL
);
"""


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    state: JobState
    instruction: str | None = None
    feedback: str | None = None
    clips: list[str] = field(default_factory=list)
    session_id: str | None = None
    status_message_id: int | None = None
    revision: int = 0
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    delivered_at: float | None = None
    cleaned: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        d = dict(row)
        d["state"] = JobState(d["state"])
        d["clips"] = json.loads(d["clips"] or "[]")
        return cls(**d)


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- jobs -------------------------------------------------------------------
    def create_job(self, user_id: int, chat_id: int) -> Job:
        now = time.time()
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.conn.execute(
            "INSERT INTO jobs (id, user_id, chat_id, state, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, user_id, chat_id, JobState.COLLECTING.value, now, now),
        )
        self.conn.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def active_job(self, user_id: int) -> Job | None:
        placeholders = ",".join("?" * len(TERMINAL))
        row = self.conn.execute(
            f"SELECT * FROM jobs WHERE user_id = ? AND state NOT IN ({placeholders}) "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, *[s.value for s in TERMINAL]),
        ).fetchone()
        return Job.from_row(row) if row else None

    def latest_job(self, user_id: int) -> Job | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 1", (user_id,)
        ).fetchone()
        return Job.from_row(row) if row else None

    def jobs_in_states(self, states: set[JobState] | frozenset[JobState]) -> list[Job]:
        placeholders = ",".join("?" * len(states))
        rows = self.conn.execute(
            f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY updated_at",
            [s.value for s in states],
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    def update(self, job_id: str, **fields) -> Job:
        if "clips" in fields:
            fields["clips"] = json.dumps(fields["clips"])
        if "state" in fields:
            fields["state"] = JobState(fields["state"]).value
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))
        self.conn.commit()
        return self.get(job_id)

    def transition(self, job_id: str, event: Event | str, **fields) -> Job:
        job = self.get(job_id)
        new = next_state(job.state, event)  # raises InvalidTransition
        if new == JobState.DELIVERED:
            fields.setdefault("delivered_at", time.time())
        return self.update(job_id, state=new, **fields)

    def add_clip(self, job_id: str, path: str) -> Job:
        job = self.get(job_id)
        return self.update(job_id, clips=[*job.clips, path])

    def jobs_to_clean(self, older_than_s: float) -> list[Job]:
        cutoff = time.time() - older_than_s
        rows = self.conn.execute(
            "SELECT * FROM jobs WHERE cleaned = 0 AND ("
            " (state = ? AND delivered_at < ?) OR (state IN (?, ?) AND updated_at < ?))",
            (JobState.DELIVERED.value, cutoff, JobState.CANCELLED.value, JobState.FAILED.value,
             cutoff),
        ).fetchall()
        return [Job.from_row(r) for r in rows]

    # ---- metrics ----------------------------------------------------------------
    def log_metric(self, job_id: str, step: str, backend: str, ok: bool, wall_s: float,
                   turns: int | None = None, input_tokens: int | None = None,
                   output_tokens: int | None = None, cache_read_tokens: int | None = None,
                   cache_write_tokens: int | None = None, cost_usd: float | None = None,
                   detail: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO metrics (job_id, step, backend, ok, turns, input_tokens, output_tokens,"
            " cache_read_tokens, cache_write_tokens, cost_usd, wall_s, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, step, backend, int(ok), turns, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, cost_usd, wall_s, detail, time.time()),
        )
        self.conn.commit()

    def metrics_for(self, job_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM metrics WHERE job_id = ? ORDER BY id", (job_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def all_metrics(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM metrics ORDER BY id")]
