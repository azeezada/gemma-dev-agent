#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from _bootstrap import add_project_root

add_project_root()
from gemma_agent.rollout.evaluation import derive_metrics  # noqa: E402
from gemma_agent.rollout.tracing import atomic_json  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = derive_metrics(args.run_directory)
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
