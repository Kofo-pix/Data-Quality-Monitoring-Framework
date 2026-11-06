"""The check library.

Every check:
  * is declared in JSON (`{"type": "not_null", "table": ..., "column": ...}`),
  * reads data only (SQL is generated with validated identifiers and bound parameters),
  * returns a CheckResult with failing-row samples so problems are debuggable, and
  * can be `severity: "error"` (blocks the pipeline) or `"warn"` (reported only).
"""
from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass
from datetime import datetime

from .connectors import Source
from .connectors import quote_ident as q
from .metrics import null_rate
from .models import CheckResult

SAMPLE_SIZE = 5


class Skip(Exception):
    """Raised when a check can't be evaluated yet (e.g. no history to compare against)."""


@dataclass
class Context:
    source: Source
    history: object | None
    suite_name: str
    as_of: datetime
    run_id: int = 0


def normalize_type(t: str | None) -> str:
    t = re.sub(r"\(.*\)", "", (t or "").upper()).strip()
    if not t:
        return "ANY"
    if "INT" in t:
        return "INTEGER"
    if any(k in t for k in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if any(k in t for k in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    if any(k in t for k in ("DATE", "TIME")):
        return "DATETIME"
    if "BOOL" in t:
        return "BOOLEAN"
    if "DEC" in t or "NUM" in t:
        return "NUMERIC"
    return t


class Check:
    type = "check"

    def __init__(self, table: str, column: str | None = None, severity: str = "error",
                 max_fail_pct: float = 0.0, name: str | None = None):
        if severity not in ("error", "warn"):
            raise ValueError(f"severity must be 'error' or 'warn', got {severity!r}")
        q(table)
        if column:
            q(column)
        self.table, self.column, self.severity = table, column, severity
        self.max_fail_pct = float(max_fail_pct)
        self.name = name or self._default_name()

    def _default_name(self) -> str:
        return f"{self.type}:{self.table}" + (f".{self.column}" if self.column else "")

    # -- to override -------------------------------------------------------------------
    def evaluate(self, ctx: Context) -> tuple[int | None, int | None, str, list]:
        raise NotImplementedError

    def passes(self, failed: int | None, total: int | None) -> bool:
        return not failed

    # -- execution ---------------------------------------------------------------------
    def run(self, ctx: Context) -> CheckResult:
        t0 = time.perf_counter()
        failed = total = None
        sample: list = []
        try:
            failed, total, msg, sample = self.evaluate(ctx)
            status = "passed" if self.passes(failed, total) else "failed"
        except Skip as s:
            status, msg = "skipped", str(s)
        except Exception as exc:  # a broken check must never crash the whole run
            status, msg = "error", f"{type(exc).__name__}: {exc}"
        return CheckResult(
            name=self.name, check_type=self.type, table=self.table, column=self.column,
            severity=self.severity, status=status, message=msg, failed_count=failed,
            total_count=total, sample=sample, duration_ms=(time.perf_counter() - t0) * 1000,
        )


class RowCheck(Check):
    """Checks that count violating rows. `max_fail_pct` allows a tolerated failure rate."""

    order_by: str | None = None

    def condition(self) -> tuple[str, tuple]:
        raise NotImplementedError

    def violation_text(self) -> str:
        raise NotImplementedError

    def evaluate(self, ctx):
        cond, params = self.condition()
        where, t = f"({cond})", q(self.table)
        total = ctx.source.scalar(f"SELECT COUNT(*) FROM {t}")
        failed = ctx.source.scalar(f"SELECT COUNT(*) FROM {t} WHERE {where}", params)
        sample = []
        if failed:
            order = f" ORDER BY {self.order_by}" if self.order_by else ""
            sample = ctx.source.query(
                f"SELECT * FROM {t} WHERE {where}{order} LIMIT {SAMPLE_SIZE}", params
            )
        pct = 100.0 * failed / total if total else 0.0
        msg = f"{failed} of {total} rows ({pct:.2f}%) {self.violation_text()}"
        if self.max_fail_pct:
            msg += f"; tolerance {self.max_fail_pct}%"
        return failed, total, msg, sample

    def passes(self, failed, total):
        if not failed:
            return True
        return bool(total) and 100.0 * failed / total <= self.max_fail_pct


# ------------------------------------------------------------------ completeness ---------
class NotNull(RowCheck):
    type = "not_null"

    def __init__(self, table, column, treat_blank_as_null: bool = True, **kw):
        super().__init__(table, column, **kw)
        self.treat_blank_as_null = treat_blank_as_null

    def condition(self):
        c = q(self.column)
        if self.treat_blank_as_null:
            return f"{c} IS NULL OR TRIM(CAST({c} AS TEXT)) = ''", ()
        return f"{c} IS NULL", ()

    def violation_text(self):
        return f"have a missing {self.column}"


# ------------------------------------------------------------------ uniqueness -----------
class Unique(RowCheck):
    type = "unique"

    def __init__(self, table, column=None, columns=None, **kw):
        cols = list(columns) if columns else ([column] if column else [])
        if not cols:
            raise ValueError("unique check needs 'column' or 'columns'")
        for c in cols:
            q(c)
        self.columns = cols
        super().__init__(table, None, **kw)
        self.column = ", ".join(cols)
        if "name" not in kw:
            self.name = self._default_name()

    def condition(self):
        cols = ", ".join(q(c) for c in self.columns)
        not_null = " AND ".join(f"{q(c)} IS NOT NULL" for c in self.columns)
        return (f"({cols}) IN (SELECT {cols} FROM {q(self.table)} WHERE {not_null} "
                f"GROUP BY {cols} HAVING COUNT(*) > 1)"), ()

    @property
    def order_by(self):  # keep duplicate rows adjacent in the sample
        return ", ".join(q(c) for c in self.columns)

    def violation_text(self):
        return f"share a duplicate {self.column}"


# ------------------------------------------------------------------ validity -------------
class AllowedValues(RowCheck):
    type = "allowed_values"

    def __init__(self, table, column, values, case_sensitive: bool = True, **kw):
        super().__init__(table, column, **kw)
        if not values:
            raise ValueError("allowed_values needs a non-empty 'values' list")
        self.values, self.case_sensitive = list(values), case_sensitive

    def condition(self):
        c, marks = q(self.column), ",".join("?" * len(self.values))
        if self.case_sensitive:
            return f"{c} IS NOT NULL AND {c} NOT IN ({marks})", tuple(self.values)
        return (f"{c} IS NOT NULL AND UPPER(CAST({c} AS TEXT)) NOT IN ({marks})",
                tuple(str(v).upper() for v in self.values))

    def violation_text(self):
        return f"have a {self.column} outside the allowed set"


class Range(RowCheck):
    type = "range"

    def __init__(self, table, column, min_value=None, max_value=None, **kw):
        super().__init__(table, column, **kw)
        if min_value is None and max_value is None:
            raise ValueError("range needs 'min_value' and/or 'max_value'")
        self.min_value, self.max_value = min_value, max_value

    def condition(self):
        c = f"CAST({q(self.column)} AS REAL)"
        parts, params = [], []
        if self.min_value is not None:
            parts.append(f"{c} < ?")
            params.append(self.min_value)
        if self.max_value is not None:
            parts.append(f"{c} > ?")
            params.append(self.max_value)
        return f"{q(self.column)} IS NOT NULL AND ({' OR '.join(parts)})", tuple(params)

    def violation_text(self):
        return f"have {self.column} outside [{self.min_value}, {self.max_value}]"


class Regex(RowCheck):
    type = "regex"

    def __init__(self, table, column, pattern, **kw):
        super().__init__(table, column, **kw)
        re.compile(pattern)  # fail fast on a bad pattern
        self.pattern = pattern

    def condition(self):
        c = q(self.column)
        return (f"{c} IS NOT NULL AND TRIM(CAST({c} AS TEXT)) <> '' AND NOT ({c} REGEXP ?)",
                (self.pattern,))

    def violation_text(self):
        return f"have a malformed {self.column}"


class ReferentialIntegrity(RowCheck):
    type = "referential_integrity"

    def __init__(self, table, column, ref_table, ref_column, **kw):
        super().__init__(table, column, **kw)
        q(ref_table), q(ref_column)
        self.ref_table, self.ref_column = ref_table, ref_column

    def condition(self):
        c = q(self.column)
        return (f"{c} IS NOT NULL AND {c} NOT IN "
                f"(SELECT {q(self.ref_column)} FROM {q(self.ref_table)} "
                f"WHERE {q(self.ref_column)} IS NOT NULL)"), ()

    def violation_text(self):
        return f"are orphans (no matching {self.ref_table}.{self.ref_column})"


class CustomSQL(Check):
    """Any SELECT that returns the *violating* rows. Zero rows = pass."""

    type = "custom_sql"

    def __init__(self, name, sql, table="custom", **kw):
        super().__init__(table, None, name=name, **kw)
        if not re.match(r"^\s*(select|with)\b", sql, re.I):
            raise ValueError("custom_sql must be a SELECT/WITH query")
        self.sql = sql.strip().rstrip(";")

    def evaluate(self, ctx):
        failed = ctx.source.scalar(f"SELECT COUNT(*) FROM ({self.sql})")
        sample = ctx.source.query(f"SELECT * FROM ({self.sql}) LIMIT {SAMPLE_SIZE}") if failed else []
        return failed, None, f"{failed} violating rows returned by custom query", sample


# ------------------------------------------------------------------ table-level ----------
class RowCount(Check):
    type = "row_count"

    def __init__(self, table, min_rows=None, max_rows=None, **kw):
        super().__init__(table, None, **kw)
        self.min_rows, self.max_rows = min_rows, max_rows

    def evaluate(self, ctx):
        n = ctx.source.scalar(f"SELECT COUNT(*) FROM {q(self.table)}")
        bad = (self.min_rows is not None and n < self.min_rows) or \
              (self.max_rows is not None and n > self.max_rows)
        lo = "-inf" if self.min_rows is None else self.min_rows
        hi = "+inf" if self.max_rows is None else self.max_rows
        return int(bad), None, f"{n} rows (expected {lo} to {hi})", []


class Freshness(Check):
    type = "freshness"

    def __init__(self, table, column, max_age_days, **kw):
        super().__init__(table, column, **kw)
        self.max_age_days = float(max_age_days)

    def evaluate(self, ctx):
        latest = ctx.source.scalar(f"SELECT MAX({q(self.column)}) FROM {q(self.table)}")
        if latest is None:
            return 1, None, f"no data in {self.table}.{self.column}", []
        ts = datetime.fromisoformat(str(latest).strip().replace("Z", ""))
        ts = ts.replace(tzinfo=None)
        age = (ctx.as_of - ts).total_seconds() / 86400.0
        msg = f"latest {self.column} = {latest} ({age:.1f} days old, limit {self.max_age_days:g})"
        return int(age > self.max_age_days), None, msg, []


# ------------------------------------------------------------------ schema ---------------
class Schema(Check):
    """Contract check: required columns and their types."""

    type = "schema"

    def __init__(self, table, expected: dict, allow_extra_columns: bool = True, **kw):
        super().__init__(table, None, **kw)
        self.expected, self.allow_extra = dict(expected), allow_extra_columns

    def evaluate(self, ctx):
        actual = {n: normalize_type(t) for n, t in ctx.source.columns(self.table)}
        issues = []
        for col, typ in self.expected.items():
            if col not in actual:
                issues.append(f"missing column '{col}'")
            elif actual[col] != normalize_type(typ):
                issues.append(f"'{col}': expected {normalize_type(typ)}, found {actual[col]}")
        if not self.allow_extra:
            issues += [f"unexpected column '{c}'" for c in actual if c not in self.expected]
        msg = "; ".join(issues) if issues else f"schema matches contract ({len(self.expected)} columns)"
        return len(issues), None, msg, [{"issue": i} for i in issues[:SAMPLE_SIZE]]


class SchemaDrift(Check):
    """Monitoring check: compare the current schema against the previous run's snapshot."""

    type = "schema_drift"

    def __init__(self, table, **kw):
        super().__init__(table, None, **kw)

    def evaluate(self, ctx):
        if ctx.history is None:
            raise Skip("history disabled")
        prev = ctx.history.last_schema(ctx.suite_name, self.table)
        if prev is None:
            raise Skip("no previous snapshot; baseline recorded")
        cur = dict(ctx.source.columns(self.table))
        issues = [f"column added: '{c}' ({cur[c]})" for c in cur if c not in prev]
        issues += [f"column dropped: '{c}'" for c in prev if c not in cur]
        issues += [f"type changed: '{c}' {prev[c] or 'ANY'} -> {cur[c] or 'ANY'}"
                   for c in cur if c in prev and normalize_type(cur[c]) != normalize_type(prev[c])]
        msg = "; ".join(issues) if issues else "no schema changes since previous run"
        return len(issues), None, msg, [{"change": i} for i in issues[:SAMPLE_SIZE]]


# ------------------------------------------------------------------ anomaly detection ----
class RowCountAnomaly(Check):
    type = "row_count_anomaly"

    def __init__(self, table, max_change_pct: float = 30.0, lookback: int = 7, **kw):
        super().__init__(table, None, **kw)
        self.max_change_pct, self.lookback = float(max_change_pct), int(lookback)

    def evaluate(self, ctx):
        if ctx.history is None:
            raise Skip("history disabled")
        series = ctx.history.metric_series(ctx.suite_name, self.table, "", "row_count", self.lookback)
        if not series:
            raise Skip("no history yet; baseline recorded")
        base = statistics.median(series)
        cur = ctx.source.scalar(f"SELECT COUNT(*) FROM {q(self.table)}")
        change = ((cur - base) / base * 100.0) if base else (0.0 if cur == 0 else 100.0)
        msg = (f"{cur} rows vs median {base:g} of last {len(series)} runs "
               f"({change:+.1f}%, limit +/-{self.max_change_pct:g}%)")
        return int(abs(change) > self.max_change_pct), None, msg, []


class NullRateDrift(Check):
    type = "null_rate_drift"

    def __init__(self, table, column, max_increase_pp: float = 5.0, lookback: int = 7, **kw):
        super().__init__(table, column, **kw)
        self.max_increase_pp, self.lookback = float(max_increase_pp), int(lookback)

    def evaluate(self, ctx):
        if ctx.history is None:
            raise Skip("history disabled")
        series = ctx.history.metric_series(ctx.suite_name, self.table, self.column, "null_rate", self.lookback)
        if not series:
            raise Skip("no history yet; baseline recorded")
        base, cur = statistics.median(series), null_rate(ctx.source, self.table, self.column)
        delta = cur - base
        msg = (f"null rate {cur:.1f}% vs baseline {base:.1f}% "
               f"({delta:+.1f} pp, limit +{self.max_increase_pp:g} pp)")
        return int(delta > self.max_increase_pp), None, msg, []


CHECK_TYPES = {c.type: c for c in (
    NotNull, Unique, AllowedValues, Range, Regex, ReferentialIntegrity, CustomSQL,
    RowCount, Freshness, Schema, SchemaDrift, RowCountAnomaly, NullRateDrift,
)}
