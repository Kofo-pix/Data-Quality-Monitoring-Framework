"""Data sources. Checks only ever *read*: SQLite files are opened read-only."""
from __future__ import annotations

import csv
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Strict quoting for identifiers that come from config files (prevents SQL injection)."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return f'"{name}"'


def quote_any(name: str) -> str:
    """Lenient quoting for identifiers discovered from the database itself."""
    return '"' + str(name).replace('"', '""') + '"'


@lru_cache(maxsize=256)
def _compile(pattern: str):
    return re.compile(pattern)


def _regexp(pattern, value):
    return 1 if value is not None and _compile(pattern).search(str(value)) else 0


def _jsonable(v):
    return v.hex() if isinstance(v, (bytes, bytearray)) else v


class Source:
    def __init__(self, con: sqlite3.Connection, label: str = "memory"):
        self.con = con
        self.label = label
        self.con.row_factory = sqlite3.Row
        self.con.create_function("REGEXP", 2, _regexp)

    # -- constructors ----------------------------------------------------------------
    @classmethod
    def from_sqlite(cls, path: str | Path) -> "Source":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"SQLite database not found: {p}")
        return cls(sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True), str(p))

    @classmethod
    def from_csv_dir(cls, directory: str | Path) -> "Source":
        d = Path(directory)
        files = sorted(d.glob("*.csv"))
        if not files:
            raise FileNotFoundError(f"No CSV files found in {d}")
        con = sqlite3.connect(":memory:")
        for f in files:
            with f.open(newline="", encoding="utf-8-sig") as fh:
                rows = list(csv.reader(fh))
            header, data = rows[0], rows[1:]
            types = [_infer([r[i] if i < len(r) else "" for r in data]) for i in range(len(header))]
            cols = ", ".join(f"{quote_any(h)} {t}" for h, t in zip(header, types))
            con.execute(f"CREATE TABLE {quote_any(f.stem)} ({cols})")
            conv = [
                tuple(_convert(r[i] if i < len(r) else "", types[i]) for i in range(len(header)))
                for r in data
            ]
            con.executemany(
                f"INSERT INTO {quote_any(f.stem)} VALUES ({','.join('?' * len(header))})", conv
            )
        con.commit()
        return cls(con, str(d))

    @classmethod
    def from_spec(cls, spec: dict) -> "Source":
        kind = spec.get("type", "sqlite")
        if kind == "sqlite":
            return cls.from_sqlite(spec["path"])
        if kind == "csv":
            return cls.from_csv_dir(spec["dir"])
        raise ValueError(f"Unknown source type: {kind!r}")

    # -- helpers ---------------------------------------------------------------------
    def tables(self) -> list[str]:
        rows = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [r[0] for r in rows]

    def columns(self, table: str) -> list[tuple[str, str]]:
        rows = self.con.execute(f"PRAGMA table_info({quote_any(table)})").fetchall()
        if not rows:
            raise LookupError(f"Table not found: {table}")
        return [(r["name"], r["type"]) for r in rows]

    def scalar(self, sql: str, params=()):
        row = self.con.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def query(self, sql: str, params=()) -> list[dict]:
        cur = self.con.execute(sql, tuple(params))
        return [{k: _jsonable(r[k]) for k in r.keys()} for r in cur.fetchall()]

    def close(self) -> None:
        self.con.close()


def _infer(values: list[str]) -> str:
    vals = [v for v in values if v != ""]
    if not vals:
        return "TEXT"
    for caster, name in ((int, "INTEGER"), (float, "REAL")):
        try:
            for v in vals:
                caster(v)
            return name
        except ValueError:
            continue
    return "TEXT"


def _convert(value: str, typ: str):
    if typ == "TEXT":
        return value
    if value == "":
        return None
    return int(value) if typ == "INTEGER" else float(value)
