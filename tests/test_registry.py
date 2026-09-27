import json

import pytest

from gemma_agent.observe.dashboard import build_dashboard
from gemma_agent.observe.registry import load_index, log_metric, pull_run, read_metrics, write_manifest


def _run(tmp_path, run_id, kind="eval", **kw):
    run = tmp_path / "kaggle_out" / run_id
    adapter = tmp_path / "adapter.safetensors"
    adapter.write_bytes(b"weights")
    write_manifest(run, run_id=run_id, kind=kind, contract_id="train_v1", config={"k": 1}, adapter=adapter, **kw)
    log_metric(run, "resolve_rate", 0.25, stage="eval")
    return run


def test_manifest_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError):
        write_manifest(tmp_path / "r", run_id="r", kind="bogus", contract_id="x")


def test_pull_is_idempotent_and_indexed(tmp_path):
    runs = tmp_path / "runs"
    run = _run(tmp_path, "eval_1", gpu_hours=1.5, summary={"resolve_rate": 0.25})
    pull_run(run, runs)
    pull_run(run, runs)
    index = load_index(runs)
    assert [r["run_id"] for r in index] == ["eval_1"]
    assert index[0]["contract_id"] == "train_v1" and len(index[0]["adapter_sha256"]) == 64
    assert read_metrics(runs / "eval_1")[0]["value"] == 0.25


def test_dashboard_renders_runs_metrics_and_grid(tmp_path):
    runs = tmp_path / "runs"
    pull_run(_run(tmp_path, "eval_1", gpu_hours=1.5, summary={"resolve_rate": 0.25}), runs)
    page = build_dashboard(runs)
    assert "eval_1" in page and "resolve_rate" in page and "Adapter × harness" in page and "1.50" in page
    assert json.loads(json.dumps(page))  # plain str


def test_weekly_report_burn_grid_failures_and_gates(tmp_path):
    from datetime import datetime, timezone

    from gemma_agent.observe.report import build_report
    runs = tmp_path / "runs"
    pull_run(_run(tmp_path, "eval_1", gpu_hours=3.0, summary={
        "resolve_rate": 0.25, "n": 8, "outcomes": {"PASS": 2, "FAIL": 5, "INFRA_ERROR": 1},
        "termination_reasons": {"time_budget_exhausted": 4}, "gates": {"parity": False}}), runs)
    now = datetime.strptime(load_index(runs)[0]["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    page = build_report(runs, now=now)
    assert "3.0 of 30 (10 %)" in page and "| 0.250 | 8 |" in page
    assert "- FAIL: 5" in page and "time_budget_exhausted: 4" in page and "parity **FAIL**" in page
