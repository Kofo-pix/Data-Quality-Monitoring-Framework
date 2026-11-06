from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CheckResult:
    name: str
    check_type: str
    table: str
    column: str | None
    severity: str  # "error" blocks the pipeline, "warn" is informational
    status: str  # passed | failed | error | skipped
    message: str = ""
    failed_count: int | None = None
    total_count: int | None = None
    sample: list = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def failed_pct(self) -> float | None:
        if self.failed_count is None or not self.total_count:
            return None
        return round(100.0 * self.failed_count / self.total_count, 2)

    @property
    def is_blocking(self) -> bool:
        return self.severity == "error" and self.status in ("failed", "error")

    @property
    def label(self) -> str:
        if self.status == "passed":
            return "PASS"
        if self.status == "skipped":
            return "SKIP"
        if self.severity == "warn":
            return "WARN"
        return "ERROR" if self.status == "error" else "FAIL"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "type": self.check_type, "table": self.table, "column": self.column,
            "severity": self.severity, "status": self.status, "label": self.label,
            "message": self.message, "failed_count": self.failed_count,
            "total_count": self.total_count, "failed_pct": self.failed_pct,
            "sample": self.sample, "duration_ms": round(self.duration_ms, 2),
        }


@dataclass
class RunSummary:
    suite: str
    run_id: int
    started_at: datetime
    as_of: datetime
    results: list[CheckResult]

    @property
    def total(self) -> int:
        return len(self.results)

    def _count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def passed(self) -> int:
        return self._count("passed")

    @property
    def failed(self) -> int:
        return self._count("failed")

    @property
    def errored(self) -> int:
        return self._count("error")

    @property
    def skipped(self) -> int:
        return self._count("skipped")

    @property
    def blocking(self) -> int:
        return sum(1 for r in self.results if r.is_blocking)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.results if r.severity == "warn" and r.status in ("failed", "error"))

    @property
    def gate_passed(self) -> bool:
        return self.blocking == 0

    @property
    def score(self) -> float:
        """Quality score: % of evaluated (non-skipped) checks that passed."""
        evaluated = self.total - self.skipped
        return round(100.0 * self.passed / evaluated, 1) if evaluated else 100.0

    def to_dict(self) -> dict:
        return {
            "suite": self.suite, "run_id": self.run_id,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "as_of": self.as_of.isoformat(timespec="seconds"),
            "gate_passed": self.gate_passed, "score": self.score,
            "totals": {"checks": self.total, "passed": self.passed, "failed": self.failed,
                       "errored": self.errored, "skipped": self.skipped,
                       "blocking": self.blocking, "warnings": self.warnings},
            "results": [r.to_dict() for r in self.results],
        }
