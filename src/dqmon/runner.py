from __future__ import annotations

from datetime import datetime

from .checks import Context
from .connectors import Source
from .history import History
from .metrics import collect_metrics
from .models import RunSummary
from .suite import Suite


def run_suite(suite: Suite, source: Source, history: History | None = None,
              as_of: datetime | None = None) -> RunSummary:
    started = datetime.now()
    as_of = as_of or started
    run_id = history.start_run(suite.name, started.isoformat(timespec="seconds")) if history else 0
    ctx = Context(source=source, history=history, suite_name=suite.name, as_of=as_of, run_id=run_id)

    results = [check.run(ctx) for check in suite.checks]
    summary = RunSummary(suite.name, run_id, started, as_of, results)

    if history:
        # Record observations AFTER the checks ran, so a run is never compared with itself.
        tables = []
        for t in suite.tables:
            try:
                source.columns(t)
                tables.append(t)
            except LookupError:
                pass  # the failing check already reported the missing table
        history.save_results(run_id, results)
        history.save_metrics(run_id, suite.name, collect_metrics(source, tables))
        for t in tables:
            history.save_schema(run_id, suite.name, t, source.columns(t))
        history.finish_run(run_id, summary, datetime.now().isoformat(timespec="seconds"))
    return summary
