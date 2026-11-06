"""Per-run profiling metrics recorded to history (row counts, null rates)."""
from __future__ import annotations

from .connectors import Source, quote_any


def null_rate(source: Source, table: str, column: str) -> float:
    c = quote_any(column)
    row = source.con.execute(
        f"""SELECT COUNT(*), SUM(CASE WHEN {c} IS NULL OR TRIM(CAST({c} AS TEXT)) = '' THEN 1 ELSE 0 END)
            FROM {quote_any(table)}"""
    ).fetchone()
    n, nulls = row[0], row[1] or 0
    return round(100.0 * nulls / n, 4) if n else 0.0


def collect_metrics(source: Source, tables: list[str]) -> list[tuple]:
    """Returns rows of (table, column, metric, value). Table-level metrics use column ''."""
    out: list[tuple] = []
    for t in tables:
        cols = [c for c, _ in source.columns(t)]
        exprs = ", ".join(
            f"SUM(CASE WHEN {quote_any(c)} IS NULL OR TRIM(CAST({quote_any(c)} AS TEXT)) = '' THEN 1 ELSE 0 END)"
            for c in cols
        )
        row = source.con.execute(f"SELECT COUNT(*), {exprs} FROM {quote_any(t)}").fetchone()
        n = row[0]
        out.append((t, "", "row_count", float(n)))
        for c, nulls in zip(cols, row[1:]):
            out.append((t, c, "null_rate", round(100.0 * (nulls or 0) / n, 4) if n else 0.0))
    return out
