from __future__ import annotations

import json
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT / "notebooks" / "gemma_agent_mvp_v2.ipynb"
HARNESS = PROJECT / "mvp_v2" / "harness.py"
SUBMISSION_ROOT = PROJECT / "submission_v2"
SUBMISSION_PATHS = (
    "agent.yaml",
    "eval_config.yaml",
    "configs/sampling.yaml",
    "prompts/system.md",
)


def source_lines(value: str) -> list[str]:
    return value.splitlines(keepends=True)


def markdown(value: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": source_lines(value)}


def code(value: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source_lines(value),
    }


def build() -> Path:
    submission_files = {
        relative: (SUBMISSION_ROOT / relative).read_text(encoding="utf-8")
        for relative in SUBMISSION_PATHS
    }
    embedded_submission = json.dumps(submission_files, indent=2, ensure_ascii=False)
    harness_source = HARNESS.read_text(encoding="utf-8")

    cells = [
        markdown(
            """# Gemma Developer Agent — MVP v2 development harness

This notebook serves the attached Gemma 4 31B W4A16 model on **four L4 GPUs**, runs a single-agent tool loop against a seven-task mixed public benchmark, verifies each patch in a fresh workspace, and creates `/kaggle/working/submission.zip`.

Acceptance gate:

1. `fastapi_14492` must issue a valid tool call, submit a nonempty patch, and pass its gold test.
2. All seven tasks must complete with a recorded PASS or FAIL.

Individual benchmark failures are results, not notebook failures. Temporary task workspaces are removed after each result is preserved.

The reference fix and gold tests are never exposed to the model. No LoRA is trained or packaged, and this notebook never submits anything to the competition.
"""
        ),
        code(
            """from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from IPython.display import JSON, Markdown, display

TASK_IDS = (
    "fastapi_14492",
    "fastapi_14258",
    "rich_4076",
    "rich_3468",
    "requests_7205",
    "requests_6629",
    "httpx_3672",
)
CANARY_TASK_ID = TASK_IDS[0]
MODEL_ID = "gemma-4-31b-it-qat-w4a16-ct"
MODEL_ROOT = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
DATA_ROOT = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WORK_ROOT = Path("/kaggle/working")
OUTPUT_ROOT = WORK_ROOT / "mvp_v2_diagnostics"
WARM_ROOT = WORK_ROOT / "mvp_v2_warm"
SUBMISSION_ZIP = WORK_ROOT / "submission.zip"
DIAGNOSTICS_ZIP = WORK_ROOT / "mvp_v2_diagnostics.zip"

VLLM_VERSION = "0.29.0"
MAX_MODEL_LEN = 32768
MAX_TOOL_CALLS = 18
MAX_TIME_MINUTES = 3.0
MAX_TURNS = 50
COMMAND_TIMEOUT_SECONDS = 60
THINKING_BUDGET = 2048
MAX_OUTPUT_TOKENS = 8192

if OUTPUT_ROOT.exists():
    shutil.rmtree(OUTPUT_ROOT)
OUTPUT_ROOT.mkdir(parents=True)
WARM_ROOT.mkdir(parents=True, exist_ok=True)
print("Configuration loaded. This notebook does not submit to the competition.")
"""
        ),
        markdown("## 1. Install the pinned offline model server"),
        code(
            """install_started = time.monotonic()
try:
    installed_vllm = importlib.metadata.version("vllm")
except importlib.metadata.PackageNotFoundError:
    installed_vllm = None

wheel_candidates = [
    Path("/kaggle/input/datasets/nguyenkhaho/vllm-wheel/wheels"),
    Path("/kaggle/input/vllm-wheel/wheels"),
]
wheel_root = next((path for path in wheel_candidates if path.is_dir()), None)
if wheel_root is None:
    matches = list(Path("/kaggle/input").glob("**/vllm-0.29.0-*.whl"))
    wheel_root = matches[0].parent if matches else None
if wheel_root is None:
    raise FileNotFoundError("The attached nguyenkhaho/vllm-wheel dataset was not found")

install_log = OUTPUT_ROOT / "vllm_install.log"
if installed_vllm != VLLM_VERSION:
    command = [
        sys.executable, "-m", "pip", "install", "--no-cache-dir", "--upgrade",
        "--no-index", f"--find-links={wheel_root}",
        f"vllm=={VLLM_VERSION}",
    ]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    install_log.write_text(result.stdout, encoding="utf-8")
    print(result.stdout[-4000:])
    if result.returncode != 0:
        raise RuntimeError(f"vLLM installation failed; see {install_log}")
else:
    install_log.write_text(f"vllm=={installed_vllm} already installed\\n", encoding="utf-8")

resolved_vllm = importlib.metadata.version("vllm")
if resolved_vllm != VLLM_VERSION:
    raise RuntimeError(f"Expected vllm=={VLLM_VERSION}, resolved {resolved_vllm}")
print(
    f"vllm=={resolved_vllm} ready from {wheel_root} "
    f"in {time.monotonic() - install_started:.1f}s"
)
"""
        ),
        markdown("## 2. Development harness"),
        code(harness_source),
        markdown("## 3. Validate inputs and build the clean submission"),
        code(
            f"""SUBMISSION_FILES = {embedded_submission}

gpus = gpu_inventory()
if len(gpus) != 4 or not all("L4" in row["name"] for row in gpus):
    raise RuntimeError(f"Expected four NVIDIA L4 GPUs, found: {{gpus}}")
if not DATA_ROOT.is_dir():
    raise FileNotFoundError(DATA_ROOT)
if not MODEL_ROOT.is_dir():
    raise FileNotFoundError(MODEL_ROOT)

config_files = sorted(MODEL_ROOT.rglob("config.json"))
if not config_files:
    raise FileNotFoundError(f"No model config below {{MODEL_ROOT}}")
MODEL_PATH = max(config_files, key=lambda path: sum(
    child.suffix in {{".json", ".safetensors"}} for child in path.parent.iterdir() if child.is_file()
)).parent

tasks = {{}}
with (DATA_ROOT / "tasks.jsonl").open(encoding="utf-8") as handle:
    for line in handle:
        task = json.loads(line)
        if task.get("instance_id") in TASK_IDS:
            tasks[task["instance_id"]] = task
if tuple(task_id for task_id in TASK_IDS if task_id in tasks) != TASK_IDS:
    raise ValueError(f"Missing requested tasks: {{set(TASK_IDS) - set(tasks)}}")

for task_id in TASK_IDS:
    for asset in (
        DATA_ROOT / "snapshots" / f"{{task_id}}.tgz",
        DATA_ROOT / "graphs" / f"{{task_id}}.json",
        DATA_ROOT / "embeddings" / f"{{task_id}}.npz",
    ):
        if not asset.exists():
            raise FileNotFoundError(asset)

submission_manifest = build_submission_zip(SUBMISSION_FILES, SUBMISSION_ZIP)
environment = {{
    "created_utc": utc_now(),
    "python": sys.version,
    "vllm": resolved_vllm,
    "vllm_wheel_root": str(wheel_root),
    "gpu": gpus,
    "model_id": MODEL_ID,
    "model_path": str(MODEL_PATH),
    "model_files": len([path for path in MODEL_PATH.rglob("*") if path.is_file()]),
    "model_size_bytes": sum(path.stat().st_size for path in MODEL_PATH.rglob("*") if path.is_file()),
    "data_root": str(DATA_ROOT),
    "task_ids": TASK_IDS,
    "budgets": {{
        "max_tool_calls": MAX_TOOL_CALLS,
        "max_time_minutes": MAX_TIME_MINUTES,
        "max_turns": MAX_TURNS,
        "command_timeout_seconds": COMMAND_TIMEOUT_SECONDS,
        "thinking_budget": THINKING_BUDGET,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }},
    "submission": submission_manifest,
    "vllm_server_environment": {{
        "VLLM_USE_FLASHINFER_SAMPLER": "0",
        "VLLM_NO_USAGE_STATS": "1",
    }},
}}
(OUTPUT_ROOT / "environment.json").write_text(json.dumps(environment, indent=2), encoding="utf-8")
display(JSON(environment))
"""
        ),
        markdown("## 4. Reuse or start Gemma, then run the public tasks"),
        code(
            """server = VLLMServer(
    model_path=MODEL_PATH,
    model_name=MODEL_ID,
    log_path=WARM_ROOT / "server.log",
    max_model_len=MAX_MODEL_LEN,
)
task_summaries = []
leak_audits = []
server_profile = None
run_error = None

def server_status(status):
    print(
        f"server loading: {status['elapsed_seconds']:.0f}s | "
        f"{status['last_log']} | gpu={status['gpu']}"
    )

try:
    server_profile = server.ensure_started(status_callback=server_status)
    if server_profile["reused"]:
        print("Reusing the healthy vLLM server already loaded in this session")
    else:
        print(f"vLLM ready in {server_profile['startup_seconds']:.1f}s")

    for task_id in TASK_IDS:
        task_started = time.monotonic()
        task = tasks[task_id]
        task_root = OUTPUT_ROOT / "tasks" / task_id
        task_root.mkdir(parents=True, exist_ok=True)
        logger = EventLogger(task_root)
        agent_workspace = WORK_ROOT / "mvp_v2_workspaces" / task_id / "agent"
        verification_workspace = WORK_ROOT / "mvp_v2_workspaces" / task_id / "verification"
        task_venv = WORK_ROOT / "mvp_v2_workspaces" / task_id / "venv"
        patch_path = OUTPUT_ROOT / "patches" / f"{task_id}.patch"
        test_log = OUTPUT_ROOT / "test_outputs" / f"{task_id}.log"
        patch_path.parent.mkdir(exist_ok=True)
        test_log.parent.mkdir(exist_ok=True)

        summary = {
            "instance_id": task_id,
            "repo": task["repo"],
            "status": "FAIL",
            "resolved": False,
        }
        try:
            safe_extract(DATA_ROOT / "snapshots" / f"{task_id}.tgz", agent_workspace)
            configure_workspace(agent_workspace)
            task_python = create_task_venv(task_venv, agent_workspace, DATA_ROOT / "wheels")
            budget = Budget(
                max_tool_calls=MAX_TOOL_CALLS,
                max_time_minutes=MAX_TIME_MINUTES,
                max_turns=MAX_TURNS,
                command_timeout_seconds=COMMAND_TIMEOUT_SECONDS,
            )
            tools = WorkspaceTools(
                agent_workspace,
                budget,
                logger,
                python=task_python,
                graph_path=DATA_ROOT / "graphs" / f"{task_id}.json",
                embeddings_path=DATA_ROOT / "embeddings" / f"{task_id}.npz",
            )
            agent_result = run_agent(
                server.client,
                task,
                SUBMISSION_FILES["prompts/system.md"],
                tools,
                logger,
                max_tokens=MAX_OUTPUT_TOKENS,
                thinking_budget=THINKING_BUDGET,
            )
            patch_path.write_text(agent_result["patch"], encoding="utf-8")
            (task_root / "transcript.md").write_text(agent_result["transcript"], encoding="utf-8")
            leak_audit = write_leak_audit(
                task_root / "leak_audit.json", task=task, agent_result=agent_result
            )
            leak_audits.append(leak_audit)

            verification = verify_patch(
                snapshot=DATA_ROOT / "snapshots" / f"{task_id}.tgz",
                workspace=verification_workspace,
                agent_patch=agent_result["patch"],
                test_patch=task["test_patch"],
                python=task_python,
                output_log=test_log,
                wheels_dir=DATA_ROOT / "wheels",
            ) if agent_result["patch"].strip() else {
                "resolved": False, "error": "empty_agent_patch",
            }
            summary.update({
                "status": "PASS" if verification["resolved"] else "FAIL",
                "resolved": verification["resolved"],
                "termination_reason": agent_result["termination_reason"],
                "valid_tool_calls": agent_result["budget"]["tool_calls_used"],
                "patch_size": len(agent_result["patch"]),
                "patch_sha256": sha256_text(agent_result["patch"]),
                "model_calls": agent_result["model_calls"],
                "prompt_tokens_total": agent_result["prompt_tokens_total"],
                "prompt_tokens_max": agent_result["prompt_tokens_max"],
                "completion_tokens_total": agent_result["completion_tokens_total"],
                "context_compactions": agent_result["context_compactions"],
                "budget": agent_result["budget"],
                "verification": verification,
                "leak_audit_passed": leak_audit["passed"],
            })
        except Exception as exc:
            summary.update({"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"})
            logger.emit("task_exception", error=summary["error"])
        dependency_setup_path = task_venv.parent / "task_dependency_setup.json"
        if dependency_setup_path.exists():
            shutil.copy2(dependency_setup_path, task_root / "dependency_setup.json")
        summary["elapsed_seconds"] = round(time.monotonic() - task_started, 3)
        task_summaries.append(summary)
        (task_root / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        print(
            f"{task_id}: {summary['status']} resolved={summary['resolved']} "
            f"calls={summary.get('valid_tool_calls', 0)} patch={summary.get('patch_size', 0)} bytes"
        )
        task_work_root = task_venv.parent
        if task_work_root.exists():
            shutil.rmtree(task_work_root)
except Exception as exc:
    run_error = f"{type(exc).__name__}: {exc}"
    print(f"Harness-level failure: {run_error}")

for task_id in TASK_IDS:
    if not any(row.get("instance_id") == task_id for row in task_summaries):
        task_summaries.append({
            "instance_id": task_id,
            "repo": tasks[task_id]["repo"],
            "status": "FAIL",
            "resolved": False,
            "error": run_error or "task_not_executed",
        })

canary = next(row for row in task_summaries if row["instance_id"] == CANARY_TASK_ID)
all_tasks_recorded = all(
    any(row.get("instance_id") == task_id for row in task_summaries)
    for task_id in TASK_IDS
)
pass_count = sum(bool(row.get("resolved")) for row in task_summaries)
fail_count = len(task_summaries) - pass_count
accepted = bool(
    canary.get("resolved")
    and canary.get("valid_tool_calls", 0) > 0
    and canary.get("patch_size", 0) > 0
    and all_tasks_recorded
    and SUBMISSION_ZIP.is_file()
    and all(audit.get("passed") for audit in leak_audits)
)

run_summary = {
    "created_utc": utc_now(),
    "accepted": accepted,
    "run_error": run_error,
    "acceptance": {
        "canary_resolved": bool(canary.get("resolved")),
        "canary_used_tools": canary.get("valid_tool_calls", 0) > 0,
        "canary_nonempty_patch": canary.get("patch_size", 0) > 0,
        "all_tasks_recorded": all_tasks_recorded,
        "submission_zip_valid": SUBMISSION_ZIP.is_file(),
        "leak_audits_passed": bool(leak_audits) and all(audit.get("passed") for audit in leak_audits),
    },
    "benchmark": {
        "task_count": len(task_summaries),
        "pass_count": pass_count,
        "fail_count": fail_count,
        "resolution_rate": round(pass_count / len(task_summaries), 4) if task_summaries else 0.0,
        "task_ids": list(TASK_IDS),
    },
    "server": server_profile,
    "submission": submission_manifest,
    "tasks": task_summaries,
}
(OUTPUT_ROOT / "summary.json").write_text(json.dumps(run_summary, indent=2, default=str), encoding="utf-8")
(OUTPUT_ROOT / "leak_audit.json").write_text(json.dumps(leak_audits, indent=2), encoding="utf-8")
warm_server_log = WARM_ROOT / "server.log"
if warm_server_log.exists():
    shutil.copy2(warm_server_log, OUTPUT_ROOT / "server.log")
zip_directory(OUTPUT_ROOT, DIAGNOSTICS_ZIP)
# Keep Kaggle's saved outputs compact. The exact patches, transcripts, tests,
# and logs are already preserved in the diagnostics bundle.
workspaces_root = WORK_ROOT / "mvp_v2_workspaces"
if workspaces_root.exists():
    shutil.rmtree(workspaces_root)
display(JSON(run_summary))
print(f"submission: {SUBMISSION_ZIP}")
print(f"diagnostics: {DIAGNOSTICS_ZIP}")

if not accepted:
    raise RuntimeError("Mixed benchmark infrastructure gate failed; inspect the diagnostics bundle.")
"""
        ),
        markdown(
            """## Outputs

When the infrastructure gate passes:

- `/kaggle/working/submission.zip` is the clean prompt-only competition artifact.
- `/kaggle/working/mvp_v2_diagnostics.zip` contains the environment, server log, trajectories, patches, leak audits, and fresh-workspace test output.
- `/kaggle/working/mvp_v2_warm/server.log` belongs to the session-level vLLM process and survives another notebook run in the same session.

The notebook deliberately leaves the healthy vLLM process running for another interactive iteration and performs no competition submission.
"""
        ),
    ]

    for index, cell in enumerate(cells):
        cell["id"] = f"mvp-v2-{index:02d}"

    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(notebook, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(build())
