"""Build the end-to-end MVP notebook (plan section 17): rollouts -> token parity -> filter -> LoRA SFT ->
serving parity -> eval -> submission.zip. One Kaggle 4xL4 session, internet ON (peft/accelerate if missing).

    python notebooks/src/build_train_mvp.py   # -> notebooks/build/train_mvp.ipynb + kaggle/train_mvp/

Success = the notebook completes and produces a loadable adapter and a valid submission.zip; a score gain is not
expected. Every stage writes metrics to /kaggle/working/mvp_run/metrics.jsonl; gates fail loudly at the end.
"""
from __future__ import annotations

import ast
import hashlib
import json
import subprocess
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
OUTPUT = PROJECT / "notebooks" / "build" / "train_mvp.ipynb"
KAGGLE_DIR = PROJECT / "kaggle" / "train_mvp"
HARNESS = PROJECT / "harnesses" / "train_v1"
CONTRACT_ID = "train_v1"

# MVP split (plan 17). These public tasks were validated earlier (baseline fails, gold passes, no pytest-httpbin).
# Training on public tasks departs from D6 and is accepted only for this throwaway pipeline adapter: its rollout
# tasks are recorded in the manifest and it must never be used for eval comparisons.
ROLLOUT_TASK_IDS = ("rich_4076", "fastapi_14492", "rich_3468")
EVAL_TASK_IDS = ("fastapi_14258", "rich_4077")
SAMPLES_PER_TASK = 2

KERNEL_METADATA = {
    "id": "dawoodazeeza/gemma-dev-train-mvp",
    "title": "gemma-dev-train-mvp",
    "code_file": "train_mvp.ipynb",
    "language": "python",
    "kernel_type": "notebook",
    "is_private": True,
    "enable_gpu": True,
    "enable_tpu": False,
    "enable_internet": True,
    "dataset_sources": ["nguyenkhaho/vllm-wheel"],
    "kernel_sources": [],
    "competition_sources": ["gemma-4-developer-agent"],
    "model_sources": ["google/gemma-4/Other/gemma-4-31b-it-qat-w4a16-ct/2"],
    # Same image as the multi-experiment runs (Python 3.12; the vLLM 0.29.0 wheel set was built for it).
    "docker_image": "gcr.io/kaggle-private-byod/python@sha256:37c64f7dd9c54116ecd1bcc88817c5469b88387388fade02bfa8bf3fc647d461",
    "machine_shape": "NvidiaL4",
}


def _sources() -> dict[str, str]:
    package = PROJECT / "src" / "gemma_agent"
    return {
        path.relative_to(PROJECT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
    }


def _harness() -> dict[str, str]:
    return {p.relative_to(HARNESS).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(HARNESS.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _git_sha() -> str:
    out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=PROJECT, capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "src", "harnesses/train_v1", "notebooks/src/build_train_mvp.py"],
                           cwd=PROJECT, capture_output=True, text=True).stdout.strip()
    return (out.stdout.strip() or "unknown") + ("-dirty" if dirty else "")


CONFIG = r'''
from __future__ import annotations
import importlib.metadata, json, os, shutil, subprocess, sys, threading, time
from pathlib import Path
from IPython.display import JSON, display

NOTEBOOK_STARTED = time.monotonic()
SOURCE_GIT_SHA = __SHA__
CONTRACT_ID = __CONTRACT__
ROLLOUT_TASK_IDS = __ROLLOUT__
EVAL_TASK_IDS = __EVAL__
SAMPLES_PER_TASK = __SAMPLES__
# Kaggle session length. Rollouts stop starting at SESSION_LIMIT - RESERVE (time kept for training,
# vLLM restart, parity, eval and packaging); eval runs stop starting 10 min before the limit.
SESSION_LIMIT_SECONDS = 7200
RESERVE_SECONDS = 75 * 60
LORA = {"rank": 16, "alpha": 32, "lr": 1e-4, "epochs": 2, "max_len": 16384}
ADAPTER_NAME = "mvp_lora"
PROBE_TOKENS = 1024
PYTEST_VERSION = "8.3.4"
VLLM_VERSION = "0.29.0"
MAX_MODEL_LEN = 32768
BASE_MODEL_NAME = "gemma-4-31b-it-qat-w4a16-ct"

MODEL_ROOT = Path("/kaggle/input/models/google/gemma-4/other/gemma-4-31b-it-qat-w4a16-ct/2")
DATA_ROOT = Path("/kaggle/input/competitions/gemma-4-developer-agent")
WORK = Path("/kaggle/working")
RUNTIME = WORK / "mvp_runtime"
RUN_DIR = WORK / "mvp_run"                      # registry run: manifest, metrics, reports, adapter
ROLLOUTS = RUN_DIR / "rollouts"
EVALS = RUN_DIR / "eval"
CONTROL = WORK / "mvp_private_control"          # test/gold patches: controller-only, deleted at the end
SCRATCH = WORK / "mvp_scratch"
ADAPTER_DIR = RUN_DIR / "adapter" / ADAPTER_NAME
SUBMISSION_ZIP = WORK / "submission.zip"
RUN_ID = "mvp-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
for stale in (RUN_DIR, CONTROL, SCRATCH):
    shutil.rmtree(stale, ignore_errors=True)
RUN_DIR.mkdir(parents=True)
GATES: dict[str, bool] = {}
STAGES: dict[str, float] = {}

def elapsed() -> float:
    return time.monotonic() - NOTEBOOK_STARTED

def stage(name: str) -> None:
    STAGES[name] = round(elapsed(), 1)
    print(f"=== {name} at {elapsed():.0f}s", flush=True)

print(RUN_ID, SOURCE_GIT_SHA, ROLLOUT_TASK_IDS, EVAL_TASK_IDS)
'''

INSTALL = r'''
stage("setup")
wheel_root = next((p for p in (Path("/kaggle/input/datasets/nguyenkhaho/vllm-wheel/wheels"),
                               Path("/kaggle/input/vllm-wheel/wheels")) if p.is_dir()), None)
if wheel_root is None:
    matches = list(Path("/kaggle/input").glob("**/vllm-0.29.0-*.whl"))
    wheel_root = matches[0].parent if matches else None
if wheel_root is None:
    raise FileNotFoundError("Attach the nguyenkhaho/vllm-wheel dataset")

def version(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None

def pip(*args):
    result = subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1200)
    print(result.stdout[-3000:])
    if result.returncode:
        raise RuntimeError(f"pip install {args} failed")

if version("vllm") != VLLM_VERSION:
    pip("--upgrade", "--no-index", f"--find-links={wheel_root}", f"vllm=={VLLM_VERSION}")
# Training libraries without dependency resolution, so vLLM's torch/transformers pins stay intact.
for package in ("accelerate", "peft"):
    if version(package) is None:
        pip("--no-deps", package)
VERSIONS = {name: version(name) for name in ("vllm", "torch", "transformers", "peft", "accelerate",
                                             "compressed-tensors", "safetensors", "pytest")}
print(json.dumps(VERSIONS, indent=1))
if VERSIONS["vllm"] != VLLM_VERSION:
    raise RuntimeError("Unexpected vLLM version")

config_files = sorted(MODEL_ROOT.rglob("config.json"))
MODEL_PATH = max(config_files, key=lambda p: sum(c.suffix == ".safetensors" for c in p.parent.iterdir())).parent
# Preflight the training stack now (CPU only), not after an hour of rollouts.
preflight = subprocess.run([sys.executable, "-c", f"""
import json, transformers, peft
from transformers import AutoConfig, CompressedTensorsConfig
config = AutoConfig.from_pretrained({str(MODEL_PATH)!r})
arch = config.architectures[0]
assert hasattr(transformers, arch), 'transformers lacks ' + arch
print(json.dumps({{'architecture': arch, 'model_type': config.model_type,
                  'softcap': getattr(config.get_text_config(), 'final_logit_softcapping', None)}}))
"""], capture_output=True, text=True, timeout=600)
print(preflight.stdout, preflight.stderr[-3000:])
if preflight.returncode:
    raise RuntimeError("Training stack preflight failed (see above); fix before spending GPU time")
'''

MATERIALIZE = r'''
RUNTIME_SOURCES = __SOURCES__
HARNESS_FILES = __HARNESS__
shutil.rmtree(RUNTIME, ignore_errors=True)
for relative, content in RUNTIME_SOURCES.items():
    target = RUNTIME / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
HARNESS_DIR = RUNTIME / "harness" / CONTRACT_ID
for relative, content in HARNESS_FILES.items():
    target = HARNESS_DIR / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
sys.path.insert(0, str(RUNTIME / "src"))
from gemma_agent.rollout import RunConfig, TaskSpec, VerificationSpec, run_task
from gemma_agent.env.harness import VLLMServer, zip_directory
from gemma_agent.tasks.validate import pytest_command, validate_task
from gemma_agent.data.tokens import parity_report
from gemma_agent.data.sft import build_dataset, find_runs, run_record, select_runs
from gemma_agent.eval.package import stage_submission, zip_submission
from gemma_agent.eval.parity import parity_gate, vllm_prompt_logprobs
from gemma_agent.observe.registry import log_metric, write_manifest

def metric(name, value, **tags):
    log_metric(RUN_DIR, name, value, **tags)

WHEELS = WORK / "mvp_test_wheels"
WHEELS.mkdir(exist_ok=True)
for source in (DATA_ROOT / "wheels").glob("*.whl"):
    if not (WHEELS / source.name).exists():
        (WHEELS / source.name).symlink_to(source)

TASKS = {}
with (DATA_ROOT / "tasks.jsonl").open(encoding="utf-8") as handle:
    for line in handle:
        row = json.loads(line)
        if row["instance_id"] in ROLLOUT_TASK_IDS + EVAL_TASK_IDS:
            TASKS[row["instance_id"]] = row
missing = set(ROLLOUT_TASK_IDS + EVAL_TASK_IDS) - set(TASKS)
if missing:
    raise KeyError(f"Tasks not found: {sorted(missing)}")
CONTROL.mkdir(parents=True)

def specs(task_id):
    row = TASKS[task_id]
    test_patch = CONTROL / f"{task_id}.test.patch"
    test_patch.write_text(row["test_patch"], encoding="utf-8")
    graph, embeddings = DATA_ROOT / "graphs" / f"{task_id}.json", DATA_ROOT / "embeddings" / f"{task_id}.npz"
    task = TaskSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "repository": row["repo"],
        "problem_statement": row["problem_statement"], "hints": row.get("hints_text", ""),
        "snapshot_path": str(DATA_ROOT / "snapshots" / f"{task_id}.tgz"),
        **({"graph_path": str(graph)} if graph.is_file() else {}),
        **({"embeddings_path": str(embeddings)} if embeddings.is_file() else {}),
    })
    verification = VerificationSpec.from_dict({
        "schema_version": "1.0", "task_id": task_id, "test_patch_path": str(test_patch),
        "verification_command": pytest_command(row["test_patch"]), "timeout_seconds": 600,
    })
    return task, verification

def run_config(submission_dir, output_root, name, seed):
    return RunConfig.from_dict({
        "schema_version": "1.0", "submission_directory": str(submission_dir), "output_root": str(output_root),
        "work_root": str(SCRATCH / "work"), "experiment_name": name, "seed": seed,
        "model": {"path": str(MODEL_PATH), "name": BASE_MODEL_NAME},
        "server": {"max_model_len": MAX_MODEL_LEN}, "wheels_directory": str(WHEELS), "pytest_version": PYTEST_VERSION,
    })

def start_server(lora_modules=None):
    server = VLLMServer(model_path=MODEL_PATH, model_name=BASE_MODEL_NAME, log_path=RUN_DIR / f"server_{int(elapsed())}.log",
                        max_model_len=MAX_MODEL_LEN, startup_timeout=1800, enable_lora=True, lora_modules=lora_modules)
    return server

def progress(status):
    print(f"vLLM loading {status['elapsed_seconds']:.0f}s | {status['last_log'][:200]}", flush=True)
print(f"Runtime ready: {len(RUNTIME_SOURCES)} source files, harness {CONTRACT_ID}, model {MODEL_PATH}")
'''

ENV_OK = r'''
# Stage 1-2a: vLLM loads in the background (competition flags + --enable-lora) while the env_ok gate
# (plan 6.2.3) checks every selected task on CPU: baseline must fail and the gold patch must pass.
stage("env_ok")
server = start_server()
server_state = {}
def _start():
    try:
        server_state["profile"] = server.start(status_callback=progress)
    except Exception as exc:
        server_state["error"] = exc
loader = threading.Thread(target=_start, daemon=True)
loader.start()
ENV_OK = {}
with (RUN_DIR / "env_ok.jsonl").open("w", encoding="utf-8") as handle:
    for task_id in ROLLOUT_TASK_IDS + EVAL_TASK_IDS:
        row = validate_task(TASKS[task_id], DATA_ROOT / "snapshots" / f"{task_id}.tgz", SCRATCH / "env_ok",
                            wheels=WHEELS, pytest_version=PYTEST_VERSION)
        ENV_OK[task_id] = row
        handle.write(json.dumps(row) + "\n")
        print(f"env_ok {task_id}: {row['reason']} ({row['seconds']}s)", flush=True)
metric("env_ok_rate", sum(r["env_ok"] for r in ENV_OK.values()) / len(ENV_OK), stage="env_ok")
GATES["env_ok_any_rollout_task"] = any(ENV_OK[t]["env_ok"] for t in ROLLOUT_TASK_IDS)
loader.join()
if "error" in server_state:
    raise server_state["error"]
metric("vllm_startup_seconds", server_state["profile"]["startup_seconds"], stage="rollout")
client = server.client
TOKENIZER_PROFILE = client.configure_tokenizer(MODEL_PATH, ["run_command", "read_file", "get_status", "submit_patch"])
print(TOKENIZER_PROFILE)
'''

ROLLOUT = r'''
# Stage 2: rollouts with the frozen training harness; every model call records vLLM's exact token ids.
stage("rollouts")
ROLLOUT_RESULTS = []
rollout_started = time.monotonic()
queue = [(t, k) for k in range(SAMPLES_PER_TASK) for t in ROLLOUT_TASK_IDS if ENV_OK[t]["env_ok"]]
try:
    for task_id, sample in queue:
        if elapsed() > SESSION_LIMIT_SECONDS - RESERVE_SECONDS:
            print(f"Rollout cutoff at {elapsed():.0f}s; {len(queue) - len(ROLLOUT_RESULTS)} runs skipped", flush=True)
            break
        task, verification = specs(task_id)
        tick = time.monotonic()
        result = run_task(task, verification, run_config(HARNESS_DIR, ROLLOUTS, "mvp-rollout", 42 + sample), client=client)
        row = {**result.as_dict(), "sample": sample, "seconds": round(time.monotonic() - tick, 1)}
        ROLLOUT_RESULTS.append(row)
        metric("rollout_outcome_pass", float(result.outcome == "PASS"), stage="rollout", task_id=task_id, sample=sample)
        metric("rollout_seconds", row["seconds"], stage="rollout", task_id=task_id)
        print(f"{task_id}#{sample}: {result.outcome} {result.termination_reason} {row['seconds']}s", flush=True)
finally:
    server.stop()
hours = (time.monotonic() - rollout_started) / 3600
metric("rollouts_per_session_hour", len(ROLLOUT_RESULTS) / hours if hours else 0.0, stage="rollout")
metric("rollout_pass_rate", sum(r["outcome"] == "PASS" for r in ROLLOUT_RESULTS) / max(1, len(ROLLOUT_RESULTS)), stage="rollout")
GATES["rollouts_completed"] = bool(ROLLOUT_RESULTS) and all(r["completed"] for r in ROLLOUT_RESULTS)
display(JSON(ROLLOUT_RESULTS))
'''

TOKEN_PARITY = r'''
# Stage 2b (plan 6.2.2): the HF renderer must reproduce vLLM's prompt token ids on every recorded turn.
# Variant 2 also forwards `reasoning_effort` into the template, in case vLLM does.
stage("token_parity")
RUN_DIRS = find_runs(ROLLOUTS)
TOKEN_PARITY = parity_report(client.tokenizer, RUN_DIRS, extra_kwargs=({}, {"reasoning_effort": "reasoning_effort"}))
(RUN_DIR / "token_parity.json").write_text(json.dumps(TOKEN_PARITY, indent=1))
metric("token_parity_turns", TOKEN_PARITY["turns_with_vllm_ids"], stage="token_parity")
if TOKEN_PARITY["prefix_consistent_rate"] is not None:
    metric("prefix_consistent_rate", TOKEN_PARITY["prefix_consistent_rate"], stage="token_parity")
GATES["token_parity"] = TOKEN_PARITY["passed"]
print(json.dumps({k: v for k, v in TOKEN_PARITY.items() if k != "mismatches"}, indent=1))
if TOKEN_PARITY["mismatches"]:
    print("first mismatch:", json.dumps(TOKEN_PARITY["mismatches"][0]))
'''

FILTER = r'''
# Stage 3: passing + protocol filters; if nothing passes, fall back to all clean-token runs so every
# later stage still runs (fallback data is marked and the adapter is a pipeline test only).
stage("filter")
RECORDS = [run_record(p) for p in RUN_DIRS]
SELECTED, REJECTED = select_runs(RECORDS, per_task=2, fallback=True)
FALLBACK = any(r.get("fallback") for r in SELECTED)
ROWS = build_dataset(SELECTED, contract_id=CONTRACT_ID, max_len=MAX_MODEL_LEN)
SFT_PATH = RUN_DIR / "sft.jsonl"
SFT_PATH.write_text("".join(json.dumps(r) + "\n" for r in ROWS))
PROBES_PATH = RUN_DIR / "probes.jsonl"
PROBES_PATH.write_text("".join(json.dumps({"id": r["id"], "input_ids": r["input_ids"][:PROBE_TOKENS]}) + "\n"
                               for r in ROWS[:2]))
(RUN_DIR / "sft_selection.json").write_text(json.dumps(
    {"records": RECORDS, "selected": [r["run"] for r in SELECTED], "rejected": REJECTED, "fallback": FALLBACK}, indent=1))
metric("sft_runs", len(SELECTED), stage="filter")
metric("sft_sequences", len(ROWS), stage="filter")
metric("sft_tokens", sum(len(r["input_ids"]) for r in ROWS), stage="filter")
metric("sft_trained_tokens", sum(sum(r["loss_mask"]) for r in ROWS), stage="filter")
GATES["sft_data"] = bool(ROWS)
print(f"selected {len(SELECTED)} runs -> {len(ROWS)} sequences; fallback={FALLBACK}; rejected={REJECTED}")
if not ROWS:
    raise RuntimeError("No run has complete vLLM token ids; check token_parity.json and the rollout traces")
'''

TRAIN = r'''
# Stage 4-5a: LoRA SFT in a subprocess (GPU memory is fully released when it exits), then HF logprobs
# of the probe sequences with and without the adapter.
stage("train")
train_log = RUN_DIR / "train.log"
command = [sys.executable, "-m", "gemma_agent.train.lora_sft", "--model", str(MODEL_PATH), "--data", str(SFT_PATH),
           "--out", str(ADAPTER_DIR), "--run-dir", str(RUN_DIR), "--rank", str(LORA["rank"]),
           "--alpha", str(LORA["alpha"]), "--lr", str(LORA["lr"]), "--epochs", str(LORA["epochs"]),
           "--max-len", str(LORA["max_len"]), "--probes", str(PROBES_PATH),
           "--probe-out", str(RUN_DIR / "hf_probe_logprobs.json")]
env = {**os.environ, "PYTHONPATH": str(RUNTIME / "src"), "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
with train_log.open("w") as log:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    for line in process.stdout:
        log.write(line)
        if line.startswith("[train]") or "Error" in line or "error" in line:
            print(line, end="", flush=True)
    returncode = process.wait()
GATES["train"] = returncode == 0 and (ADAPTER_DIR / "adapter_model.safetensors").is_file()
if not GATES["train"]:
    print(train_log.read_text()[-6000:])
    raise RuntimeError(f"Training failed (exit {returncode}); see mvp_run/train.log")
print(json.loads((ADAPTER_DIR / "adapter_config.json").read_text()))
'''

SERVING = r'''
# Stage 5b: vLLM serves base + adapter with competition flags; probe logprobs must match HF (plan 9).
stage("serving_parity")
server = start_server({ADAPTER_NAME: ADAPTER_DIR})
server.start(status_callback=progress)
client = server.client
client.configure_tokenizer(MODEL_PATH, ["run_command", "read_file", "get_status", "submit_patch"])
hf = json.loads((RUN_DIR / "hf_probe_logprobs.json").read_text())
probes = {}
for probe_id, row in hf.items():
    ids = row["input_ids"]
    probes[probe_id] = {"hf_base": row["hf_base"], "hf_adapter": row["hf_adapter"],
                        "vllm_base": vllm_prompt_logprobs(client.base_url, BASE_MODEL_NAME, ids),
                        "vllm_adapter": vllm_prompt_logprobs(client.base_url, ADAPTER_NAME, ids)}
SERVING_PARITY = parity_gate(probes)
(RUN_DIR / "serving_parity.json").write_text(json.dumps(SERVING_PARITY, indent=1))
for probe_id, row in SERVING_PARITY["probes"].items():
    metric("parity_base_mean_abs", row["base"]["mean_abs"], stage="parity", probe=probe_id)
    metric("parity_adapter_mean_abs", row["adapter"]["mean_abs"], stage="parity", probe=probe_id)
    metric("parity_effect_correlation", row["effect_correlation"], stage="parity", probe=probe_id)
GATES["serving_parity"] = SERVING_PARITY["passed"]
print(json.dumps(SERVING_PARITY, indent=1)[:4000])
'''

EVAL = r'''
# Stage 6: base vs adapter on held-out tasks, same harness; the adapter run uses the packaged submission
# (agent.yaml `adapter: mvp_lora`), so the model name routing is exercised exactly as in the competition.
stage("eval")
STAGE_DIR = stage_submission(HARNESS_DIR, SCRATCH / "submission", {ADAPTER_NAME: ADAPTER_DIR}, ADAPTER_NAME)
EVAL_RESULTS = []
try:
    for task_id in EVAL_TASK_IDS:
        if not ENV_OK[task_id]["env_ok"]:
            continue
        for variant, submission_dir in (("base", HARNESS_DIR), ("adapter", STAGE_DIR)):
            if elapsed() > SESSION_LIMIT_SECONDS - 10 * 60:
                print(f"Eval cutoff at {elapsed():.0f}s", flush=True)
                break
            task, verification = specs(task_id)
            result = run_task(task, verification, run_config(submission_dir, EVALS / variant, f"mvp-eval-{variant}", 42),
                              client=client)
            EVAL_RESULTS.append({**result.as_dict(), "variant": variant})
            metric("eval_pass", float(result.outcome == "PASS"), stage="eval", task_id=task_id, variant=variant)
            print(f"eval {variant} {task_id}: {result.outcome} {result.termination_reason}", flush=True)
finally:
    server.stop()
display(JSON(EVAL_RESULTS))
'''

PACKAGE = r'''
# Stage 7: package, manifest, diagnostics; then fail loudly on any gate.
stage("package")
PACKAGE_REPORT = zip_submission(STAGE_DIR, SUBMISSION_ZIP)
(RUN_DIR / "submission_check.json").write_text(json.dumps(PACKAGE_REPORT, indent=1))
GATES["submission_zip"] = PACKAGE_REPORT["ok"]
print(json.dumps({k: PACKAGE_REPORT[k] for k in ("zip", "zip_bytes", "zip_sha256", "declared_adapters")}, indent=1))

def rate(variant):
    rows = [r for r in EVAL_RESULTS if r["variant"] == variant]
    return {"n": len(rows), "pass": sum(r["outcome"] == "PASS" for r in rows)}

outcomes = {}
for row in ROLLOUT_RESULTS + EVAL_RESULTS:
    outcomes[row["outcome"]] = outcomes.get(row["outcome"], 0) + 1
reasons = {}
for row in ROLLOUT_RESULTS + EVAL_RESULTS:
    reasons[row["termination_reason"]] = reasons.get(row["termination_reason"], 0) + 1
write_manifest(
    RUN_DIR, run_id=RUN_ID, kind="experiment", contract_id=CONTRACT_ID, source_sha=SOURCE_GIT_SHA,
    config={"lora": LORA, "rollout_tasks": ROLLOUT_TASK_IDS, "eval_tasks": EVAL_TASK_IDS,
            "samples": SAMPLES_PER_TASK, "session_limit": SESSION_LIMIT_SECONDS},
    inputs={"model": str(MODEL_PATH), "vllm": VERSIONS["vllm"], "versions": json.dumps(VERSIONS),
            "chat_template_sha256": TOKENIZER_PROFILE["chat_template_sha256"],
            "trained_on_public_tasks": ",".join(r["task_id"] for r in SELECTED)},
    adapter=ADAPTER_DIR / "adapter_model.safetensors", gpu_hours=elapsed() / 3600,
    summary={"gates": GATES, "stages": STAGES, "fallback_data": FALLBACK, "outcomes": outcomes,
             "termination_reasons": reasons, "eval": {"base": rate("base"), "adapter": rate("adapter")}},
)
shutil.rmtree(CONTROL, ignore_errors=True)
shutil.rmtree(SCRATCH, ignore_errors=True)
zip_directory(RUN_DIR, WORK / "mvp_run.zip")
print(json.dumps({"gates": GATES, "stages": STAGES}, indent=1))
failed = [name for name, ok in GATES.items() if not ok]
if failed:
    raise RuntimeError(f"MVP gates failed: {failed}. Outputs are still in mvp_run.zip and submission.zip")
print("MVP complete: submission.zip and mvp_run.zip are ready (submit only after review).")
'''


def build(output: Path = OUTPUT) -> Path:
    sources, harness = _sources(), _harness()
    cells: list[dict] = []

    def markdown(text: str) -> None:
        cells.append({"cell_type": "markdown", "metadata": {}, "source": text.strip().splitlines(keepends=True)})

    def code(text: str) -> None:
        text = text.strip() + "\n"
        ast.parse(text)
        cells.append({"cell_type": "code", "execution_count": None, "metadata": {}, "outputs": [],
                      "source": text.splitlines(keepends=True)})

    markdown(f"""# gemma-dev MVP: rollouts -> SFT -> LoRA -> parity -> eval -> submission

Plan section 17. Contract `{CONTRACT_ID}`; rollout tasks {', '.join(ROLLOUT_TASK_IDS)} x {SAMPLES_PER_TASK};
held-out eval tasks {', '.join(EVAL_TASK_IDS)}. Needs 4xL4, internet ON, the competition data, the Gemma 4 31B
QAT model and the `nguyenkhaho/vllm-wheel` dataset. Outputs: `submission.zip` (not submitted) and `mvp_run.zip`
(manifest, metrics, gates, traces, adapter). Built from `notebooks/src/build_train_mvp.py`; do not hand-edit.""")
    code(CONFIG.replace("__SHA__", repr(_git_sha())).replace("__CONTRACT__", repr(CONTRACT_ID))
         .replace("__ROLLOUT__", repr(ROLLOUT_TASK_IDS)).replace("__EVAL__", repr(EVAL_TASK_IDS))
         .replace("__SAMPLES__", repr(SAMPLES_PER_TASK)))
    markdown("## Setup: offline vLLM, training libraries, preflight of the training stack")
    code(INSTALL)
    markdown("## Runtime source, harness and task specs")
    code(MATERIALIZE.replace("__SOURCES__", json.dumps(sources, ensure_ascii=False))
         .replace("__HARNESS__", json.dumps(harness, ensure_ascii=False)))
    for title, body in (("## 1. env_ok gate while vLLM loads", ENV_OK), ("## 2. Rollouts", ROLLOUT),
                        ("## 2b. Token parity (renderer vs vLLM)", TOKEN_PARITY), ("## 3. Filter to SFT data", FILTER),
                        ("## 4. LoRA SFT", TRAIN), ("## 5. Serving parity (HF vs vLLM logprobs)", SERVING),
                        ("## 6. Eval: base vs adapter", EVAL), ("## 7. Package and gates", PACKAGE)):
        markdown(title)
        code(body)
    for index, cell in enumerate(cells):
        cell["id"] = f"mvp-{index:02d}"
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python", "version": "3.12"},
            "source_sha256": {name: hashlib.sha256(text.encode()).hexdigest() for name, text in sources.items()},
        },
        "nbformat": 4, "nbformat_minor": 5,
    }
    text = json.dumps(notebook, indent=1, ensure_ascii=False)
    if len(text.encode()) >= 1_000_000:
        raise ValueError(f"notebook is {len(text.encode())} bytes; Kaggle rejects sources over 1 MB")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    KAGGLE_DIR.mkdir(parents=True, exist_ok=True)
    (KAGGLE_DIR / "kernel-metadata.json").write_text(json.dumps(KERNEL_METADATA, indent=2) + "\n", encoding="utf-8")
    return output


if __name__ == "__main__":
    print(build())
