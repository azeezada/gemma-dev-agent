"""Run registry: every Kaggle/local run writes `run_manifest.json` + `metrics.jsonl`; `pull_run` copies a finished
run into `runs/<run_id>/` and appends it to `runs/INDEX.csv` (idempotent per run_id)."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

MANIFEST = "run_manifest.json"
METRICS = "metrics.jsonl"
INDEX_FIELDS = ["run_id", "kind", "contract_id", "git_sha", "config_hash", "adapter_sha256", "gpu_hours", "created_at", "summary"]
KINDS = {"rollout", "train", "eval", "teacher", "taskpack", "experiment"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def config_hash(config: Any) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, default=str).encode()).hexdigest()[:16]


def git_sha(repo: Path | None = None) -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def write_manifest(run_dir: Path, *, run_id: str, kind: str, contract_id: str, config: Any = None,
                   inputs: dict[str, str] | None = None, adapter: Path | None = None,
                   gpu_hours: float = 0.0, summary: dict[str, Any] | None = None, repo: Path | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}; expected one of {sorted(KINDS)}")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "kind": kind, "contract_id": contract_id, "git_sha": git_sha(repo),
        "config_hash": config_hash(config), "inputs": inputs or {},
        "adapter_sha256": sha256_file(adapter) if adapter else "", "gpu_hours": round(float(gpu_hours), 4),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "summary": summary or {},
    }
    path = run_dir / MANIFEST
    path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return path


def log_metric(run_dir: Path, name: str, value: float, **tags: Any) -> None:
    """Append one metric row; `tags` (stage, task_id, step, ...) are stored beside the value."""
    with (Path(run_dir) / METRICS).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"t": round(time.time(), 3), "name": name, "value": value, **tags}) + "\n")


def read_metrics(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / METRICS
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_index(runs_root: Path) -> list[dict[str, str]]:
    path = Path(runs_root) / "INDEX.csv"
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def pull_run(src: Path, runs_root: Path) -> Path:
    """Copy a downloaded run into `runs/<run_id>/` and (re)write its INDEX.csv row."""
    src, runs_root = Path(src), Path(runs_root)
    manifest = json.loads((src / MANIFEST).read_text())
    dest = runs_root / manifest["run_id"]
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    rows = [r for r in load_index(runs_root) if r["run_id"] != manifest["run_id"]]
    rows.append({**{k: str(manifest.get(k, "")) for k in INDEX_FIELDS if k != "summary"},
                 "summary": json.dumps(manifest.get("summary", {}), sort_keys=True)})
    rows.sort(key=lambda r: (r["created_at"], r["run_id"]))
    with (runs_root / "INDEX.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return dest
