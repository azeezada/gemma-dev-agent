from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a read-only Kaggle dataset to an already validated publisher stage."
    )
    parser.add_argument("stage_dir", type=Path)
    parser.add_argument("dataset_source")
    args = parser.parse_args()

    stage = args.stage_dir.resolve()
    metadata_path = stage / "kernel-metadata.json"
    manifest_path = stage / "publish-manifest.json"
    if not metadata_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Stage must contain kernel-metadata.json and publish-manifest.json")

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    code_file = stage / metadata["code_file"]
    actual_sha = hashlib.sha256(code_file.read_bytes()).hexdigest()
    if actual_sha != manifest["notebook_sha256"]:
        raise RuntimeError("Staged notebook changed after publisher validation")

    sources = list(metadata.get("dataset_sources") or [])
    if args.dataset_source not in sources:
        sources.append(args.dataset_source)
    metadata["dataset_sources"] = sources
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "stage_dir": str(stage),
        "notebook_sha256": actual_sha,
        "enable_gpu": metadata.get("enable_gpu"),
        "enable_internet": metadata.get("enable_internet"),
        "machine_shape": metadata.get("machine_shape"),
        "competition_sources": metadata.get("competition_sources"),
        "dataset_sources": metadata.get("dataset_sources"),
        "model_sources": metadata.get("model_sources"),
    }, indent=2))


if __name__ == "__main__":
    main()
