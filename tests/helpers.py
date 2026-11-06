import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dqmon.checks import Context  # noqa: E402
from dqmon.connectors import Source  # noqa: E402


def make_source(ddl: str, rows: dict[str, list[tuple]] | None = None) -> Source:
    con = sqlite3.connect(":memory:")
    con.executescript(ddl)
    for table, data in (rows or {}).items():
        marks = ",".join("?" * len(data[0]))
        con.executemany(f"INSERT INTO {table} VALUES ({marks})", data)
    con.commit()
    return Source(con)


def ctx(source, history=None, as_of=None, suite="t"):
    return Context(source=source, history=history, suite_name=suite,
                   as_of=as_of or datetime(2025, 1, 10, 12, 0))
