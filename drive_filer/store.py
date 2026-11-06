"""Local state in SQLite: what has been filed, the undo log, and Sheet rows still waiting to be written.

The undo log is written in two steps around every Drive change: a "pending"
row with the file's state before and the intended state after, then the Drive
PATCH, then the row is marked applied. If the process dies in between, the
next run checks Drive and either confirms the change or drops the pending row,
so every change the filer made can always be reversed.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  file_id TEXT PRIMARY KEY, content_hash TEXT, status TEXT, name TEXT, folder TEXT,
  run_id TEXT, attempts INTEGER DEFAULT 0, note TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS files_hash ON files(content_hash);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, file_id TEXT, at TEXT,
  before TEXT, after TEXT, state TEXT, undone_at TEXT);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, started TEXT, finished TEXT, dry_run INTEGER, cost_usd REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS sheet_outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, row TEXT);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # --- files -----------------------------------------------------------
    def file(self, file_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()

    def set_file(self, file_id: str, **cols) -> None:
        cols["updated_at"] = now_iso()
        existing = self.file(file_id)
        if existing is None:
            cols["file_id"] = file_id
            keys = ", ".join(cols)
            self.db.execute(f"INSERT INTO files ({keys}) VALUES ({', '.join('?' * len(cols))})", tuple(cols.values()))
        else:
            sets = ", ".join(f"{k} = ?" for k in cols)
            self.db.execute(f"UPDATE files SET {sets} WHERE file_id = ?", (*cols.values(), file_id))
        self.db.commit()

    def bump_attempts(self, file_id: str, note: str, run_id: str) -> int:
        row = self.file(file_id)
        attempts = (row["attempts"] if row else 0) + 1
        self.set_file(file_id, status="error", attempts=attempts, note=note[:500], run_id=run_id)
        return attempts

    def filed_with_hash(self, content_hash: str, exclude: str) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM files WHERE content_hash = ? AND file_id != ? AND status IN ('filed', 'unsorted') "
            "ORDER BY updated_at LIMIT 1", (content_hash, exclude)).fetchone()

    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM files GROUP BY status").fetchall()
        return {r["status"]: r["n"] for r in rows}

    def files_with_status(self, status: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM files WHERE status = ? ORDER BY updated_at", (status,)).fetchall()

    # --- undo log --------------------------------------------------------
    def begin_action(self, run_id: str, file_id: str, before: dict, after: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO actions (run_id, file_id, at, before, after, state) VALUES (?, ?, ?, ?, ?, 'pending')",
            (run_id, file_id, now_iso(), json.dumps(before), json.dumps(after)))
        self.db.commit()
        return cur.lastrowid

    def finish_action(self, action_id: int, state: str = "applied") -> None:
        self.db.execute("UPDATE actions SET state = ? WHERE id = ?", (state, action_id))
        self.db.commit()

    def mark_undone(self, action_id: int) -> None:
        self.db.execute("UPDATE actions SET undone_at = ? WHERE id = ?", (now_iso(), action_id))
        self.db.commit()

    def actions(self, *, run_id: Optional[str] = None, file_id: Optional[str] = None,
                state: Optional[str] = None, include_undone: bool = False) -> list[dict]:
        sql, args = "SELECT * FROM actions WHERE 1=1", []
        if run_id:
            sql += " AND run_id = ?"
            args.append(run_id)
        if file_id:
            sql += " AND file_id = ?"
            args.append(file_id)
        if state:
            sql += " AND state = ?"
            args.append(state)
        if not include_undone:
            sql += " AND undone_at IS NULL"
        rows = self.db.execute(sql + " ORDER BY id DESC", args).fetchall()
        return [{**dict(r), "before": json.loads(r["before"]), "after": json.loads(r["after"])} for r in rows]

    def last_run_with_actions(self) -> Optional[str]:
        row = self.db.execute("SELECT run_id FROM actions WHERE state = 'applied' AND undone_at IS NULL "
                              "ORDER BY id DESC LIMIT 1").fetchone()
        return row["run_id"] if row else None

    # --- runs ------------------------------------------------------------
    def start_run(self, run_id: str, dry_run: bool) -> None:
        self.db.execute("INSERT INTO runs (run_id, started, dry_run) VALUES (?, ?, ?)", (run_id, now_iso(), int(dry_run)))
        self.db.commit()

    def finish_run(self, run_id: str, cost: float, summary: dict) -> None:
        self.db.execute("UPDATE runs SET finished = ?, cost_usd = ?, summary = ? WHERE run_id = ?",
                        (now_iso(), cost, json.dumps(summary), run_id))
        self.db.commit()

    def runs(self, limit: int = 10) -> list[dict]:
        rows = self.db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "summary": json.loads(r["summary"] or "{}")} for r in rows]

    def total_cost(self) -> float:
        return float(self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs").fetchone()[0])

    # --- Sheet outbox ----------------------------------------------------
    def queue_row(self, row: dict) -> None:
        self.db.execute("INSERT INTO sheet_outbox (row) VALUES (?)", (json.dumps(row),))
        self.db.commit()

    def queued_rows(self) -> list[tuple[int, dict]]:
        return [(r["id"], json.loads(r["row"])) for r in self.db.execute("SELECT * FROM sheet_outbox ORDER BY id")]

    def clear_rows(self, ids: list[int]) -> None:
        self.db.executemany("DELETE FROM sheet_outbox WHERE id = ?", [(i,) for i in ids])
        self.db.commit()

    # --- key/value (changes page token) ----------------------------------
    def get(self, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()
