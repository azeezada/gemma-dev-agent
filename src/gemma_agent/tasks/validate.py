"""Per-task validity gate `env_ok` (plan 6.2.3).

A task is usable for training or local eval only if, in our environment, the untouched snapshot FAILS the whole
target test files (a real test failure, not an import/collection error) and the gold patch PASSES them. Both
checks reuse the rollout verifier (`_verify_phase`), i.e. the competition's whole-file pytest command.

The gold patch and test patch are controller-only inputs; nothing here involves an agent.
Usage (Kaggle CPU notebook or GPU notebook, offline wheels attached):
    python -m gemma_agent.tasks.validate data/tasks.jsonl data/snapshots out/env_ok.jsonl \
        --wheels <wheels_dir> [--pytest-version 8.3.4] [--task-ids a b ...]
"""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from gemma_agent.env.harness import parse_patch_targets
from gemma_agent.rollout.runtime import _verify_phase
from gemma_agent.rollout.schemas import TaskSpec, VerificationSpec
from gemma_agent.rollout.tracing import TraceRecorder

KEEP = ("resolved", "test_failure", "infrastructure_error", "exit_code", "elapsed_seconds", "timed_out", "error")


def pytest_command(test_patch: str) -> str:
    """HARNESS_README 8.2 step 4, on the whole files the test patch touches."""
    return shlex.join([
        "python", "-m", "pytest", *parse_patch_targets(test_patch), "-p", "no:anyio",
        "-o", "timeout=0", "-o", "norecursedirs=.* build dist venv", "-o", "python_classes=Test* *Test", "-q",
    ])


def classify(baseline: dict[str, Any], gold: dict[str, Any]) -> str:
    """A gold pass proves the environment; the baseline may then fail for any reason (often an ImportError of
    the symbol the fix adds), exactly as the competition's exit-code criterion allows."""
    if gold.get("error"):
        return "gold_setup_error"  # gold patch did not apply, or environment setup failed
    if not gold.get("resolved"):
        return "gold_infra_error" if gold.get("infrastructure_error") else "gold_fails"
    if baseline.get("error"):
        return "baseline_setup_error"
    if baseline.get("resolved"):
        return "baseline_passes"
    return "ok"


def validate_task(task: dict[str, Any], snapshot: Path, work_root: Path, *, wheels: Path | None = None,
                  pytest_version: str | None = None, timeout_seconds: int = 600,
                  command: str | None = None) -> dict[str, Any]:
    """`command` overrides the whole-file pytest command (unit tests only)."""
    task_id = task["instance_id"]
    root = Path(work_root) / task_id
    if root.exists():
        shutil.rmtree(root)
    control = root / "control"
    control.mkdir(parents=True)
    test_patch = control / "test.patch"
    test_patch.write_text(task["test_patch"], encoding="utf-8")
    started = time.monotonic()
    spec = TaskSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "repository": task["repo"],
        "problem_statement": task.get("problem_statement") or "-", "hints": task.get("hints_text", ""),
        "snapshot_path": str(snapshot),
    })
    verification = VerificationSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "test_patch_path": str(test_patch),
        "verification_command": command or pytest_command(task["test_patch"]), "timeout_seconds": timeout_seconds,
    })
    trace = TraceRecorder(root / "trace", experiment_id="env_ok", run_id=task_id, task_id=task_id)
    config = SimpleNamespace(wheels_directory=wheels, pytest_version=pytest_version)
    phases: dict[str, dict[str, Any]] = {}
    for phase, patch in (("baseline", ""), ("gold", task["patch"])):
        try:
            phases[phase] = _verify_phase(phase=phase, task=spec, verification=verification, agent_patch=patch,
                                          work_root=root / "work", trace=trace, run_config=config)
        except Exception as exc:  # environment build failures are a result, not a crash
            phases[phase] = {"resolved": False, "infrastructure_error": True, "error": f"{type(exc).__name__}: {exc}"}
    reason = classify(phases["baseline"], phases["gold"])
    shutil.rmtree(root / "work", ignore_errors=True)
    shutil.rmtree(control, ignore_errors=True)
    return {
        "task_id": task_id, "repo": task["repo"], "env_ok": reason == "ok", "reason": reason,
        "targets": parse_patch_targets(task["test_patch"]),
        "baseline": {k: phases["baseline"].get(k) for k in KEEP},
        "gold": {k: phases["gold"].get(k) for k in KEEP},
        "python": sys.version.split()[0], "pytest_version": pytest_version,
        "seconds": round(time.monotonic() - started, 1),
    }


def load_env_ok(path: Path) -> dict[str, dict[str, Any]]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return {row["task_id"]: row for row in rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("tasks", type=Path)
    parser.add_argument("snapshots", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--wheels", type=Path)
    parser.add_argument("--pytest-version")
    parser.add_argument("--work", type=Path, default=Path("/tmp/env_ok_work"))
    parser.add_argument("--task-ids", nargs="*")
    args = parser.parse_args(argv)
    tasks = [json.loads(line) for line in args.tasks.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.task_ids:
        tasks = [t for t in tasks if t["instance_id"] in set(args.task_ids)]
    done = load_env_ok(args.out) if args.out.is_file() else {}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as handle:
        for task in tasks:
            if task["instance_id"] in done:
                continue
            snapshot = args.snapshots / f"{task['instance_id']}.tgz"
            row = validate_task(task, snapshot, args.work, wheels=args.wheels, pytest_version=args.pytest_version) \
                if snapshot.is_file() else {"task_id": task["instance_id"], "repo": task["repo"], "env_ok": False,
                                            "reason": "missing_snapshot"}
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            print(f"{row['task_id']}: {row['reason']}", flush=True)
    rows = load_env_ok(args.out)
    print(json.dumps({"tasks": len(rows), "env_ok": sum(r["env_ok"] for r in rows.values())}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
