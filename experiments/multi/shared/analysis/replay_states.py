"""Replay every intermediate workspace state of every traced run against the hidden tests.

    python3 experiments/multi/shared/analysis/replay_states.py [--tasks rich_4076 ...]

Controller-only (reads hidden test_patch from tasks.jsonl). Never embed in a solver.
Each tool result that changed the workspace stores a cumulative diff vs the snapshot HEAD
(workspace/diffs/*.patch). Each distinct diff is applied to work/review-snapshots/<task>, the
target test files are reset and the hidden test_patch applied, then the target tests run with
work/review-env. Results are cached in replay_cache.json as {"task:sha256(diff)": PASS|FAIL|APPLY_ERROR}.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[3]
sys.path.insert(0, str(PROJECT / "src"))
sys.path.insert(0, str(HERE))
from aggregate import discover, load_tasks, patch_files  # noqa: E402
from gemma_agent.env.harness import parse_patch_targets, task_pytest_plugins  # noqa: E402

PYTHON = PROJECT / "work" / "review-env" / "bin" / "python"
CACHE = HERE / "replay_cache.json"


def states() -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for _, _, run in discover():
        events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        task_id = events[0].get("task_id")
        for e in events:
            path = e.get("attributes", {}).get("workspace_diff_path")
            if e["event_type"] == "tool_result" and path and (run / path).is_file():
                diff = (run / path).read_text(errors="replace")
                found.setdefault(task_id, {})[hashlib.sha256(diff.encode()).hexdigest()] = diff
        final = run / "final" / "agent.patch"
        if final.is_file() and final.read_text().strip():
            diff = final.read_text()
            found.setdefault(task_id, {})[hashlib.sha256(diff.encode()).hexdigest()] = diff
    return found


def apply(repo: Path, diff: str) -> bool:
    """git apply, tolerating a missing final newline (rstrip would eat trailing context spaces)."""
    text = diff if diff.endswith("\n") else diff + "\n"
    for extra in ([], ["--recount"], ["--recount", "--ignore-whitespace"]):
        if subprocess.run(["git", "apply", "--whitespace=nowarn", *extra, "-"], input=text, text=True,
                          cwd=repo, capture_output=True).returncode == 0:
            return True
    return False


def verify(task: dict, diff: str) -> str:
    source = PROJECT / "work" / "review-snapshots" / task["instance_id"]
    with tempfile.TemporaryDirectory(prefix="replay-") as tmp:
        repo = Path(tmp)
        archive = subprocess.run(["git", "archive", "HEAD"], cwd=source, capture_output=True, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
            handle.extractall(repo, filter="data")
        for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                    ["git", "-c", "user.email=e@e", "-c", "user.name=e", "commit", "-qm", "base"]):
            subprocess.run(cmd, cwd=repo, capture_output=True, check=True)
        if not apply(repo, diff):
            return "APPLY_ERROR"
        targets = parse_patch_targets(task["test_patch"])
        for target in targets:
            subprocess.run(["git", "checkout", "HEAD", "--", target], cwd=repo, capture_output=True)
            subprocess.run(["git", "clean", "-f", "--", target], cwd=repo, capture_output=True)
        if not apply(repo, task["test_patch"]):
            return "TEST_PATCH_ERROR"
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(repo / "src"), str(repo)]),
               "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": task_pytest_plugins(PYTHON, repo),
               "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            result = subprocess.run([str(PYTHON), "-m", "pytest", *targets, "-p", "no:anyio", "-o", "timeout=0",
                                     "-q", "-x"], cwd=repo, env=env, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return "TIMEOUT"
        return "PASS" if result.returncode == 0 else "FAIL"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", nargs="*")
    args = parser.parse_args()
    tasks = load_tasks()
    cache = json.loads(CACHE.read_text()) if CACHE.is_file() else {}
    for task_id, diffs in sorted(states().items()):
        if args.tasks and task_id not in args.tasks:
            continue
        if not (PROJECT / "work" / "review-snapshots" / task_id).is_dir():
            print("skip (no review snapshot)", task_id)
            continue
        for sha, diff in diffs.items():
            key = f"{task_id}:{sha}"
            if key in cache:
                continue
            try:
                cache[key] = verify(tasks[task_id], diff)
            except Exception as exc:  # keep replaying the rest
                cache[key] = f"ERROR:{type(exc).__name__}"
            print(task_id, sha[:10], cache[key], patch_files(diff), flush=True)
            CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))
    print({v: sum(1 for x in cache.values() if x == v) for v in set(cache.values())})


if __name__ == "__main__":
    main()
