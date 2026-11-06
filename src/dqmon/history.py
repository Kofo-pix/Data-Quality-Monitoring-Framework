"""Run history: results, metrics and schema snapshots, persisted in a small SQLite file.

This is what turns one-off checks into *monitoring*: volume anomalies, null-rate drift and
schema drift are all judged against what previous runs observed.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_DDL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, suite TEXT, started_at TEXT, finished_at TEXT,
    total INTEGER, passed INTEGER, failed INTEGER, errored INTEGER, skipped INTEGER,
    score REAL, gate_passed INTEGER);
CREATE TABLE IF NOT EXISTS results (
    run_id INTEGER, check_name TEXT, check_type TEXT, tbl TEXT, col TEXT, severity TEXT,
    status TEXT, failed_count INTEGER, total_count INTEGER, message TEXT, sample_json TEXT,
    duration_ms REAL);
CREATE TABLE IF NOT EXISTS metrics (
    run_id INTEGER, suite TEXT, tbl TEXT, col TEXT, metric TEXT, value REAL);
CREATE TABLE IF NOT EXISTS schema_snapshots (
    run_id INTEGER, suite TEXT, tbl TEXT, col TEXT, type TEXT, position INTEGER);
CREATE INDEX IF NOT EXISTS ix_metrics ON metrics (suite, tbl, col, metric, run_id);
"""


class History:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(_DDL)

    def start_run(self, suite: str, started_at: str) -> int:
        cur = self.con.execute("INSERT INTO runs (suite, started_at) VALUES (?, ?)", (suite, started_at))
        self.con.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, summary, finished_at: str) -> None:
        self.con.execute(
            """UPDATE runs SET finished_at=?, total=?, passed=?, failed=?, errored=?, skipped=?,
                   score=?, gate_passed=? WHERE run_id=?""",
            (finished_at, summary.total, summary.passed, summary.failed, summary.errored,
             summary.skipped, summary.score, int(summary.gate_passed), run_id),
        )
        self.con.commit()

    def save_results(self, run_id: int, results) -> None:
        self.con.executemany(
            "INSERT INTO results VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r.name, r.check_type, r.table, r.column, r.severity, r.status, r.failed_count,
              r.total_count, r.message, json.dumps(r.sample, default=str), r.duration_ms)
             for r in results],
        )
        self.con.commit()

    def save_metrics(self, run_id: int, suite: str, rows) -> None:
        self.con.executemany(
            "INSERT INTO metrics VALUES (?,?,?,?,?,?)", [(run_id, suite, *r) for r in rows]
        )
        self.con.commit()

    def save_schema(self, run_id: int, suite: str, table: str, columns) -> None:
        self.con.executemany(
            "INSERT INTO schema_snapshots VALUES (?,?,?,?,?,?)",
            [(run_id, suite, table, name, typ, i) for i, (name, typ) in enumerate(columns)],
        )
        self.con.commit()

    def metric_series(self, suite: str, table: str, column: str, metric: str, limit: int = 7) -> list[float]:
        """Most recent `limit` observations, oldest first."""
        rows = self.con.execute(
            """SELECT value FROM metrics WHERE suite=? AND tbl=? AND col=? AND metric=?
               ORDER BY run_id DESC LIMIT ?""",
            (suite, table, column or "", metric, limit),
        ).fetchall()
        return [r[0] for r in reversed(rows)]

    def last_schema(self, suite: str, table: str) -> dict[str, str] | None:
        row = self.con.execute(
            "SELECT MAX(run_id) FROM schema_snapshots WHERE suite=? AND tbl=?", (suite, table)
        ).fetchone()
        if not row or row[0] is None:
            return None
        cols = self.con.execute(
            "SELECT col, type FROM schema_snapshots WHERE run_id=? AND suite=? AND tbl=? ORDER BY position",
            (row[0], suite, table),
        ).fetchall()
        return {c["col"]: c["type"] for c in cols}

    def runs(self, suite: str | None = None, limit: int = 20) -> list[dict]:
        sql = "SELECT * FROM runs WHERE finished_at IS NOT NULL"
        params: list = []
        if suite:
            sql += " AND suite = ?"
            params.append(suite)
        sql += " ORDER BY run_id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in reversed(self.con.execute(sql, params).fetchall())]

    def close(self) -> None:
        self.con.close()
