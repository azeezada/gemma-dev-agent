"""Assemble and statically check a competition `submission.zip` with LoRA adapters (plan 14, 17 step 7).

Checks mirror HARNESS_README section 2.4/3.4: allowed extensions only, no symlinks or traversal, one base model,
`adapter:` names resolving to `adapters/<name>/{adapter_config.json, adapter_model.safetensors}`, rank <= 128,
at most 8 adapters, unpacked size < 3 GiB. Adapter weights must be language-model LoRA tensors only.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import struct
import zipfile
from pathlib import Path
from typing import Any

from gemma_agent.env.v3 import load_submission

ALLOWED_EXTENSIONS = {".yaml", ".yml", ".md", ".txt", ".py", ".json", ".safetensors"}
MAX_TOTAL_BYTES = 3 * 1024**3
MAX_RANK = 128
MAX_ADAPTERS = 8
NON_TEXT = re.compile(r"vision|audio|multi_modal_projector|embed_vision|embed_audio")
LORA_KEY = re.compile(r"\.lora_[AB]\.weight$")


def safetensors_header(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        (length,) = struct.unpack("<Q", handle.read(8))
        return json.loads(handle.read(length))


def check_adapter(adapter_dir: Path, max_rank: int = MAX_RANK) -> dict[str, Any]:
    adapter_dir = Path(adapter_dir)
    problems: list[str] = []
    config_path, weights_path = adapter_dir / "adapter_config.json", adapter_dir / "adapter_model.safetensors"
    if not config_path.is_file() or not weights_path.is_file():
        return {"ok": False, "problems": ["adapter_config.json and adapter_model.safetensors are required"]}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    header = {k: v for k, v in safetensors_header(weights_path).items() if k != "__metadata__"}
    rank = int(config.get("r", 0))
    if not 1 <= rank <= max_rank:
        problems.append(f"rank {rank} outside 1..{max_rank}")
    bad_keys = [k for k in header if not LORA_KEY.search(k)]
    if bad_keys:
        problems.append(f"non-LoRA tensors: {bad_keys[:3]}")
    non_text = [k for k in header if NON_TEXT.search(k)]
    if non_text:
        problems.append(f"non-language-model tensors: {non_text[:3]}")
    shapes = {min(v["shape"]) for k, v in header.items() if LORA_KEY.search(k)}
    if shapes - {rank}:
        problems.append(f"tensor ranks {sorted(shapes)} differ from config r={rank}")
    dtypes = sorted({v["dtype"] for v in header.values()})
    if set(dtypes) - {"BF16", "F16", "F32"}:
        problems.append(f"unexpected dtypes {dtypes}")
    modules = sorted({k.split(".lora_")[0].rsplit(".", 1)[-1] for k in header if LORA_KEY.search(k)})
    return {"ok": not problems, "problems": problems, "rank": rank, "alpha": config.get("lora_alpha"),
            "tensors": len(header), "modules": modules, "dtypes": dtypes,
            "bytes": weights_path.stat().st_size, "sha256": _sha256(weights_path)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def with_adapter(agent_yaml: str, adapter: str) -> str:
    """Add `adapter: <name>` after the root agent's `model:` line (replacing an existing adapter line)."""
    lines = [line for line in agent_yaml.splitlines() if not line.startswith("adapter:")]
    index = next(i for i, line in enumerate(lines) if line.startswith("model:"))
    lines.insert(index + 1, f"adapter: {adapter}")
    return "\n".join(lines) + "\n"


def stage_submission(harness_dir: Path, stage_dir: Path, adapters: dict[str, Path], root_adapter: str | None) -> Path:
    """Copy a harness and its adapters into `stage_dir`; the root agent uses `root_adapter`."""
    harness_dir, stage_dir = Path(harness_dir), Path(stage_dir)
    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    for path in sorted(harness_dir.rglob("*")):
        relative = path.relative_to(harness_dir)
        if path.is_file() and "__pycache__" not in relative.parts and not relative.name.startswith("."):
            (stage_dir / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, stage_dir / relative)
    if root_adapter:
        agent = stage_dir / "agent.yaml"
        agent.write_text(with_adapter(agent.read_text(encoding="utf-8"), root_adapter), encoding="utf-8")
    for name, source in adapters.items():
        target = stage_dir / "adapters" / name
        target.mkdir(parents=True)
        for member in ("adapter_config.json", "adapter_model.safetensors"):
            shutil.copyfile(Path(source) / member, target / member)
    return stage_dir


def check_submission(stage_dir: Path) -> dict[str, Any]:
    stage_dir = Path(stage_dir)
    problems: list[str] = []
    files = [p for p in sorted(stage_dir.rglob("*")) if p.is_file() or p.is_symlink()]
    if any(p.is_symlink() for p in files):
        problems.append("symlinks are not allowed")
    bad_ext = [p.relative_to(stage_dir).as_posix() for p in files if p.suffix not in ALLOWED_EXTENSIONS]
    if bad_ext:
        problems.append(f"disallowed extensions: {bad_ext}")
    total = sum(p.stat().st_size for p in files)
    if total >= MAX_TOTAL_BYTES:
        problems.append(f"unpacked size {total} >= 3 GiB")
    text = {p.relative_to(stage_dir).as_posix(): p.read_text(encoding="utf-8") for p in files
            if p.relative_to(stage_dir).parts[0] != "adapters"}
    config: dict[str, Any] = {}
    try:
        config = load_submission(text)
    except ValueError as exc:
        problems.append(f"harness: {exc}")
    declared = {a for a in [config.get("adapter")] + [c.get("adapter") for c in config.get("agent_tools", {}).values()] if a}
    present = {p.name for p in (stage_dir / "adapters").iterdir()} if (stage_dir / "adapters").is_dir() else set()
    if declared - present:
        problems.append(f"adapter(s) referenced but missing: {sorted(declared - present)}")
    if len(present) > MAX_ADAPTERS:
        problems.append(f"{len(present)} adapters > {MAX_ADAPTERS}")
    adapters = {name: check_adapter(stage_dir / "adapters" / name) for name in sorted(present)}
    problems += [f"adapter {name}: {p}" for name, row in adapters.items() for p in row["problems"]]
    return {"ok": not problems, "problems": problems, "unpacked_bytes": total, "declared_adapters": sorted(declared),
            "adapters": adapters, "files": [p.relative_to(stage_dir).as_posix() for p in files]}


def zip_submission(stage_dir: Path, destination: Path) -> dict[str, Any]:
    """Deterministic zip of a checked stage directory (files at the archive root)."""
    report = check_submission(stage_dir)
    if not report["ok"]:
        raise ValueError(f"submission check failed: {report['problems']}")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in report["files"]:
            member = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            member.external_attr = 0o100644 << 16
            compress = zipfile.ZIP_STORED if name.endswith(".safetensors") else zipfile.ZIP_DEFLATED
            with (Path(stage_dir) / name).open("rb") as source, archive.open(member, "w") as target:
                member.compress_type = compress
                shutil.copyfileobj(source, target, 1 << 20)
    return {**report, "zip": str(destination), "zip_bytes": destination.stat().st_size, "zip_sha256": _sha256(destination)}
