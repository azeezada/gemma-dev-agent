"""Build a self-contained multi-task Kaggle notebook for the traced harness."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT / "notebooks" / "gemma_agent_trace.ipynb"


def _runtime_sources() -> dict[str, str]:
    roots = (PROJECT / "agent_harness", PROJECT / "mvp_v2")
    sources = {}
    for root in roots:
        for path in sorted(root.glob("*.py")):
            relative = path.relative_to(PROJECT).as_posix()
            sources[relative] = path.read_text(encoding="utf-8")
    return sources


CORE_TASK_IDS = (
    "fastapi_14492",
    "fastapi_14258",
    "rich_4076",
    "rich_3468",
    "requests_7205",
    "requests_6629",
    "httpx_3672",
)
# v21 additions: validated locally (baseline fails, gold passes, no pytest-httpbin).
EXTRA_TASK_IDS = (
    "fastapi_14786",
    "fastapi_14873",
    "fastapi_14458",
    "fastapi_12942",
    "fastapi_15588",
    "fastapi_14372",
    "fastapi_13713",
    "rich_4077",
    "rich_3882",
    "rich_3521",
    "rich_3052",
    "rich_3718",
    "requests_7309",
)


def build(core_only: bool = True, *, submission_dir: Path | None = None,
          output: Path | None = None, interactive: bool = False,
          supplemental_wheels: bool = False) -> Path:
    task_ids = CORE_TASK_IDS if core_only else CORE_TASK_IDS + EXTRA_TASK_IDS
    submission_dir = submission_dir or PROJECT / "harnesses" / "v22"
    output = output or OUTPUT
    sources = _runtime_sources()
    submission = {
        path.relative_to(submission_dir).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(submission_dir.rglob("*"))
        if path.is_file() and path.suffix != ".pyc" and not any(
            part.startswith(".") or part == "__pycache__"
            for part in path.relative_to(submission_dir).parts
        )
    }
    cells = []

    def markdown(value: str) -> None:
        cells.append({"cell_type": "markdown", "metadata": {}, "source": value.splitlines(keepends=True)})

    def code(value: str) -> None:
        ast.parse(value)
        cells.append({
            "cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
            "source": value.splitlines(keepends=True),
        })

    markdown("""# Gemma traced development harness

Runs the configured public tasks with the exact agent configuration and skill scripts and one shared model server. Each attempt gets an isolated solver workspace, separate baseline and patched verification workspaces, and a sealed replayable trace bundle. Private tests are created in a controller-only directory after task selection and removed when the run ends.

This notebook creates `submission.zip` but does not submit it to the competition.
""")
    code('''from __future__ import annotations
import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from IPython.display import JSON, display

TASK_IDS = __TASK_IDS__
MODEL_ROOT = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
DATA_ROOT = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WORK_ROOT = Path("/kaggle/working")
RUNTIME_ROOT = WORK_ROOT / "traced_agent_runtime"
OUTPUT_ROOT = WORK_ROOT / "trace_runs"
CONTROL_ROOT = WORK_ROOT / "private_verification_control"
SUBMISSION_ZIP = WORK_ROOT / "submission.zip"
DIAGNOSTICS_ZIP = WORK_ROOT / "trace_diagnostics.zip"
VLLM_VERSION = "0.29.0"
MAX_MODEL_LEN = 32768
NOTEBOOK_STARTED = time.monotonic()
# Kaggle stops the session at 7200 s. Stop starting tasks early enough that the
# slowest observed task (~390 s) plus sealing and zipping still fit.
TASK_START_CUTOFF_SECONDS = 7200 - 900
print(f"Configured traced run for {len(TASK_IDS)} tasks: {', '.join(TASK_IDS)}")
'''.replace("__TASK_IDS__", repr(task_ids)))
    markdown("## Prepare the proven offline serving environment")
    code('''wheel_candidates = [Path("/kaggle/input/datasets/nguyenkhaho/vllm-wheel/wheels"), Path("/kaggle/input/vllm-wheel/wheels")]
wheel_root = next((path for path in wheel_candidates if path.is_dir()), None)
if wheel_root is None:
    matches = list(Path("/kaggle/input").glob("**/vllm-0.29.0-*.whl"))
    wheel_root = matches[0].parent if matches else None
if wheel_root is None:
    raise FileNotFoundError("Attach nguyenkhaho/vllm-wheel")
try:
    installed = importlib.metadata.version("vllm")
except importlib.metadata.PackageNotFoundError:
    installed = None
if installed != VLLM_VERSION:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "--no-index",
         f"--find-links={wheel_root}", f"vllm=={VLLM_VERSION}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
    )
    print(result.stdout[-4000:])
    if result.returncode:
        raise RuntimeError("Offline vLLM installation failed")
if importlib.metadata.version("vllm") != VLLM_VERSION:
    raise RuntimeError("Unexpected vLLM version")
''')
    markdown("## Materialize the versioned harness source")
    code("RUNTIME_SOURCES = " + json.dumps(sources, ensure_ascii=False) + "\n" + '''
if RUNTIME_ROOT.exists():
    shutil.rmtree(RUNTIME_ROOT)
for relative, content in RUNTIME_SOURCES.items():
    destination = RUNTIME_ROOT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
sys.path.insert(0, str(RUNTIME_ROOT))
from agent_harness import RunConfig, TaskSpec, VerificationSpec, run_task
from mvp_v2.harness import VLLMServer, build_submission_zip, parse_patch_targets, zip_directory
from mvp_v2.v3 import load_submission
''')
    markdown("## Complete the offline test environment")
    # Kaggle rejects notebook sources over 1 MB, so the wheels are only embedded on request.
    wheel_paths = sorted((PROJECT / "data" / "runtime_wheels").glob("*.whl")) if supplemental_wheels else []
    wheels = {path.name: base64.b64encode(path.read_bytes()).decode("ascii") for path in wheel_paths}
    wheel_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in wheel_paths}
    code("SUPPLEMENTAL_WHEELS = " + json.dumps(wheels) + "\nWHEEL_HASHES = " + json.dumps(wheel_hashes) + "\n" + '''
import base64
import hashlib
WHEELS_ROOT = WORK_ROOT / "trace_test_wheels"
WHEELS_ROOT.mkdir(exist_ok=True)
for source in (DATA_ROOT / "wheels").glob("*.whl"):
    destination = WHEELS_ROOT / source.name
    if not destination.exists():
        destination.symlink_to(source)
for name, encoded in SUPPLEMENTAL_WHEELS.items():
    data = base64.b64decode(encoded)
    if hashlib.sha256(data).hexdigest() != WHEEL_HASHES[name]:
        raise ValueError(f"Supplemental wheel hash mismatch: {name}")
    (WHEELS_ROOT / name).write_bytes(data)
print(f"Offline test wheel overlay ready: {len(SUPPLEMENTAL_WHEELS)} verified supplements")
''')
    markdown("## Prepare all benchmark cases and freeze configuration")
    code("SUBMISSION_FILES = " + json.dumps(submission, ensure_ascii=False) + "\n" + '''
submission_root = RUNTIME_ROOT / "submission"
for relative, content in SUBMISSION_FILES.items():
    destination = submission_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
submission_manifest = build_submission_zip(SUBMISSION_FILES, SUBMISSION_ZIP)
effective_submission = load_submission(SUBMISSION_FILES)
task_rows = {}
with (DATA_ROOT / "tasks.jsonl").open(encoding="utf-8") as handle:
    for line in handle:
        candidate = json.loads(line)
        if candidate.get("instance_id") in TASK_IDS:
            task_rows[candidate["instance_id"]] = candidate
missing = set(TASK_IDS) - set(task_rows)
if missing:
    raise KeyError(f"Tasks not found: {sorted(missing)}")
config_files = sorted(MODEL_ROOT.rglob("config.json"))
if not config_files:
    raise FileNotFoundError("Attached model config was not found")
MODEL_PATH = max(config_files, key=lambda path: sum(child.suffix == ".safetensors" for child in path.parent.iterdir())).parent
for stale in (OUTPUT_ROOT, CONTROL_ROOT):
    if stale.exists():
        shutil.rmtree(stale)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=False)
CONTROL_ROOT.mkdir(parents=True, exist_ok=False)
task_specs = {}
verification_specs = {}
verification_targets = {}
for task_id in TASK_IDS:
    task_row = task_rows[task_id]
    snapshot = DATA_ROOT / "snapshots" / f"{task_id}.tgz"
    graph = DATA_ROOT / "graphs" / f"{task_id}.json"
    embeddings = DATA_ROOT / "embeddings" / f"{task_id}.npz"
    private_test_patch = CONTROL_ROOT / f"{task_id}.patch"
    private_test_patch.write_text(task_row["test_patch"], encoding="utf-8")
    targets = parse_patch_targets(task_row["test_patch"])
    if not targets:
        raise ValueError(f"Private test patch has no target files: {task_id}")
    verification_command = shlex.join([
        "python", "-m", "pytest", *targets, "-p", "no:anyio",
        "-o", "timeout=0", "-o", "norecursedirs=.* build dist venv",
        "-o", "python_classes=Test* *Test", "-q",
    ])
    task_specs[task_id] = TaskSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "repository": task_row["repo"],
        "problem_statement": task_row["problem_statement"], "hints": task_row.get("hints_text", ""),
        "snapshot_path": str(snapshot),
        **({"graph_path": str(graph)} if graph.is_file() else {}),
        **({"embeddings_path": str(embeddings)} if embeddings.is_file() else {}),
    })
    verification_specs[task_id] = VerificationSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id,
        "test_patch_path": str(private_test_patch), "verification_command": verification_command,
        "timeout_seconds": 600,
    })
    verification_targets[task_id] = targets
run_config = RunConfig.from_dict({
    "schema_version": "1.0", "submission_directory": str(submission_root),
    "output_root": str(OUTPUT_ROOT), "work_root": str(WORK_ROOT / "trace_workspaces"),
    "experiment_name": "kaggle-public-trace", "seed": 42,
    "model": {"path": str(MODEL_PATH), "name": "gemma-4-31b-it-qat-w4a16-ct"},
    "server": {"max_model_len": MAX_MODEL_LEN, "startup_timeout": 1800},
    "wheels_directory": str(WHEELS_ROOT), "pytest_version": "8.3.4",
})
display(JSON({"task_ids": TASK_IDS, "targets": verification_targets,
              "configuration": effective_submission, "submission": submission_manifest,
              "runtime_source_sha256": {name: __import__('hashlib').sha256(value.encode()).hexdigest() for name, value in RUNTIME_SOURCES.items()}}))
''')
    markdown("""## Run selection — change only here, immediately before execution

The submission and tools above are shared by all seven cases. Select the cases for this execution below. For interactive work use this editor's Run controls; do not Save & Run through the CLI. Verification retains the whole-file pass criterion.
""")
    code('''RUN_TASK_IDS = TASK_IDS
if not RUN_TASK_IDS or set(RUN_TASK_IDS) - set(TASK_IDS):
    raise ValueError("Select one or more prepared benchmark tasks")
print("Running:", RUN_TASK_IDS)
''')
    markdown("## Run, verify, seal, and preserve diagnostics")
    code('''results = []
server = VLLMServer(
    model_path=MODEL_PATH,
    model_name="gemma-4-31b-it-qat-w4a16-ct",
    log_path=OUTPUT_ROOT / "server.log",
    max_model_len=MAX_MODEL_LEN,
    startup_timeout=1800,
)
server_profile = None
try:
    server_profile = server.start(
        status_callback=lambda status: print(
            f"Loading model: {status['elapsed_seconds']:.0f}s | {status['last_log']} | GPU: {status['gpu']}",
            flush=True,
        )
    )
    client = server.client
    tokenizer_profile = client.configure_tokenizer(MODEL_PATH, effective_submission["tools"])
    for task_id in RUN_TASK_IDS:
        elapsed = time.monotonic() - NOTEBOOK_STARTED
        if elapsed > TASK_START_CUTOFF_SECONDS:
            skipped = [row for row in RUN_TASK_IDS if row not in {result["task_id"] for result in results}]
            print(f"Time cutoff reached at {elapsed:.0f}s; skipping {skipped}", flush=True)
            break
        print(f"Starting {task_id} at {elapsed:.0f}s", flush=True)
        result = run_task(
            task_specs[task_id], verification_specs[task_id], run_config, client=client
        )
        results.append(result.as_dict())
        display(JSON(results[-1]))
finally:
    server.stop()
    if CONTROL_ROOT.exists():
        shutil.rmtree(CONTROL_ROOT)
    if OUTPUT_ROOT.exists():
        batch_summary = {
            "task_ids": list(RUN_TASK_IDS),
            "benchmark_task_ids": list(TASK_IDS),
            "results": results,
            "completed": len(results) == len(RUN_TASK_IDS) and all(row["completed"] for row in results),
            "skipped": [task for task in RUN_TASK_IDS if task not in {row["task_id"] for row in results}],
            "outcomes": {
                outcome: sum(row["outcome"] == outcome for row in results)
                for outcome in ("PASS", "FAIL", "INCONCLUSIVE", "INFRA_ERROR")
            },
            "server": server_profile,
        }
        (OUTPUT_ROOT / "batch_summary.json").write_text(json.dumps(batch_summary, indent=2), encoding="utf-8")
        zip_directory(OUTPUT_ROOT, DIAGNOSTICS_ZIP)
print(f"Submission: {SUBMISSION_ZIP}")
print(f"Diagnostics: {DIAGNOSTICS_ZIP}")
if not results or not all(row["completed"] for row in results):
    raise RuntimeError("One or more traced task executions did not complete; inspect trace_diagnostics.zip")
''')
    markdown("""## Outputs

- `/kaggle/working/submission.zip`: the configured competition package.
- `/kaggle/working/trace_diagnostics.zip`: all sealed runs, canonical events, artifacts, verification results, transcripts, offline timelines, server log, and batch summary.

The private test patch is deleted from the controller directory after verification and is never copied into the solver bundle.
""")
    for index, cell in enumerate(cells):
        cell["id"] = f"trace-{index:02d}"
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
            "source_sha256": {
                name: hashlib.sha256(value.encode()).hexdigest() for name, value in sources.items()
            },
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    notebook["metadata"]["interactive"] = interactive
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(notebook, indent=1, ensure_ascii=False), encoding="utf-8")
    return output


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--extra-tasks", action="store_true")
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--submission-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--supplemental-wheels", action="store_true")
    args = parser.parse_args()
    print(build(core_only=not args.extra_tasks, submission_dir=args.submission_dir,
                output=args.output, interactive=args.interactive,
                supplemental_wheels=args.supplemental_wheels))
