"""Build the self-contained seven-task v3 notebook from source and submission files."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT / "notebooks" / "gemma_agent_v3.ipynb"


def build() -> Path:
    files = {path.relative_to(PROJECT / "submission_v3").as_posix(): path.read_text()
             for path in sorted((PROJECT / "submission_v3").rglob("*"))
             if path.is_file() and not any(part.startswith(".") for part in path.relative_to(PROJECT / "submission_v3").parts)}
    shared = (PROJECT / "mvp_v2/harness.py").read_text()
    runtime = (PROJECT / "mvp_v2/v3.py").read_text().replace("from .harness import *  # EMBED_SHARED_HARNESS", "")
    cells = []
    def markdown(value):
        cells.append({"cell_type": "markdown", "metadata": {}, "source": value.splitlines(keepends=True)})
    def code(value):
        ast.parse(value)
        cells.append({"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                      "source": value.splitlines(keepends=True)})
    markdown("""# Gemma developer agent — one-shot v3

One frozen submission, seven tasks, one agent attempt each. Four L4 GPUs serve the attached Gemma checkpoint offline. Gold tests are used only after the agent stops, in separate fresh verification and baseline environments.

`submission.zip` contains the exact declarative files exercised here. Task failures are benchmark results; infrastructure failures and incomplete audits fail the execution gate. This notebook does not submit to the competition.
""")
    code('''from __future__ import annotations
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from IPython.display import JSON, display

TASK_IDS = (
    "fastapi_14492", "fastapi_14258", "rich_4076", "rich_3468",
    "requests_7205", "requests_6629", "httpx_3672",
)
MODEL_ROOT = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
DATA_ROOT = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WORK_ROOT = Path("/kaggle/working")
OUTPUT_ROOT = WORK_ROOT / "v3_diagnostics"
SUBMISSION_ZIP = WORK_ROOT / "submission.zip"
DIAGNOSTICS_ZIP = WORK_ROOT / "v3_diagnostics.zip"
VLLM_VERSION = "0.29.0"
MAX_MODEL_LEN = 32768
if OUTPUT_ROOT.exists():
    raise FileExistsError("v3_diagnostics already exists; preserve it before starting another attempt")
OUTPUT_ROOT.mkdir(parents=True)
print("v3 configured. Agent settings come exclusively from the embedded submission files.")
''')
    markdown("## Install the proven offline serving environment before importing Torch")
    code('''install_started = time.monotonic()
wheel_candidates = [Path("/kaggle/input/datasets/nguyenkhaho/vllm-wheel/wheels"), Path("/kaggle/input/vllm-wheel/wheels")]
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
install_log = OUTPUT_ROOT / "vllm_install.log"
if installed != VLLM_VERSION:
    with install_log.open("w") as handle:
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade",
                                 "--no-index", f"--find-links={wheel_root}", f"vllm=={VLLM_VERSION}"],
                                stdout=handle, stderr=subprocess.STDOUT, timeout=900)
    print(install_log.read_text()[-4000:])
    if result.returncode:
        raise RuntimeError("Offline vLLM installation failed; see vllm_install.log")
else:
    install_log.write_text(f"vllm=={installed} already installed\\n")
if importlib.metadata.version("vllm") != VLLM_VERSION:
    raise RuntimeError("Unexpected vLLM version")
print(f"vLLM environment ready in {time.monotonic()-install_started:.1f}s")
''')
    markdown("## Embedded execution and verification code")
    code(shared)
    code(runtime)
    markdown("## Validate and freeze the configuration")
    code("SUBMISSION_FILES = " + json.dumps(files, indent=2, ensure_ascii=False) + "\n" + '''
config = load_submission(SUBMISSION_FILES)
submission_manifest = build_submission_zip(SUBMISSION_FILES, SUBMISSION_ZIP)
with zipfile.ZipFile(SUBMISSION_ZIP) as archive:
    exercised_files = {name: archive.read(name).decode("utf-8") for name in archive.namelist()}
if exercised_files != SUBMISSION_FILES:
    raise RuntimeError("Packaged and exercised files differ")
config = load_submission(exercised_files)
(OUTPUT_ROOT / "effective_config.json").write_text(json.dumps(config, indent=2))
gpus = gpu_inventory()
if len(gpus) != 4 or not all("L4" in row["name"] for row in gpus):
    raise RuntimeError(f"Expected four L4 GPUs, got {gpus}")
config_files = sorted(MODEL_ROOT.rglob("config.json"))
if not config_files:
    raise FileNotFoundError(f"Model config missing under {MODEL_ROOT}")
MODEL_PATH = max(config_files, key=lambda path: sum(child.suffix == ".safetensors" for child in path.parent.iterdir())).parent
model_config = json.loads((MODEL_PATH / "config.json").read_text())
if "gemma" not in str(model_config.get("model_type", "")).lower():
    raise RuntimeError("Attached checkpoint is not a Gemma model")
tasks = {}
with (DATA_ROOT / "tasks.jsonl").open() as handle:
    for line in handle:
        row = json.loads(line)
        if row.get("instance_id") in TASK_IDS:
            tasks[row["instance_id"]] = row
if set(tasks) != set(TASK_IDS):
    raise ValueError(f"Missing tasks: {set(TASK_IDS)-set(tasks)}")
for task_id in TASK_IDS:
    if not (DATA_ROOT / "snapshots" / f"{task_id}.tgz").is_file():
        raise FileNotFoundError(task_id)
environment = {
    "created_utc": utc_now(), "python": sys.version, "gpu": gpus,
    "model_id": config["model"], "model_path": str(MODEL_PATH),
    "model_config_sha256": hashlib.sha256((MODEL_PATH / "config.json").read_bytes()).hexdigest(),
    "model_size_bytes": sum(path.stat().st_size for path in MODEL_PATH.rglob("*.safetensors")),
    "packages": sorted({dist.metadata["Name"]: dist.version for dist in importlib.metadata.distributions() if dist.metadata["Name"]}.items()),
    "submission": submission_manifest, "task_ids": TASK_IDS,
    "fidelity": {
        "runtime": "Documented competition API emulation; official compiler is not installed",
        "context": "Token-counted preflight compaction with last two exchanges and bounded factual digest; differs from ADK compaction",
        "isolation": "Separate repositories and venvs with inherited system packages and command guards; not OS containers",
        "python": "Kaggle Python 3.12; competition sandbox documentation specifies Python 3.13",
        "test_plugins": "Global plugin auto-loading disabled; baseline distinguishes environment problems",
        "test_runtime_override": "pytest==8.3.4 overrides historical pytest development pins incompatible with the Kaggle image",
    },
}
(OUTPUT_ROOT / "environment.json").write_text(json.dumps(environment, indent=2))
display(JSON({"configuration": config, "gpu": gpus, "submission": submission_manifest}))
''')
    markdown("## Launch Gemma, preflight tools, and run the fixed seven-task benchmark")
    code('''server = VLLMServer(model_path=MODEL_PATH, model_name=config["model"],
                    log_path=OUTPUT_ROOT / "server.log", max_model_len=MAX_MODEL_LEN)
summaries, audits = [], []
profile = None
run_error = None
preflight = None
run_started = time.monotonic()
def server_status(status):
    print(f"Loading model: {status['elapsed_seconds']:.0f}s | {status['last_log']} | GPU: {status['gpu']}", flush=True)
try:
    profile = server.start(status_callback=server_status)
    print(f"vLLM ready in {profile['startup_seconds']:.1f}s", flush=True)
    client = server.client
    environment["tokenizer"] = client.configure_tokenizer(MODEL_PATH, config["tools"])
    environment["server"] = profile
    (OUTPUT_ROOT / "environment.json").write_text(json.dumps(environment, indent=2))
    preflight = synthetic_preflight(client, config, OUTPUT_ROOT)
    summaries, audits = benchmark_v3(client=client, tasks=tasks, task_ids=TASK_IDS, config=config,
                                    data_root=DATA_ROOT, output_root=OUTPUT_ROOT,
                                    work_root=WORK_ROOT / "v3_workspaces", max_model_len=MAX_MODEL_LEN)
except Exception as exc:
    run_error = f"{type(exc).__name__}: {exc}"
    print(run_error, flush=True)
finally:
    server.stop()
    # Recover incremental results if an outer operation failed after completed tasks.
    results_path = OUTPUT_ROOT / "task_results.jsonl"
    if not summaries and results_path.exists():
        summaries = [json.loads(line) for line in results_path.read_text().splitlines() if line]
    audits = [json.loads(path.read_text()) for path in sorted((OUTPUT_ROOT / "tasks").glob("*/leak_audit.json"))]
    for task_id in TASK_IDS:
        if not any(row["instance_id"] == task_id for row in summaries):
            summaries.append({"instance_id": task_id, "status": "INFRA_ERROR", "resolved": False,
                              "agent_executed": False, "error": run_error or "task_not_executed"})
    counts = {status: sum(row["status"] == status for row in summaries)
              for status in ("PASS", "FAIL", "INFRA_ERROR", "INCONCLUSIVE")}
    audit_coverage = {row["instance_id"] for row in audits} == set(TASK_IDS)
    archive_unchanged = hashlib.sha256(SUBMISSION_ZIP.read_bytes()).hexdigest() == submission_manifest["sha256"]
    run_completed = bool(not run_error and preflight and preflight["passed"]
                         and all(row.get("agent_executed") for row in summaries)
                         and not counts["INFRA_ERROR"] and audit_coverage
                         and all(row["passed"] for row in audits) and archive_unchanged)
    measured = [row["setup_seconds"] + row["budget"]["agent_elapsed_seconds"]
                for row in summaries if "setup_seconds" in row and "budget" in row]
    projection = (sum(measured) / len(measured) * 120 + (profile or {}).get("startup_seconds", 0)) / 3600 if measured else None
    run_summary = {
        "created_utc": utc_now(), "run_completed": run_completed, "run_error": run_error,
        "benchmark": {"task_count": len(TASK_IDS), "counts": counts,
                      "resolution_rate": counts["PASS"] / len(TASK_IDS), "task_ids": TASK_IDS},
        "audit_coverage_complete": audit_coverage, "archive_unchanged": archive_unchanged,
        "projected_120_task_inference_hours": projection,
        "projection_note": "Observed setup + agent time, plus server startup; excludes verification. Seven tasks are not a representative timing guarantee.",
        "elapsed_seconds": time.monotonic() - run_started,
        "submission": submission_manifest, "server": profile, "tasks": summaries,
    }
    (OUTPUT_ROOT / "summary.json").write_text(json.dumps(run_summary, indent=2))
    (OUTPUT_ROOT / "leak_audit.json").write_text(json.dumps(audits, indent=2))
    zip_directory(OUTPUT_ROOT, DIAGNOSTICS_ZIP)
    display(JSON(run_summary))
    print(f"Submission: {SUBMISSION_ZIP}\\nDiagnostics: {DIAGNOSTICS_ZIP}", flush=True)
if not run_completed:
    raise RuntimeError("v3 execution gate failed; diagnostics and partial results are preserved")
''')
    markdown("""## Outputs

- `/kaggle/working/submission.zip`: frozen declarative configuration; no model, adapters, task data, or diagnostics.
- `/kaggle/working/v3_diagnostics.zip`: all requests, trajectories, complete logs, exact patches, patched/baseline test results, audits, environment, and summary.

A notebook completion is distinct from the number of tasks solved. A verified PASS requires patched tests to pass and the fresh baseline to fail with a test failure. This development runner is an approximation of the documented competition runtime, not an execution of its private compiler.
""")
    for index, cell in enumerate(cells):
        cell["id"] = f"gemma-v3-{index:02d}"
    notebook = {"cells": cells, "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.12"},
        "source_sha256": {"harness": hashlib.sha256(shared.encode()).hexdigest(),
                          "v3": hashlib.sha256(runtime.encode()).hexdigest()},
    }, "nbformat": 4, "nbformat_minor": 5}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n")
    return OUTPUT


if __name__ == "__main__":
    print(build())
