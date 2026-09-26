"""Static HTML dashboard generated from `runs/` (INDEX.csv, manifests, metrics.jsonl). No server, no JS deps.
Usage: python -m gemma_agent.observe.dashboard runs runs/dashboard.html"""
from __future__ import annotations

import html
import json
import sys
from pathlib import Path

from .registry import load_index, read_metrics

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--acc:#2a6fdb}
@media (prefers-color-scheme:dark){:root{--bg:#14161a;--fg:#e8e8e8;--mut:#9aa0a6;--line:#2c3036;--acc:#6ea8ff}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}table{border-collapse:collapse;width:100%;display:block;overflow-x:auto}
th,td{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;white-space:nowrap}th{color:var(--mut);font-weight:600}
.n{text-align:right;font-variant-numeric:tabular-nums}.mut{color:var(--mut)}
"""


def _table(headers: list[str], rows: list[list[str]], numeric: set[int] = frozenset()) -> str:
    head = "".join(f"<th>{html.escape(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(
        f'<td class="{"n" if i in numeric else ""}">{html.escape(str(c))}</td>' for i, c in enumerate(row)) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _latest_metrics(run_dir: Path) -> dict[str, float]:
    latest: dict[str, float] = {}
    for row in read_metrics(run_dir):
        latest[row["name"]] = row["value"]
    return latest


def build_dashboard(runs_root: Path) -> str:
    runs_root = Path(runs_root)
    index = load_index(runs_root)
    gpu_total = sum(float(r["gpu_hours"] or 0) for r in index)
    sections = [f"<h1>gemma-dev runs</h1><p class='mut'>{len(index)} runs, {gpu_total:.2f} GPU-h total</p>"]
    sections.append("<h2>Runs</h2>" + _table(
        ["run", "kind", "contract", "git", "adapter", "GPU-h", "created"],
        [[r["run_id"], r["kind"], r["contract_id"], r["git_sha"], (r["adapter_sha256"] or "")[:10], r["gpu_hours"], r["created_at"]]
         for r in index], numeric={5}))
    by_kind: dict[str, float] = {}
    for r in index:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0.0) + float(r["gpu_hours"] or 0)
    sections.append("<h2>GPU-h by kind</h2>" + _table(["kind", "GPU-h"], [[k, f"{v:.2f}"] for k, v in sorted(by_kind.items())], {1}))
    metric_rows = []
    for r in index:
        for name, value in sorted(_latest_metrics(runs_root / r["run_id"]).items()):
            metric_rows.append([r["run_id"], name, f"{value:.4g}"])
    if metric_rows:
        sections.append("<h2>Latest metrics</h2>" + _table(["run", "metric", "value"], metric_rows, {2}))
    grid = {}
    for r in index:
        if r["kind"] == "eval":
            s = json.loads(r["summary"] or "{}")
            if "resolve_rate" in s:
                grid[(r["adapter_sha256"][:10] or "base", r["contract_id"])] = s["resolve_rate"]
    if grid:
        sections.append("<h2>Adapter × harness (resolve rate)</h2>" + _table(
            ["adapter", "contract", "resolve rate"], [[a, c, f"{v:.3f}"] for (a, c), v in sorted(grid.items())], {2}))
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>gemma-dev runs</title><style>{CSS}</style></head><body>{''.join(sections)}</body></html>")


def main(argv: list[str]) -> int:
    runs_root, out = Path(argv[1]), Path(argv[2])
    out.write_text(build_dashboard(runs_root), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
