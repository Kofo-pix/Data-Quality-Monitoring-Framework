import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from helpers import make_source

from dqmon import alerts, demo, profiler, reporters
from dqmon.cli import main
from dqmon.connectors import Source
from dqmon.history import History
from dqmon.runner import run_suite
from dqmon.suite import Suite, load_suite

ROOT = Path(__file__).resolve().parents[1]


def suite_for(checks):
    return Suite.from_dict({"name": "s", "checks": checks})


class Monitoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hist = History(Path(self.tmp.name) / "h.db")

    def tearDown(self):
        self.hist.close()
        self.tmp.cleanup()

    def _run(self, rows, ddl="CREATE TABLE t (id INTEGER, v TEXT);"):
        src = make_source(ddl, {"t": rows})
        suite = suite_for([
            {"type": "row_count_anomaly", "table": "t", "max_change_pct": 30},
            {"type": "null_rate_drift", "table": "t", "column": "v", "max_increase_pp": 5},
            {"type": "schema_drift", "table": "t"},
        ])
        return {r.check_type: r for r in run_suite(suite, src, self.hist, datetime(2025, 1, 1)).results}

    def test_first_run_is_baseline_then_stable_runs_pass(self):
        first = self._run([(i, "x") for i in range(100)])
        self.assertTrue(all(r.status == "skipped" for r in first.values()))
        second = self._run([(i, "x") for i in range(105)])
        self.assertTrue(all(r.status == "passed" for r in second.values()), second)

    def test_volume_collapse_detected(self):
        self._run([(i, "x") for i in range(100)])
        r = self._run([(i, "x") for i in range(30)])
        self.assertEqual(r["row_count_anomaly"].status, "failed")

    def test_null_rate_drift_detected(self):
        self._run([(i, "x") for i in range(100)])
        r = self._run([(i, None if i % 4 == 0 else "x") for i in range(100)])
        self.assertEqual(r["null_rate_drift"].status, "failed")

    def test_schema_drift_detected(self):
        self._run([(1, "x")])
        r = self._run([(1, "x", 1.0)], ddl="CREATE TABLE t (id INTEGER, v TEXT, extra REAL);")
        self.assertEqual(r["schema_drift"].status, "failed")
        self.assertIn("column added: 'extra'", r["schema_drift"].message)

    def test_run_never_compares_with_itself(self):
        # a failing run must not poison its own baseline
        self._run([(i, "x") for i in range(100)])
        self._run([(i, "x") for i in range(10)])
        series = self.hist.metric_series("s", "t", "", "row_count", 10)
        self.assertEqual(series, [100.0, 10.0])

    def test_history_records_runs(self):
        self._run([(1, "x")])
        runs = self.hist.runs("s")
        self.assertEqual(len(runs), 1)
        self.assertIn("score", runs[0])


class DemoScenario(unittest.TestCase):
    """The end-to-end story: healthy -> bad deploy -> outage."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        work = Path(cls.tmp.name)
        suite = load_suite(ROOT / "suites" / "ecommerce.json")
        cls.hist = History(work / "h.db")
        cls.runs = {}
        for day in (1, 2, 3):
            demo.build_demo_db(work / "s.db", day)
            src = Source.from_sqlite(work / "s.db")
            cls.runs[day] = run_suite(suite, src, cls.hist, demo.as_of_for(day))
            src.close()

    @classmethod
    def tearDownClass(cls):
        cls.hist.close()
        cls.tmp.cleanup()

    def failed(self, day):
        return {r.name for r in self.runs[day].results if r.status == "failed"}

    def test_day1_is_clean(self):
        s = self.runs[1]
        self.assertTrue(s.gate_passed)
        self.assertEqual((s.failed, s.errored), (0, 0))

    def test_day2_detects_quality_regressions(self):
        self.assertFalse(self.runs[2].gate_passed)
        for name in ("not_null:customers.email", "unique:orders.order_id",
                     "allowed_values:orders.status", "referential_integrity:orders.customer_id",
                     "range:orders.amount", "schema_drift:orders"):
            self.assertIn(name, self.failed(2))

    def test_day3_detects_outage_and_type_change(self):
        for name in ("row_count_anomaly:orders", "freshness:orders.order_date",
                     "schema:orders", "schema_drift:orders"):
            self.assertIn(name, self.failed(3))

    def test_no_check_errored_in_demo(self):
        for s in self.runs.values():
            self.assertEqual(s.errored, 0)


class Reporting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        work = Path(self.tmp.name)
        demo.build_demo_db(work / "s.db", 2)
        src = Source.from_sqlite(work / "s.db")
        self.summary = run_suite(load_suite(ROOT / "suites" / "ecommerce.json"), src, None, demo.as_of_for(2))
        src.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_html_report(self):
        out = reporters.render_html(self.summary, [{"run_id": 1, "score": 90, "gate_passed": 1}])
        self.assertIn("GATE FAILED", out)
        self.assertIn("unique:orders.order_id", out)
        self.assertIn("<svg", out)

    def test_html_escapes_content(self):
        self.summary.results[0].message = "<script>alert(1)</script>"
        self.assertNotIn("<script>alert(1)</script>", reporters.render_html(self.summary))

    def test_json_report_roundtrip(self):
        p = Path(self.tmp.name) / "r.json"
        reporters.write_json(self.summary, p)
        data = json.loads(p.read_text())
        self.assertFalse(data["gate_passed"])
        self.assertEqual(data["totals"]["checks"], len(self.summary.results))

    def test_slack_payload(self):
        text = alerts.build_slack_payload(self.summary)["text"]
        self.assertIn("FAILED", text)
        self.assertIn("unique:orders.order_id", text)


class Profiler(unittest.TestCase):
    def setUp(self):
        rows = [(i, ["new", "paid", "shipped"][i % 3], float(i % 50 + 1), f"2025-01-{i % 9 + 1:02d}")
                for i in range(1, 101)]
        self.src = make_source("CREATE TABLE orders (order_id INTEGER, status TEXT, amount REAL, created_at TEXT);",
                               {"orders": rows})

    def test_suggested_suite_passes_on_its_own_data(self):
        spec = profiler.suggest_suite(self.src, ["orders"], "auto")
        suite = Suite.from_dict(spec)
        s = run_suite(suite, self.src, None, datetime(2025, 1, 10))
        self.assertTrue(s.gate_passed, [r.message for r in s.results if r.is_blocking])
        types = {c["type"] for c in spec["checks"]}
        self.assertTrue({"schema", "not_null", "unique", "allowed_values", "range", "freshness"} <= types)

    def test_suggested_suite_catches_corruption(self):
        suite = Suite.from_dict(profiler.suggest_suite(self.src, ["orders"], "auto"))
        self.src.con.execute("UPDATE orders SET order_id = 1 WHERE order_id = 2")
        self.src.con.execute("UPDATE orders SET status = 'refunded' WHERE order_id = 5")
        s = run_suite(suite, self.src, None, datetime(2025, 1, 10))
        self.assertFalse(s.gate_passed)


class CLI(unittest.TestCase):
    def test_run_exit_codes_and_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            demo.build_demo_db(d / "s.db", 1)
            args = ["run", str(ROOT / "suites/ecommerce.json"), "--db", str(d / "s.db"),
                    "--history", str(d / "h.db"), "--as-of", "2025-03-01T12:00:00",
                    "--html", str(d / "r.html"), "--json", str(d / "r.json")]
            self.assertEqual(main(args), 0)
            self.assertTrue((d / "r.html").exists() and (d / "r.json").exists())
            demo.build_demo_db(d / "s.db", 2)
            self.assertEqual(main(args[:-4] + ["--as-of", "2025-03-02T12:00:00"]), 1)
            self.assertEqual(main(args[:-4] + ["--as-of", "2025-03-02T12:00:00", "--fail-on", "never"]), 0)

    def test_csv_source(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "people.csv").write_text("id,age\n1,30\n2,\n3,41\n")
            src = Source.from_csv_dir(d)
            self.assertEqual(dict(src.columns("people")), {"id": "INTEGER", "age": "INTEGER"})
            s = run_suite(suite_for([{"type": "not_null", "table": "people", "column": "age"}]), src)
            self.assertEqual(s.results[0].failed_count, 1)


if __name__ == "__main__":
    unittest.main()
