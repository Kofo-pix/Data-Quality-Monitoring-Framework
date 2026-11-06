from __future__ import annotations

import html
import json
from pathlib import Path

from .models import RunSummary

_COLORS = {"PASS": "32", "FAIL": "31", "ERROR": "31", "WARN": "33", "SKIP": "90"}


def render_console(summary: RunSummary, color: bool = False) -> str:
    def paint(label: str) -> str:
        return f"\033[{_COLORS[label]}m{label:<5}\033[0m" if color else f"{label:<5}"

    lines = [f"Suite '{summary.suite}' | run #{summary.run_id} | as of {summary.as_of:%Y-%m-%d %H:%M}", ""]
    for r in sorted(summary.results, key=lambda r: (r.table, r.name)):
        lines.append(f"  {paint(r.label)} {r.name:<46} {r.message}")
    lines += [
        "",
        f"  Checks: {summary.total} | passed {summary.passed} | failed {summary.failed} | "
        f"errored {summary.errored} | skipped {summary.skipped}",
        f"  Quality score: {summary.score}% | blocking issues: {summary.blocking} | "
        f"warnings: {summary.warnings}",
        f"  GATE: {'PASSED' if summary.gate_passed else 'FAILED'}",
    ]
    return "\n".join(lines)


def write_json(summary: RunSummary, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(summary.to_dict(), indent=2, default=str), encoding="utf-8")


# ----------------------------------------------------------------------------- HTML ------
_CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2330;--muted:#667085;--line:#e4e7ec;--ok:#12805c;--okbg:#e6f6ef;
--bad:#b42318;--badbg:#fdeceb;--warn:#a15c07;--warnbg:#fff4de;--skip:#667085;--skipbg:#eef0f3;--accent:#3b5bdb}
@media (prefers-color-scheme:dark){:root{--bg:#0f1318;--card:#171c24;--text:#e6e9ef;--muted:#98a2b3;--line:#2a313c;
--ok:#4ad19b;--okbg:#10291f;--bad:#ff8a80;--badbg:#341613;--warn:#f5b94c;--warnbg:#33260c;--skip:#98a2b3;--skipbg:#222a35;--accent:#8aa4ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px}
.wrap{max-width:1080px;margin:0 auto}h1{font-size:22px;margin:0}h2{font-size:16px;margin:28px 0 10px}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.gate{padding:6px 16px;border-radius:999px;font-weight:700;font-size:14px}
.gate.ok{background:var(--okbg);color:var(--ok)}.gate.bad{background:var(--badbg);color:var(--bad)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px;margin:18px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.kpi b{display:block;font-size:24px}.kpi span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
tr:last-child td{border-bottom:0}.tag{display:inline-block;min-width:52px;text-align:center;padding:2px 8px;
border-radius:6px;font-size:12px;font-weight:700}
.PASS{background:var(--okbg);color:var(--ok)}.FAIL,.ERROR{background:var(--badbg);color:var(--bad)}
.WARN{background:var(--warnbg);color:var(--warn)}.SKIP{background:var(--skipbg);color:var(--skip)}
code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
details summary{cursor:pointer;color:var(--accent);font-size:12.5px;margin-top:4px}
.mini{margin-top:6px;font-size:12px}.mini td,.mini th{padding:4px 8px;border:1px solid var(--line)}
.trend{padding:14px}.legend{color:var(--muted);font-size:12px;margin-top:6px}
"""


def _trend_svg(runs: list[dict]) -> str:
    if not runs:
        return ""
    w, h, pad = 1000, 120, 24
    n = len(runs)
    slot = (w - 2 * pad) / n
    bw = min(56, slot * 0.6)
    bars = []
    for i, r in enumerate(runs):
        score = r["score"] or 0
        bh = max(2, (h - 40) * score / 100)
        x = pad + i * slot + (slot - bw) / 2
        y = h - 20 - bh
        color = "var(--ok)" if r["gate_passed"] else "var(--bad)"
        bars.append(
            f'<g><title>Run #{r["run_id"]}: {score}% | gate {"passed" if r["gate_passed"] else "failed"}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="4" fill="{color}" opacity=".85"/>'
            f'<text x="{x + bw / 2:.1f}" y="{y - 4:.1f}" text-anchor="middle" font-size="12" fill="currentColor">{score:g}%</text>'
            f'<text x="{x + bw / 2:.1f}" y="{h - 4}" text-anchor="middle" font-size="11" fill="var(--muted)">#{r["run_id"]}</text></g>'
        )
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Quality score by run">'
            + "".join(bars) + "</svg>")


def _sample_table(sample: list[dict]) -> str:
    if not sample:
        return ""
    cols = list(sample[0].keys())
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in cols)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(r.get(c)))}</td>" for c in cols) + "</tr>" for r in sample
    )
    return (f'<details><summary>{len(sample)} sample rows</summary>'
            f'<div class="scroll"><table class="mini"><tr>{head}</tr>{body}</table></div></details>')


def render_html(summary: RunSummary, runs: list[dict] | None = None) -> str:
    e = html.escape
    order = {"ERROR": 0, "FAIL": 0, "WARN": 1, "SKIP": 2, "PASS": 3}
    rows = []
    for r in sorted(summary.results, key=lambda r: (order[r.label], r.table, r.name)):
        rows.append(
            f'<tr><td><span class="tag {r.label}">{r.label}</span></td>'
            f"<td><code>{e(r.name)}</code></td><td>{e(r.severity)}</td>"
            f"<td>{e(r.message)}{_sample_table(r.sample)}</td></tr>"
        )
    gate_cls, gate_txt = ("ok", "GATE PASSED") if summary.gate_passed else ("bad", "GATE FAILED")
    trend = (f'<h2>Quality trend</h2><div class="card trend">{_trend_svg(runs)}'
             f'<div class="legend">Quality score per run. Red = run blocked by error-level failures.</div></div>'
             if runs else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Data quality report - {e(summary.suite)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<div class="head"><div><h1>Data quality report: {e(summary.suite)}</h1>
<div class="sub">Run #{summary.run_id} &middot; as of {summary.as_of:%Y-%m-%d %H:%M}</div></div>
<div class="gate {gate_cls}">{gate_txt}</div></div>
<div class="kpis">
<div class="kpi"><b>{summary.score:g}%</b><span>Quality score</span></div>
<div class="kpi"><b>{summary.passed}</b><span>Passed</span></div>
<div class="kpi"><b>{summary.blocking}</b><span>Blocking failures</span></div>
<div class="kpi"><b>{summary.warnings}</b><span>Warnings</span></div>
<div class="kpi"><b>{summary.skipped}</b><span>Skipped</span></div></div>
{trend}
<h2>Check results ({summary.total})</h2>
<div class="card scroll"><table><tr><th>Status</th><th>Check</th><th>Severity</th><th>Details</th></tr>
{''.join(rows)}</table></div>
</div></body></html>"""


def write_html(summary: RunSummary, path: str | Path, runs: list[dict] | None = None) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(render_html(summary, runs), encoding="utf-8")
