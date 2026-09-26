"""Build notebooks/multi_experiment.ipynb: several agent harnesses on the same task(s), one model server.

Experimental only; it writes no competition submission. Each harness runs through the same
traced runtime as the trace notebook, so results are directly comparable.
"""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import sys
import zlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "experiments" / "multi"))
sys.path.insert(0, str(PROJECT / "tools"))
from assemble import CANDIDATES, assemble_all, describe  # noqa: E402
from build_trace_notebook import _runtime_sources  # noqa: E402

OUTPUT = PROJECT / "notebooks" / "multi_experiment.ipynb"
TASK_IDS = ("rich_4076",)
RUN_ORDER = tuple(CANDIDATES)  # candidates A_..F_ then the baseline_v22 control
KAGGLE_SOURCE_LIMIT = 1_000_000


def build(output: Path = OUTPUT, task_ids: tuple[str, ...] = TASK_IDS) -> Path:
    sources = _runtime_sources()
    submissions = assemble_all()
    cells = []

    def markdown(value: str) -> None:
        cells.append({"cell_type": "markdown", "metadata": {}, "source": value.splitlines(keepends=True)})

    def code(value: str) -> None:
        ast.parse(value)
        cells.append({"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                      "source": value.splitlines(keepends=True)})

    harness_rows = "\n".join(f"| `{name}` | {' | '.join(describe(name))} |" for name in RUN_ORDER)
    markdown(f"""# Multi-harness experiment

Runs {len(RUN_ORDER)} agent harnesses on {', '.join(task_ids)} with one shared Gemma 4 server and the traced runtime:

| Harness | Design | Basis |
|---|---|---|
{harness_rows}

Every harness gets the official 5-minute budget. Nothing is submitted to the competition.
Requires: competition `gemma-4-developer-agent`, model `gemma-4-31b-it-qat-w4a16-ct/2`, dataset `nguyenkhaho/vllm-wheel`, 4×L4 GPU, internet off.
""")
    code('''from __future__ import annotations
import importlib.metadata
import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from IPython.display import JSON, Markdown, display

TASK_IDS = __TASK_IDS__
RUN_ORDER = __RUN_ORDER__
MODEL_ROOT = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
DATA_ROOT = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WORK_ROOT = Path("/kaggle/working")
RUNTIME_ROOT = WORK_ROOT / "multi_runtime"
OUTPUT_ROOT = WORK_ROOT / "multi_runs"
CONTROL_ROOT = WORK_ROOT / "multi_private_control"
DIAGNOSTICS_ZIP = WORK_ROOT / "multi_experiment_diagnostics.zip"
VLLM_VERSION = "0.29.0"
MAX_MODEL_LEN = 32768
print("Harnesses:", RUN_ORDER, "| tasks:", TASK_IDS)
'''.replace("__TASK_IDS__", repr(task_ids)).replace("__RUN_ORDER__", repr(RUN_ORDER)))
    markdown("## Install the offline vLLM wheel")
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
    print(result.stdout[-3000:])
    if result.returncode:
        raise RuntimeError("Offline vLLM installation failed")
print("vLLM", importlib.metadata.version("vllm"))
''')
    markdown("## Materialize the runtime and the harness submissions")
    # zlib+base64 keeps 7 harnesses (plus the runtime) well under Kaggle's 1 MB source limit.
    payload = base64.b64encode(zlib.compress(json.dumps(
        {"runtime": sources, "submissions": submissions}, ensure_ascii=False).encode(), 9)).decode()
    code("import base64, zlib\n_PAYLOAD = json.loads(zlib.decompress(base64.b64decode(\n    "
         + json.dumps(payload) + ")))\n"
         + "RUNTIME_SOURCES, SUBMISSIONS = _PAYLOAD[\"runtime\"], _PAYLOAD[\"submissions\"]\n" + '''
if RUNTIME_ROOT.exists():
    shutil.rmtree(RUNTIME_ROOT)
for relative, content in RUNTIME_SOURCES.items():
    destination = RUNTIME_ROOT / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
# An interactive session may still hold another notebook's runtime (e.g. traced_agent_runtime);
# drop those cached modules and paths so this experiment's workflow-capable runtime is imported.
for module_name in [m for m in sys.modules if m.split(".")[0] in {"agent_harness", "mvp_v2"}]:
    del sys.modules[module_name]
sys.path[:] = [p for p in sys.path if not (Path(p) / "mvp_v2").is_dir() or Path(p) == RUNTIME_ROOT]
sys.path.insert(0, str(RUNTIME_ROOT))
import mvp_v2.v3
if Path(mvp_v2.v3.__file__).resolve() != (RUNTIME_ROOT / "mvp_v2" / "v3.py").resolve():
    raise RuntimeError(f"Imported the wrong runtime: {mvp_v2.v3.__file__}")
from agent_harness import RunConfig, TaskSpec, VerificationSpec, run_task
from mvp_v2.harness import VLLMServer, parse_patch_targets, zip_directory
from mvp_v2.v3 import load_submission
print("Runtime:", mvp_v2.v3.__file__)

submission_dirs, loaded = {}, {}
for name in RUN_ORDER:
    root = RUNTIME_ROOT / "submissions" / name
    for relative, content in SUBMISSIONS[name].items():
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
    loaded[name] = load_submission(SUBMISSIONS[name])
    submission_dirs[name] = root

def describe(node, depth=0):
    if node["kind"] == "LlmAgent":
        return ["  " * depth + f"- {node['name']} (tools: {', '.join(node['declared_tools'])}; skills: {', '.join(node['skills']) or '-'})"]
    rows = ["  " * depth + f"+ {node['kind']} {node['name']}"]
    for child in node["sub_agents"]:
        rows += describe(child, depth + 1)
    return rows

for name, config in loaded.items():
    tree = config.get("tree") or {**config, "kind": "LlmAgent"}
    print(name, "\\n" + "\\n".join(describe(tree)))
''')
    markdown("## Prepare the task(s) and private verification")
    code('''WHEELS_ROOT = WORK_ROOT / "multi_test_wheels"
WHEELS_ROOT.mkdir(exist_ok=True)
for source in (DATA_ROOT / "wheels").glob("*.whl"):
    destination = WHEELS_ROOT / source.name
    if not destination.exists():
        destination.symlink_to(source)
task_rows = {}
with (DATA_ROOT / "tasks.jsonl").open(encoding="utf-8") as handle:
    for line in handle:
        row = json.loads(line)
        if row.get("instance_id") in TASK_IDS:
            task_rows[row["instance_id"]] = row
missing = set(TASK_IDS) - set(task_rows)
if missing:
    raise KeyError(f"Tasks not found: {sorted(missing)}")
config_files = sorted(MODEL_ROOT.rglob("config.json"))
MODEL_PATH = max(config_files, key=lambda path: sum(child.suffix == ".safetensors" for child in path.parent.iterdir())).parent
for stale in (OUTPUT_ROOT, CONTROL_ROOT):
    if stale.exists():
        shutil.rmtree(stale)
OUTPUT_ROOT.mkdir(parents=True)
CONTROL_ROOT.mkdir(parents=True)
task_specs, verification_specs, gold_files = {}, {}, {}
for task_id in TASK_IDS:
    row = task_rows[task_id]
    private_test_patch = CONTROL_ROOT / f"{task_id}.patch"
    private_test_patch.write_text(row["test_patch"], encoding="utf-8")
    targets = parse_patch_targets(row["test_patch"])
    # Controller-side only, for the localization metric; never shown to an agent.
    gold_files[task_id] = parse_patch_targets(row["patch"])
    graph = DATA_ROOT / "graphs" / f"{task_id}.json"
    embeddings = DATA_ROOT / "embeddings" / f"{task_id}.npz"
    task_specs[task_id] = TaskSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "repository": row["repo"],
        "problem_statement": row["problem_statement"], "hints": row.get("hints_text", ""),
        "snapshot_path": str(DATA_ROOT / "snapshots" / f"{task_id}.tgz"),
        **({"graph_path": str(graph)} if graph.is_file() else {}),
        **({"embeddings_path": str(embeddings)} if embeddings.is_file() else {}),
    })
    verification_specs[task_id] = VerificationSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "test_patch_path": str(private_test_patch),
        "verification_command": shlex.join([
            "python", "-m", "pytest", *targets, "-p", "no:anyio", "-o", "timeout=0",
            "-o", "norecursedirs=.* build dist venv", "-o", "python_classes=Test* *Test", "-q",
        ]),
        "timeout_seconds": 600,
    })
print(json.dumps({"tasks": list(TASK_IDS), "gold_files": gold_files}, indent=1))
''')
    markdown("## Start the model server (once)")
    code('''server = VLLMServer(model_path=MODEL_PATH, model_name="gemma-4-31b-it-qat-w4a16-ct",
                    log_path=OUTPUT_ROOT / "server.log", max_model_len=MAX_MODEL_LEN, startup_timeout=1800)
server_profile = server.start(status_callback=lambda status: print(
    f"Loading model: {status['elapsed_seconds']:.0f}s | {status['last_log'][:160]}", flush=True))
client = server.client
all_tools = sorted({tool for config in loaded.values() for tool in config["tools"]})
tokenizer_profile = client.configure_tokenizer(MODEL_PATH, all_tools)
print(tokenizer_profile)
''')
    markdown("""## Run every harness

Everything a harness creates under `/tmp` (reproductions, checkpoints, candidate checkouts) is removed before the next, so no harness sees another's files. Change `RUN_ORDER` here to rerun a subset.""")
    code('''import glob

# Anything a harness leaves in /tmp (reproductions, checkpoints, candidate checkouts, notes) is removed
# before the next harness starts, so no harness sees another's files.
TMP_BASELINE = set(glob.glob("/tmp/*"))

def clean_scratch():
    for path in set(glob.glob("/tmp/*")) - TMP_BASELINE:
        shutil.rmtree(path, ignore_errors=True) if Path(path).is_dir() and not Path(path).is_symlink() else Path(path).unlink(missing_ok=True)
    # swe_edit's guard (B_guarded) installs a repro hook into site-packages; it is inert once /tmp/swe_state
    # is gone, but remove it anyway so later harnesses start from an untouched Python.
    for python in {sys.executable, shutil.which("python3"), shutil.which("python")} - {None}:
        probe = subprocess.run([python, "-c", "import site; print(*site.getsitepackages())"],
                               capture_output=True, text=True)
        for directory in probe.stdout.split():
            Path(directory, "swe_guard.pth").unlink(missing_ok=True)

results = []
for task_id in TASK_IDS:
    for name in RUN_ORDER:
        clean_scratch()
        run_config = RunConfig.from_dict({
            "schema_version": "1.0", "submission_directory": str(submission_dirs[name]),
            "output_root": str(OUTPUT_ROOT / name), "work_root": str(WORK_ROOT / "multi_workspaces"),
            "experiment_name": f"multi-{name}", "seed": 42,
            "model": {"path": str(MODEL_PATH), "name": "gemma-4-31b-it-qat-w4a16-ct"},
            "server": {"max_model_len": MAX_MODEL_LEN, "startup_timeout": 1800},
            "wheels_directory": str(WHEELS_ROOT), "pytest_version": "8.3.4",
        })
        started = time.monotonic()
        print(f"=== {name} on {task_id}", flush=True)
        try:
            result = run_task(task_specs[task_id], verification_specs[task_id], run_config, client=client).as_dict()
        except Exception as exc:  # keep going: one broken harness must not hide the others
            result = {"task_id": task_id, "outcome": "INFRA_ERROR", "completed": False,
                      "termination_reason": f"notebook_error: {type(exc).__name__}: {exc}"}
        result["harness"] = name
        result["wall_seconds"] = round(time.monotonic() - started, 1)
        results.append(result)
        print(f"    {result['outcome']} ({result['termination_reason']}) in {result['wall_seconds']}s", flush=True)
clean_scratch()
''')
    markdown("## Compare the harnesses")
    code('''import collections
import re

def run_metrics(row):
    metrics = {"harness": row["harness"], "task": row["task_id"], "outcome": row["outcome"],
               "termination": row["termination_reason"], "wall_s": row["wall_seconds"]}
    directory = Path(row.get("run_directory", ""))
    events_path = directory / "events.jsonl"
    if not events_path.is_file():
        return metrics
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    calls = [e for e in events if e["event_type"] == "tool_result"]
    errors = collections.Counter(e["attributes"].get("error_type") for e in calls if e["status"] == "error")
    arg_errors = sum(v for k, v in errors.items() if k in {"ValidationError", "ToolArgumentsJSONError", "ValueError"})
    agents = collections.Counter(e["attributes"].get("agent") for e in events if e["event_type"] == "model_response")
    patch = Path(row.get("patch_path", "")).read_text() if Path(row.get("patch_path", "")).is_file() else ""
    patched_files = re.findall(r"^\\+\\+\\+ b/(\\S+)", patch, re.M)
    edited_ever = sorted({f for e in events if e["event_type"] == "tool_result" and e["attributes"].get("workspace_changed")
                          for f in re.findall(r"^\\+\\+\\+ b/(\\S+)", (directory / (e["attributes"].get("workspace_diff_path") or "x")).read_text()
                                              if (directory / (e["attributes"].get("workspace_diff_path") or "x")).is_file() else "", re.M)})
    skill_scripts = collections.Counter()
    for e in events:
        if e["event_type"] == "tool_call" and e["attributes"].get("tool") == "run_skill_script":
            for ref in e["artifact_refs"]:
                try:
                    args = json.loads((directory / ref["relative_path"]).read_text())
                    skill_scripts[str(args.get("file_path", "?")).split("/")[-1] + (":" + str((args.get("args") or {}).get("op")) if isinstance(args.get("args"), dict) and (args.get("args") or {}).get("op") else "")] += 1
                except Exception:
                    pass
    gold = set(gold_files.get(row["task_id"], []))
    verification_path = directory / "final" / "verification.json"
    target = (json.loads(verification_path.read_text()).get("target_tests") or {}) if verification_path.is_file() else {}
    submitted = any(e["event_type"] == "tool_result" and e["attributes"].get("tool") == "submit_patch"
                    and e["status"] == "ok" for e in events)
    metrics.update({
        "target_tests_passed": f"{len(target.get('names', [])) - len(target.get('failed', []))}/{len(target.get('names', []))}"
                               if target.get("names") else "-",
        "submit_patch_called": submitted,
        "model_calls": sum(agents.values()), "tool_calls": len(calls),
        "tool_errors": sum(errors.values()), "malformed_args": arg_errors,
        "calls_per_agent": dict(agents), "skill_scripts": dict(skill_scripts),
        "patch_files": patched_files, "gold_in_patch": bool(gold & set(patched_files)),
        "gold_ever_edited": bool(gold & set(edited_ever)),
        "reruns": sum(1 for e in events if e["event_type"] == "harness_intervention"
                      and e["attributes"].get("kind") == "root_rerun_nudge"),
    })
    return metrics

table = [run_metrics(row) for row in results]
(OUTPUT_ROOT / "experiment_summary.json").write_text(json.dumps({"results": results, "metrics": table,
                                                                 "gold_files": gold_files, "server": server_profile},
                                                                indent=2, default=str))
header = "| harness | task | outcome | target tests | submitted | termination | wall s | model calls | tool calls | tool errors | malformed args | gold file in patch | gold file ever edited | patch files |"
lines = [header, "|" + "---|" * 14]
for m in table:
    lines.append(f"| {m['harness']} | {m['task']} | {m['outcome']} | {m.get('target_tests_passed', '-')} | "
                 f"{m.get('submit_patch_called', '-')} | {m['termination']} | {m['wall_s']} | "
                 f"{m.get('model_calls', '-')} | {m.get('tool_calls', '-')} | {m.get('tool_errors', '-')} | "
                 f"{m.get('malformed_args', '-')} | {m.get('gold_in_patch', '-')} | {m.get('gold_ever_edited', '-')} | "
                 f"{', '.join(m.get('patch_files', [])) or '-'} |")
(OUTPUT_ROOT / "experiment_summary.md").write_text("\\n".join(lines) + "\\n")
display(Markdown("\\n".join(lines)))
for m in table:
    print(m["harness"], "agents:", m.get("calls_per_agent"), "skills:", m.get("skill_scripts"))
''')
    markdown("## Preserve diagnostics")
    code('''server.stop()
if CONTROL_ROOT.exists():
    shutil.rmtree(CONTROL_ROOT)
zip_directory(OUTPUT_ROOT, DIAGNOSTICS_ZIP)
print("Diagnostics:", DIAGNOSTICS_ZIP, f"({DIAGNOSTICS_ZIP.stat().st_size / 1e6:.1f} MB)")
print("Summary:", OUTPUT_ROOT / "experiment_summary.md")
''')
    for index, cell in enumerate(cells):
        cell["id"] = f"multi-{index:02d}"
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
            "source_sha256": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in sources.items()},
            "submission_sha256": {
                name: hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
                for name, files in submissions.items()
            },
        },
        "nbformat": 4, "nbformat_minor": 5,
    }
    text = json.dumps(notebook, indent=1, ensure_ascii=False)
    if len(text.encode()) >= KAGGLE_SOURCE_LIMIT:
        raise ValueError(f"Notebook is {len(text.encode())} bytes; Kaggle rejects sources over 1 MB")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    return output


if __name__ == "__main__":
    path = build()
    print(path, path.stat().st_size, "bytes")
