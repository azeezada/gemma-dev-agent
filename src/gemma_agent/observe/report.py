"""Weekly one-page report (plan 12): GPU-h burn vs budget, adapter x harness grid, top failure categories, gates.
Built from `runs/INDEX.csv` + manifests. Eval/rollout manifests may carry `summary.outcomes` and
`summary.termination_reasons` (counts); train/eval manifests may carry `summary.gates` ({name: bool}).
Usage: python -m gemma_agent.observe.report runs runs/weekly_report.md [--days 7] [--budget 30]"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .registry import load_index

WEEKLY_GPU_HOURS = 30.0


def _when(row: dict[str, str]) -> datetime:
    return datetime.strptime(row["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def build_report(runs_root: Path, *, days: int = 7, budget: float = WEEKLY_GPU_HOURS,
                 now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    rows = [r for r in load_index(runs_root) if _when(r) >= now - timedelta(days=days)]
    summaries = {r["run_id"]: json.loads(r["summary"] or "{}") for r in rows}
    gpu = sum(float(r["gpu_hours"] or 0) for r in rows)
    by_kind = Counter()
    for r in rows:
        by_kind[r["kind"]] += float(r["gpu_hours"] or 0)
    out = [f"# Weekly report ({(now - timedelta(days=days)):%Y-%m-%d} to {now:%Y-%m-%d})", "",
           f"**GPU-h:** {gpu:.1f} of {budget:.0f} ({100 * gpu / budget:.0f} %) across {len(rows)} runs", ""]
    if by_kind:
        out += ["| kind | runs | GPU-h |", "|---|---:|---:|"]
        out += [f"| {k} | {sum(r['kind'] == k for r in rows)} | {v:.1f} |" for k, v in sorted(by_kind.items())] + [""]
    grid = {}
    for r in sorted(rows, key=_when):
        s = summaries[r["run_id"]]
        if r["kind"] == "eval" and "resolve_rate" in s:
            grid[((r["adapter_sha256"] or "")[:10] or "base", r["contract_id"])] = (s["resolve_rate"], s.get("n", "?"))
    out += ["## Adapter x harness (latest eval)", ""]
    if grid:
        out += ["| adapter | contract | resolve rate | n |", "|---|---|---:|---:|"]
        out += [f"| {a} | {c} | {v:.3f} | {n} |" for (a, c), (v, n) in sorted(grid.items())]
    else:
        out.append("No eval runs this week.")
    failures = Counter()
    for s in summaries.values():
        failures.update({k: v for k, v in (s.get("outcomes") or {}).items() if k != "PASS"})
        failures.update(s.get("termination_reasons") or {})
    out += ["", "## Top failure categories", ""]
    out += [f"- {k}: {v}" for k, v in failures.most_common(8)] or ["None recorded."]
    gates = [(r["run_id"], name, ok) for r in rows for name, ok in (summaries[r["run_id"]].get("gates") or {}).items()]
    out += ["", "## Gates", ""]
    out += [f"- {run}: {name} {'PASS' if ok else '**FAIL**'}" for run, name, ok in gates] or ["None recorded."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--budget", type=float, default=WEEKLY_GPU_HOURS)
    args = parser.parse_args(argv)
    args.out.write_text(build_report(args.runs_root, days=args.days, budget=args.budget), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
