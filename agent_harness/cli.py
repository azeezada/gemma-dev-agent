from __future__ import annotations

import argparse
import json
from pathlib import Path

from .reporting import export_atif, materialize_trace, render_reports, validate_trace
from .runtime import run_task
from .schemas import RunConfig, TaskSpec, VerificationSpec


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_harness")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run and verify one task")
    run.add_argument("--task", required=True, type=Path)
    run.add_argument("--verification", required=True, type=Path)
    run.add_argument("--config", required=True, type=Path)
    validate = commands.add_parser("validate", help="Validate a run trace")
    validate.add_argument("--run", required=True, type=Path)
    validate.add_argument("--allow-incomplete", action="store_true")
    render = commands.add_parser("render", help="Regenerate derived trace reports")
    render.add_argument("--run", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        result = run_task(
            TaskSpec.load(args.task), VerificationSpec.load(args.verification), RunConfig.load(args.config)
        )
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.completed else 2
    if args.command == "validate":
        result = validate_trace(args.run, require_complete=not args.allow_incomplete)
        print(json.dumps(result, indent=2))
        return 0 if result["valid"] else 1
    materialize_trace(args.run)
    export_atif(args.run)
    render_reports(args.run)
    print(json.dumps({"run": str(args.run), "rendered": True}, indent=2))
    return 0
