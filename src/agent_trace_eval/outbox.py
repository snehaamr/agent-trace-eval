"""SQLite outbox for eval jobs. Enqueue returns immediately; a worker picks up."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import REPO_ROOT

DEFAULT_DB = REPO_ROOT / "artifacts" / "eval_jobs.sqlite"
MAX_ATTEMPTS = 3

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass
class Job:
    id: int
    status: str
    attempts: int
    payload: dict[str, Any]
    result: dict[str, Any] | None
    error: str | None


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL,
            result_json TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            started_at REAL,
            finished_at REAL
        )
        """
    )
    return conn


def enqueue(payload: dict[str, Any] | None = None, *, path: Path | None = None) -> int:
    conn = connect(path)
    cur = conn.execute(
        "INSERT INTO eval_jobs (status, payload_json, created_at) VALUES (?, ?, ?)",
        (PENDING, json.dumps(payload or {}), time.time()),
    )
    job_id = int(cur.lastrowid)
    conn.close()
    return job_id


def pickup(*, path: Path | None = None, max_attempts: int = MAX_ATTEMPTS) -> Job | None:
    conn = connect(path)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        """
        SELECT * FROM eval_jobs
        WHERE status = ? AND attempts < ?
        ORDER BY id ASC
        LIMIT 1
        """,
        (PENDING, max_attempts),
    ).fetchone()
    if row is None:
        conn.execute("COMMIT")
        conn.close()
        return None
    conn.execute(
        "UPDATE eval_jobs SET status = ?, attempts = attempts + 1, started_at = ? WHERE id = ?",
        (RUNNING, time.time(), row["id"]),
    )
    conn.execute("COMMIT")
    job = _job(conn.execute("SELECT * FROM eval_jobs WHERE id = ?", (row["id"],)).fetchone())
    conn.close()
    return job


def complete(job_id: int, result: dict[str, Any], *, path: Path | None = None) -> None:
    conn = connect(path)
    conn.execute(
        """
        UPDATE eval_jobs
        SET status = ?, result_json = ?, error = NULL, finished_at = ?
        WHERE id = ?
        """,
        (SUCCEEDED, json.dumps(result), time.time(), job_id),
    )
    conn.close()


def fail(job_id: int, error: str, *, path: Path | None = None, retry: bool = True) -> None:
    conn = connect(path)
    row = conn.execute("SELECT attempts FROM eval_jobs WHERE id = ?", (job_id,)).fetchone()
    attempts = int(row["attempts"]) if row else MAX_ATTEMPTS
    status = PENDING if retry and attempts < MAX_ATTEMPTS else FAILED
    conn.execute(
        "UPDATE eval_jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
        (status, error, time.time(), job_id),
    )
    conn.close()


def get_job(job_id: int, *, path: Path | None = None) -> Job | None:
    conn = connect(path)
    row = conn.execute("SELECT * FROM eval_jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return _job(row) if row else None


def _job(row: sqlite3.Row) -> Job:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return Job(
        id=int(row["id"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        payload=json.loads(row["payload_json"] or "{}"),
        result=result,
        error=row["error"],
    )
