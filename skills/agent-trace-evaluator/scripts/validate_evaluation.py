#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

PHASES = {
    "task_understanding", "localization", "failure_hypothesis", "reproduction",
    "patch_construction", "testing_and_recovery", "final_review", "budget_and_context_discipline",
}
CATEGORIES = {
    "TASK_UNDERSTANDING", "LOCALIZATION", "HYPOTHESIS", "TOOL_INTERFACE", "EDIT_EXECUTION",
    "VERIFICATION", "BUDGET", "CONTEXT", "HARNESS", "ENVIRONMENT", "TASK_EVAL", "NONE",
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evaluation_directory", type=Path)
    args = parser.parse_args()
    root = args.evaluation_directory
    value = json.loads((root / "evaluation.json").read_text())
    metrics = json.loads((root / "deterministic_metrics.json").read_text())
    errors = []
    if value.get("outcome", {}).get("classification") != metrics.get("outcome", {}).get("classification"):
        errors.append("Teacher outcome differs from deterministic harness outcome")
    phases = value.get("trajectory", {}).get("phases", {})
    if set(phases) != PHASES:
        errors.append("Trajectory phases are missing or unexpected")
    evidence_ids = set()
    evidence_path = root / "evidence.jsonl"
    for number, line in enumerate(evidence_path.read_text().splitlines(), 1):
        try:
            row = json.loads(line)
            evidence_ids.add(row["evidence_id"])
            if not row.get("event_sequences") or not row.get("span_ids"):
                errors.append(f"Evidence line {number} lacks event_sequences or span_ids")
            if any(not re.fullmatch(r"[0-9a-f]{16}", str(item)) for item in row.get("span_ids", [])):
                errors.append(f"Evidence line {number} has an invalid span_id")
        except Exception as exc:
            errors.append(f"Invalid evidence line {number}: {exc}")
    cited = []
    for phase in phases.values():
        score = phase.get("score")
        if score != "not_observable" and not (isinstance(score, int) and 0 <= score <= 4):
            errors.append(f"Invalid trajectory score: {score!r}")
        cited.extend(phase.get("evidence_ids", []))
    attribution = value.get("failure_attribution", {})
    if attribution.get("primary") not in CATEGORIES:
        errors.append("Invalid primary failure category")
    cited.extend(attribution.get("evidence_ids", []))
    for recommendation in value.get("recommendations", []):
        cited.extend(recommendation.get("evidence_ids", []))
    missing = sorted(set(cited) - evidence_ids)
    if missing:
        errors.append(f"Unknown evidence IDs: {missing}")
    confidence = value.get("teacher", {}).get("confidence")
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        errors.append("Teacher confidence must be between 0 and 1")
    if not (root / "evaluation.md").read_text().strip():
        errors.append("evaluation.md is empty")
    result = {"valid": not errors, "errors": errors, "evidence_count": len(evidence_ids)}
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
