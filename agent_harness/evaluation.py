from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .reporting import load_events, validate_trace


PHASE_WEIGHTS = {
    "task_understanding": 0.10,
    "localization": 0.15,
    "failure_hypothesis": 0.15,
    "reproduction": 0.10,
    "patch_construction": 0.20,
    "testing_and_recovery": 0.15,
    "final_review": 0.05,
    "budget_and_context_discipline": 0.10,
}


def derive_metrics(run_root: Path | str) -> dict[str, Any]:
    root = Path(run_root)
    events = load_events(root)
    types = Counter(row["event_type"] for row in events)
    tools = [row for row in events if row["event_type"] == "tool_result"]
    tool_names = Counter(row.get("attributes", {}).get("tool", "unknown") for row in tools)
    errors = [row for row in tools if row.get("status") != "ok"]
    malformed = [
        row for row in errors
        if row.get("attributes", {}).get("error_type") in {"ToolArgumentsJSONError", "ValidationError"}
    ]
    commands = [row for row in tools if row.get("attributes", {}).get("tool") == "run_command"]
    timeouts = [row for row in commands if row.get("attributes", {}).get("error_type") == "TimeoutExceeded"]
    repeated = 0
    prior = None
    for row in [event for event in events if event["event_type"] == "tool_call"]:
        signature = (
            row.get("attributes", {}).get("tool"),
            row.get("attributes", {}).get("arguments_sha256"),
        )
        repeated += signature == prior
        prior = signature
    outcome_path = root / "final" / "outcome.json"
    outcome = json.loads(outcome_path.read_text(encoding="utf-8")) if outcome_path.exists() else {}
    summary_path = root / "final" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    context_events = [row for row in events if row["event_type"] == "context_measurement"]
    max_context = max(
        (row.get("attributes", {}).get("utilization", 0.0) for row in context_events), default=0.0
    )
    return {
        "schema_version": "1.0",
        "trace_validation": validate_trace(root, require_complete=False),
        "outcome": outcome,
        "termination_reason": summary.get("termination_reason"),
        "events": dict(types),
        "components": {
            "model_requests": types["model_request"],
            "tool_calls": len(tools),
            "tools_by_name": dict(tool_names),
            "tool_error_count": len(errors),
            "malformed_tool_call_count": len(malformed),
            "command_count": len(commands),
            "command_timeout_count": len(timeouts),
            "immediate_repeated_tool_call_count": repeated,
            "context_compaction_count": types["context_compaction"],
            "maximum_context_utilization": max_context,
            "patch_capture_count": types["patch_capture"],
            "verification_count": types["verification_result"],
        },
        "trajectory_phase_weights": PHASE_WEIGHTS,
    }


def weighted_trajectory_score(phases: dict[str, Any]) -> float | None:
    weighted = 0.0
    used = 0.0
    for name, weight in PHASE_WEIGHTS.items():
        value = phases.get(name, {}).get("score") if isinstance(phases.get(name), dict) else None
        if isinstance(value, int) and 0 <= value <= 4:
            weighted += (value / 4.0) * weight
            used += weight
    return round(weighted / used * 100, 2) if used else None
