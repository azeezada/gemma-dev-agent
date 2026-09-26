#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from _bootstrap import add_project_root

add_project_root()
from agent_harness.reporting import validate_trace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_directory")
    parser.add_argument("--allow-incomplete", action="store_true")
    args = parser.parse_args()
    result = validate_trace(args.run_directory, require_complete=not args.allow_incomplete)
    print(json.dumps(result, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
