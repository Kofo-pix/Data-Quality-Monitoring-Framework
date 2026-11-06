"""Profile tables and bootstrap a starter check suite from what the data looks like."""
from __future__ import annotations

import math
import re

from .checks import normalize_type
from .connectors import Source, quote_any

_DATE_NAME = re.compile(r"(date|_at$|time)", re.I)
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


def profile_table(source: Source, table: str) -> dict:
    t = quote_any(table)
    n = source.scalar(f"SELECT COUNT(*) FROM {t}")
    cols = {}
    for name, typ in source.columns(table):
        c = quote_any(name)
        nulls, distinct, lo, hi = source.con.execute(
            f"""SELECT SUM(CASE WHEN {c} IS NULL OR TRIM(CAST({c} AS TEXT)) = '' THEN 1 ELSE 0 END),
                       COUNT(DISTINCT {c}), MIN({c}), MAX({c}) FROM {t}"""
        ).fetchone()
        nulls = nulls or 0
        top = []
        if 0 < distinct <= 10:
            top = [r[0] for r in source.con.execute(
                f"SELECT {c} FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY COUNT(*) DESC")]
        cols[name] = {"type": typ, "norm_type": normalize_type(typ), "nulls": nulls,
                      "null_pct": round(100.0 * nulls / n, 2) if n else 0.0,
                      "distinct": distinct, "min": lo, "max": hi, "top_values": top}
    return {"table": table, "rows": n, "columns": cols}


def suggest_suite(source: Source, tables: list[str], name: str) -> dict:
    checks: list[dict] = []
    for table in tables:
        p = profile_table(source, table)
        n, cols = p["rows"], p["columns"]
        checks.append({"type": "row_count", "table": table, "min_rows": max(1, int(n * 0.5))})
        checks.append({"type": "schema", "table": table,
                       "expected": {c: i["norm_type"] for c, i in cols.items()}})
        for c, i in cols.items():
            non_null = n - i["nulls"]
            if i["nulls"] == 0 and n:
                checks.append({"type": "not_null", "table": table, "column": c})
            elif i["null_pct"] < 50:
                checks.append({"type": "not_null", "table": table, "column": c, "severity": "warn",
                               "max_fail_pct": round(i["null_pct"] * 1.5 + 1, 1)})
            is_key = c == "id" or c.endswith("_id")
            if is_key and n > 1 and i["distinct"] == non_null and non_null > 0:
                checks.append({"type": "unique", "table": table, "column": c})
            if i["norm_type"] == "TEXT" and n >= 20 and 0 < i["distinct"] <= 10:
                checks.append({"type": "allowed_values", "table": table, "column": c,
                               "values": i["top_values"], "severity": "warn"})
            if i["norm_type"] in ("INTEGER", "REAL") and not is_key and i["min"] is not None:
                span = abs(i["max"] - i["min"]) or 1
                lo = 0 if i["min"] >= 0 else math.floor(i["min"] - 0.2 * span)
                hi = math.ceil(i["max"] + 0.2 * span)
                checks.append({"type": "range", "table": table, "column": c,
                               "min_value": lo, "max_value": hi, "severity": "warn"})
            if (_DATE_NAME.search(c) and i["norm_type"] in ("TEXT", "DATETIME")
                    and isinstance(i["max"], str) and _ISO.match(i["max"])):
                checks.append({"type": "freshness", "table": table, "column": c,
                               "max_age_days": 7, "severity": "warn"})
        checks.append({"type": "schema_drift", "table": table})
        checks.append({"type": "row_count_anomaly", "table": table, "max_change_pct": 40})
    return {"name": name, "source": {"type": "sqlite", "path": "<set me>"}, "checks": checks}
