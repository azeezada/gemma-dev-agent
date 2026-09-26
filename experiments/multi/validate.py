"""Validate candidate submissions locally: assemble, parse with the v3 loader, report size.

    PYTHONPATH=tests:. <venv>/bin/python experiments/multi/validate.py [NAME ...]

Checks only what the local runtime can check without a model: file layout, YAML/includes,
declared tools and skills. It says nothing about whether Gemma will solve the task.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))
from assemble import CANDIDATES, assemble  # noqa: E402
from gemma_agent.env.v3 import load_submission  # noqa: E402


def main(names: list[str]) -> int:
    failed = 0
    for name in names or list(CANDIDATES):
        try:
            files = assemble(name)
            config = load_submission(files)
            tree = config.get("tree") or {"kind": "LlmAgent", "name": config.get("name")}
            print(f"OK   {name}: {len(files)} files, {len(json.dumps(files)) / 1e3:.1f} kB, root {tree['kind']} {tree.get('name')}")
        except Exception as exc:  # report every candidate, not just the first failure
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
