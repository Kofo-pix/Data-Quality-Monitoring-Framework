"""CLI:  python -m dqmon run|profile|history|demo"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from . import alerts, demo, profiler, reporters
from .connectors import Source
from .history import History
from .runner import run_suite
from .suite import load_suite

ROOT = Path(__file__).resolve().parents[2]


def _parse_as_of(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _exit_code(summary, fail_on: str) -> int:
    if fail_on == "never":
        return 0
    if fail_on == "warn":
        return 0 if summary.blocking == 0 and summary.warnings == 0 else 1
    return 0 if summary.gate_passed else 1


def _execute(suite, source, history, args, as_of=None, html_path=None, json_path=None) -> object:
    summary = run_suite(suite, source, history, as_of)
    print(reporters.render_console(summary, color=sys.stdout.isatty()))
    if json_path:
        reporters.write_json(summary, json_path)
    if html_path:
        runs = history.runs(suite.name, 20) if history else None
        reporters.write_html(summary, html_path, runs)
        print(f"  HTML report: {html_path}")
    return summary


def cmd_run(args) -> int:
    suite = load_suite(args.suite)
    spec = dict(suite.source)
    if args.db:
        spec = {"type": "sqlite", "path": args.db}
    elif args.csv_dir:
        spec = {"type": "csv", "dir": args.csv_dir}
    source = Source.from_spec(spec)
    history = None if args.no_history else History(args.history)
    try:
        summary = _execute(suite, source, history, args, _parse_as_of(args.as_of), args.html, args.json)
    finally:
        source.close()
        if history:
            history.close()
    webhook = args.slack_webhook or os.getenv("DQMON_SLACK_WEBHOOK")
    if not summary.gate_passed or summary.warnings:
        payload = alerts.build_slack_payload(summary)
        if webhook:
            alerts.send_slack(payload, webhook)
            print("  Slack alert sent.")
        elif args.show_alert:
            print("\n[dry-run alert]\n" + payload["text"])
    return _exit_code(summary, args.fail_on)


def cmd_profile(args) -> int:
    source = Source.from_spec({"type": "sqlite", "path": args.db} if args.db else {"type": "csv", "dir": args.csv_dir})
    tables = args.tables.split(",") if args.tables else source.tables()
    suggestion = profiler.suggest_suite(source, tables, args.name)
    suggestion["source"] = {"type": "sqlite", "path": args.db} if args.db else {"type": "csv", "dir": args.csv_dir}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(suggestion, indent=2), encoding="utf-8")
    print(f"Wrote starter suite with {len(suggestion['checks'])} checks to {args.out}")
    print("Review thresholds before relying on it: it describes today's data, not your requirements.")
    return 0


def cmd_history(args) -> int:
    history = History(args.history)
    runs = history.runs(args.suite, args.limit)
    if not runs:
        print("No runs recorded yet.")
        return 0
    print(f"{'run':>4}  {'suite':<20} {'started':<20} {'score':>6}  {'pass':>4} {'fail':>4} {'err':>3} {'skip':>4}  gate")
    for r in runs:
        print(f"{r['run_id']:>4}  {r['suite']:<20} {r['started_at']:<20} {r['score']:>5}%  "
              f"{r['passed']:>4} {r['failed']:>4} {r['errored']:>3} {r['skipped']:>4}  "
              f"{'PASS' if r['gate_passed'] else 'FAIL'}")
    return 0


def cmd_demo(args) -> int:
    work = Path(args.workdir)
    work.mkdir(parents=True, exist_ok=True)
    hist_path, db_path = work / "history.db", work / "source.db"
    hist_path.unlink(missing_ok=True)
    suite = load_suite(ROOT / "suites" / "ecommerce.json")
    for day, label in ((1, "healthy baseline"), (2, "bad deploy"), (3, "upstream outage")):
        print(f"\n{'=' * 100}\nDAY {day}: {label}\n{'=' * 100}")
        demo.build_demo_db(db_path, day)
        source, history = Source.from_sqlite(db_path), History(hist_path)
        try:
            _execute(suite, source, history, args, demo.as_of_for(day),
                     work / f"report_day{day}.html", work / f"report_day{day}.json")
        finally:
            source.close()
            history.close()
    print(f"\nDemo complete. Open {work / 'report_day3.html'} in a browser.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dqmon", description="Data quality & monitoring framework")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a check suite")
    r.add_argument("suite")
    r.add_argument("--db", help="SQLite file (overrides suite source)")
    r.add_argument("--csv-dir", help="directory of CSV files (overrides suite source)")
    r.add_argument("--history", default="dq_history.db")
    r.add_argument("--no-history", action="store_true")
    r.add_argument("--html", help="write a self-contained HTML report")
    r.add_argument("--json", help="write a machine-readable JSON report")
    r.add_argument("--as-of", help="evaluate freshness as of this ISO date/time")
    r.add_argument("--fail-on", choices=["error", "warn", "never"], default="error")
    r.add_argument("--slack-webhook")
    r.add_argument("--show-alert", action="store_true", help="print the alert text when not sending")
    r.set_defaults(fn=cmd_run)

    pr = sub.add_parser("profile", help="profile data and generate a starter suite")
    pr.add_argument("--db")
    pr.add_argument("--csv-dir")
    pr.add_argument("--tables", help="comma-separated (default: all)")
    pr.add_argument("--name", default="starter_suite")
    pr.add_argument("--out", default="suites/starter.json")
    pr.set_defaults(fn=cmd_profile)

    h = sub.add_parser("history", help="show past runs")
    h.add_argument("--history", default="dq_history.db")
    h.add_argument("--suite")
    h.add_argument("--limit", type=int, default=20)
    h.set_defaults(fn=cmd_history)

    d = sub.add_parser("demo", help="run the 3-day degradation demo")
    d.add_argument("--workdir", default="demo_output")
    d.set_defaults(fn=cmd_demo)

    args = p.parse_args(argv)
    if args.command == "profile" and not (args.db or args.csv_dir):
        p.error("profile needs --db or --csv-dir")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
