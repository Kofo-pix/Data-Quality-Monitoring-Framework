"""Alerting. Builds a Slack-compatible payload; posts it only if a webhook URL is given."""
from __future__ import annotations

import json
import urllib.request

from .models import RunSummary


def build_slack_payload(summary: RunSummary, report_url: str | None = None) -> dict:
    icon = ":white_check_mark:" if summary.gate_passed else ":rotating_light:"
    head = (f"{icon} *{summary.suite}* data quality: "
            f"{'PASSED' if summary.gate_passed else 'FAILED'} "
            f"(score {summary.score}%, {summary.blocking} blocking, {summary.warnings} warnings)")
    bad = [r for r in summary.results if r.status in ("failed", "error")]
    bad.sort(key=lambda r: (not r.is_blocking, r.name))
    lines = [f"{'*' if r.is_blocking else ''}{r.label}{'*' if r.is_blocking else ''} `{r.name}`: {r.message}"
             for r in bad[:10]]
    if len(bad) > 10:
        lines.append(f"...and {len(bad) - 10} more")
    text = head + ("\n" + "\n".join(lines) if lines else "")
    if report_url:
        text += f"\nReport: {report_url}"
    return {"text": text}


def send_slack(payload: dict, webhook_url: str, timeout: int = 10) -> int:
    req = urllib.request.Request(
        webhook_url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https webhook)
        return resp.status
