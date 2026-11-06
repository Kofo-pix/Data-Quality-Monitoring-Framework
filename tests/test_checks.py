import unittest
from datetime import datetime

from helpers import ctx, make_source

from dqmon import checks as C
from dqmon.suite import Suite

DDL = "CREATE TABLE t (id INTEGER, email TEXT, status TEXT, amount REAL, d TEXT);"
ROWS = {"t": [
    (1, "a@x.com", "ok", 10.0, "2025-01-10"),
    (2, "b@x.com", "ok", 20.0, "2025-01-09"),
    (2, None, "bad", -5.0, "2025-01-08"),       # duplicate id, null email, bad status, negative
    (3, "", "ok", 30.0, "2025-01-10"),          # blank email
    (4, "not-an-email", "ok", 99999.0, "2025-01-10"),
]}


def run(check, source=None, **kw):
    return check.run(ctx(source or make_source(DDL, ROWS), **kw))


class RowChecks(unittest.TestCase):
    def test_not_null_counts_null_and_blank(self):
        r = run(C.NotNull("t", "email"))
        self.assertEqual((r.status, r.failed_count, r.total_count), ("failed", 2, 5))

    def test_not_null_blank_can_be_allowed(self):
        r = run(C.NotNull("t", "email", treat_blank_as_null=False))
        self.assertEqual(r.failed_count, 1)

    def test_unique_flags_all_rows_in_duplicate_group(self):
        r = run(C.Unique("t", "id"))
        self.assertEqual((r.status, r.failed_count), ("failed", 2))
        self.assertEqual([row["id"] for row in r.sample], [2, 2])

    def test_unique_composite_key(self):
        r = run(C.Unique("t", columns=["id", "status"]))
        self.assertEqual(r.status, "passed")

    def test_allowed_values(self):
        r = run(C.AllowedValues("t", "status", ["ok"]))
        self.assertEqual(r.failed_count, 1)

    def test_allowed_values_case_insensitive(self):
        src = make_source("CREATE TABLE t (s TEXT);", {"t": [("OK",), ("ok",), ("no",)]})
        self.assertEqual(run(C.AllowedValues("t", "s", ["ok"], case_sensitive=False), src).failed_count, 1)

    def test_range_min_and_max(self):
        r = run(C.Range("t", "amount", min_value=0, max_value=1000))
        self.assertEqual(r.failed_count, 2)  # -5 and 99999

    def test_range_requires_a_bound(self):
        with self.assertRaises(ValueError):
            C.Range("t", "amount")

    def test_regex_ignores_nulls_and_blanks(self):
        r = run(C.Regex("t", "email", r"^[^@\s]+@[^@\s]+\.[a-z]+$"))
        self.assertEqual(r.failed_count, 1)  # only 'not-an-email'

    def test_referential_integrity(self):
        src = make_source(
            "CREATE TABLE p (id INTEGER); CREATE TABLE c (pid INTEGER);",
            {"p": [(1,), (2,)], "c": [(1,), (2,), (3,), (None,)]},
        )
        r = run(C.ReferentialIntegrity("c", "pid", "p", "id"), src)
        self.assertEqual((r.status, r.failed_count), ("failed", 1))

    def test_custom_sql(self):
        r = run(C.CustomSQL("neg", "SELECT * FROM t WHERE amount < 0"))
        self.assertEqual((r.status, r.failed_count), ("failed", 1))

    def test_custom_sql_rejects_non_select(self):
        with self.assertRaises(ValueError):
            C.CustomSQL("x", "DELETE FROM t")

    def test_tolerance_allows_small_failure_rate(self):
        self.assertEqual(run(C.NotNull("t", "email", max_fail_pct=50)).status, "passed")
        self.assertEqual(run(C.NotNull("t", "email", max_fail_pct=10)).status, "failed")

    def test_sample_is_capped(self):
        src = make_source("CREATE TABLE t (x TEXT);", {"t": [(None,)] * 50})
        self.assertEqual(len(run(C.NotNull("t", "x"), src).sample), C.SAMPLE_SIZE)

    def test_empty_table_passes_row_checks(self):
        src = make_source("CREATE TABLE t (x TEXT);")
        self.assertEqual(run(C.NotNull("t", "x"), src).status, "passed")


class TableChecks(unittest.TestCase):
    def test_row_count(self):
        self.assertEqual(run(C.RowCount("t", min_rows=5)).status, "passed")
        self.assertEqual(run(C.RowCount("t", min_rows=6)).status, "failed")
        self.assertEqual(run(C.RowCount("t", max_rows=4)).status, "failed")

    def test_freshness(self):
        ok = run(C.Freshness("t", "d", max_age_days=1), as_of=datetime(2025, 1, 10, 18))
        stale = run(C.Freshness("t", "d", max_age_days=1), as_of=datetime(2025, 1, 15))
        self.assertEqual((ok.status, stale.status), ("passed", "failed"))

    def test_freshness_empty_table_fails(self):
        src = make_source("CREATE TABLE t (d TEXT);")
        self.assertEqual(run(C.Freshness("t", "d", 1), src).status, "failed")

    def test_schema_contract(self):
        good = {"id": "INT", "email": "VARCHAR(50)", "amount": "FLOAT"}
        self.assertEqual(run(C.Schema("t", good)).status, "passed")  # type aliases normalised
        bad = run(C.Schema("t", {"id": "TEXT", "ghost": "INTEGER"}))
        self.assertEqual(bad.failed_count, 2)
        self.assertIn("missing column 'ghost'", bad.message)

    def test_schema_disallow_extra(self):
        r = run(C.Schema("t", {"id": "INTEGER"}, allow_extra_columns=False))
        self.assertGreater(r.failed_count, 0)

    def test_missing_table_is_error_not_crash(self):
        r = run(C.RowCount("nope", min_rows=1))
        self.assertEqual(r.status, "error")
        self.assertIn("no such table", r.message)

    def test_history_checks_skip_without_history(self):
        self.assertEqual(run(C.SchemaDrift("t")).status, "skipped")
        self.assertEqual(run(C.RowCountAnomaly("t")).status, "skipped")


class Safety(unittest.TestCase):
    def test_identifier_injection_rejected(self):
        for bad in ('t"; DROP TABLE t;--', "a b", "1abc", ""):
            with self.assertRaises(ValueError):
                C.NotNull(bad, "x")
        with self.assertRaises(ValueError):
            C.NotNull("t", 'x" OR 1=1 --')

    def test_source_is_read_only(self):
        import sqlite3
        import tempfile
        from pathlib import Path

        from dqmon.connectors import Source
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.db"
            con = sqlite3.connect(p)
            con.execute("CREATE TABLE t (a)")
            con.commit()
            con.close()
            src = Source.from_sqlite(p)
            with self.assertRaises(sqlite3.OperationalError):
                src.con.execute("INSERT INTO t VALUES (1)")

    def test_bad_severity_rejected(self):
        with self.assertRaises(ValueError):
            C.NotNull("t", "x", severity="fatal")


class SuiteLoading(unittest.TestCase):
    def test_unknown_type_has_helpful_error(self):
        with self.assertRaises(ValueError) as cm:
            Suite.from_dict({"name": "s", "checks": [{"type": "nope"}]})
        self.assertIn("unknown type", str(cm.exception))

    def test_bad_param_is_reported_with_position(self):
        with self.assertRaises(ValueError) as cm:
            Suite.from_dict({"name": "s", "checks": [{"type": "not_null", "table": "t"}]})
        self.assertIn("check #1", str(cm.exception))

    def test_duplicate_names_rejected(self):
        spec = {"type": "not_null", "table": "t", "column": "a"}
        with self.assertRaises(ValueError):
            Suite.from_dict({"name": "s", "checks": [spec, spec]})


if __name__ == "__main__":
    unittest.main()
