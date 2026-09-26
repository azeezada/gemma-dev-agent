#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import time
from pathlib import Path

from _bootstrap import add_project_root

add_project_root()
from gemma_agent.rollout.evaluation import PHASE_WEIGHTS, derive_metrics  # noqa: E402
from gemma_agent.rollout.reporting import validate_trace  # noqa: E402
from gemma_agent.rollout.tracing import atomic_json, atomic_write, utc_now  # noqa: E402


def tree_hash(path: Path) -> str:
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rows.append(item.relative_to(path).as_posix() + ":" + hashlib.sha256(item.read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--teacher-model", default="unspecified")
    args = parser.parse_args()
    run = args.run_directory.resolve()
    validation = validate_trace(run, require_complete=False)
    if not validation["valid"]:
        raise SystemExit("Trace validation failed; inspect validate_trace.py output")
    state = json.loads((run / "state.json").read_text())
    outcome = json.loads((run / "final" / "outcome.json").read_text())
    evaluation_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + secrets.token_hex(4)
    destination = run / "teacher" / evaluation_id
    destination.mkdir(parents=True, exist_ok=False)
    metrics = derive_metrics(run)
    atomic_json(destination / "deterministic_metrics.json", metrics)
    reference_hash = tree_hash(args.reference.resolve()) if args.reference else None
    manifest = {
        "schema_version": "1.0",
        "evaluation_id": evaluation_id,
        "created_utc": utc_now(),
        "run_id": state["run_id"],
        "trace_id": state["trace_id"],
        "teacher_model": args.teacher_model,
        "skill_version": "1.0",
        "reference_bundle_sha256": reference_hash,
        "solver_seal_sha256": hashlib.sha256((run / "run_manifest.json").read_bytes()).hexdigest(),
    }
    atomic_json(destination / "teacher_manifest.json", manifest)
    phases = {
        name: {"score": "not_observable", "rationale": "", "evidence_ids": []}
        for name in PHASE_WEIGHTS
    }
    skeleton = {
        "schema_version": "1.0",
        "evaluation_id": evaluation_id,
        "run_id": state["run_id"],
        "teacher": {"model": args.teacher_model, "skill_version": "1.0", "confidence": 0.0},
        "outcome": {"classification": outcome["classification"], "matches_harness": True},
        "trajectory": {"phases": phases, "weighted_score": None, "observable_weight": 0.0},
        "components": {"findings": []},
        "failure_attribution": {"primary": "", "secondary": [], "rationale": "", "evidence_ids": []},
        "recommendations": [],
        "limitations": [],
    }
    atomic_json(destination / "evaluation.json", skeleton)
    atomic_write(destination / "evidence.jsonl", b"")
    atomic_write(destination / "evaluation.md", b"# Agent run evaluation\n\nComplete using the trace evidence.\n")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
