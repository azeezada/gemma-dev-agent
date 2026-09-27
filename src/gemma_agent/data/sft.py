"""Run selection and SFT dataset assembly (plan sections 3, 10, 17 step 3).

A run bundle becomes training data only if it resolved the task and passes the protocol filters (calls parse,
explicit submit_patch, no scratch files, fits the budget and the 32k window). At most `per_task` runs per task are
kept, shortest first. Usage (Kaggle or local):
    python -m gemma_agent.data.sft <runs_root> <out.jsonl> [--fallback]
"""
from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from gemma_agent.rollout.evaluation import derive_metrics

from .tokens import build_sequences, load_turns

OUTPUT_TOKEN_BUDGET = 10_000  # ~5 min at 28.7 ms/token (plan 1.1)
SCRATCH = re.compile(r"(^|/)(repro|reproduce|debug|scratch|tmp|temp|check|verify)[^/]*$", re.I)


def new_files(patch: str) -> list[str]:
    """Paths the patch creates (`--- /dev/null`)."""
    created, lines = [], patch.splitlines()
    for index, line in enumerate(lines[:-1]):
        if line.startswith("--- /dev/null") and lines[index + 1].startswith("+++ b/"):
            created.append(lines[index + 1][6:].strip())
    return created


def run_record(run_root: Path) -> dict[str, Any]:
    """Everything the filters need from one sealed run bundle."""
    run_root = Path(run_root)
    summary = json.loads((run_root / "final" / "summary.json").read_text(encoding="utf-8"))
    patch_path = run_root / "final" / "agent.patch"
    patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.is_file() else ""
    metrics = derive_metrics(run_root)["components"]
    turns = load_turns(run_root)
    created = new_files(patch)
    agent = summary.get("agent", {})
    return {
        "run": run_root.name, "path": str(run_root), "task_id": summary["task_id"],
        "outcome": summary["outcome"], "termination_reason": summary.get("termination_reason"),
        "explicit_submit": summary.get("termination_reason") == "submitted_patch",
        "malformed_calls": metrics["malformed_tool_call_count"],
        "max_context_utilization": metrics["maximum_context_utilization"],
        "completion_tokens": agent.get("completion_tokens_total", 0),
        "prompt_tokens_max": agent.get("prompt_tokens_max", 0),
        "scratch_files": [p for p in created if "/" not in p or SCRATCH.search(p)],
        "turns": len(turns), "turns_with_ids": sum(bool(t.get("prompt_token_ids")) for t in turns),
        "leak_audit_passed": summary.get("leak_audit_passed", False),
    }


def rejection_reasons(record: dict[str, Any], *, require_pass: bool = True,
                      output_budget: int = OUTPUT_TOKEN_BUDGET) -> list[str]:
    reasons = []
    if require_pass and record["outcome"] != "PASS":
        reasons.append(f"outcome={record['outcome']}")
    if not record["leak_audit_passed"]:
        reasons.append("leak_audit_failed")
    if record["malformed_calls"]:
        reasons.append("malformed_calls")
    if not record["explicit_submit"]:
        reasons.append("no_submit_patch")
    if record["scratch_files"]:
        reasons.append("scratch_files")
    if record["completion_tokens"] > output_budget:
        reasons.append("over_output_budget")
    if record["max_context_utilization"] > 1:
        reasons.append("over_context")
    if not record["turns_with_ids"] or record["turns_with_ids"] < record["turns"]:
        reasons.append("missing_token_ids")
    return reasons


def select_runs(records: list[dict[str, Any]], *, per_task: int = 2, fallback: bool = False,
                output_budget: int = OUTPUT_TOKEN_BUDGET) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Filter, then keep the `per_task` shortest runs per task.

    `fallback=True` (MVP only, so every later stage runs): if nothing passes, drop the resolve requirement and the
    protocol filters but still require complete token ids and a clean leak audit. Such data is not for a real adapter.
    """
    rejected = {r["run"]: rejection_reasons(r, output_budget=output_budget) for r in records}
    kept = [r for r in records if not rejected[r["run"]]]
    if not kept and fallback:
        kept = [r for r in records if r["leak_audit_passed"] and r["turns_with_ids"] and
                r["turns_with_ids"] == r["turns"]]
        for r in kept:
            r["fallback"] = True
    by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in kept:
        by_task[record["task_id"]].append(record)
    selected = [r for rows in by_task.values()
                for r in sorted(rows, key=lambda r: (r["completion_tokens"], r["run"]))[:per_task]]
    return sorted(selected, key=lambda r: r["run"]), {k: v for k, v in rejected.items() if v}


def build_dataset(records: list[dict[str, Any]], *, contract_id: str, max_len: int = 32_768) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        for index, seq in enumerate(build_sequences(load_turns(Path(record["path"])), max_len=max_len)):
            rows.append({
                "id": f"{record['run']}#{index}", "task_id": record["task_id"], "contract_id": contract_id,
                "fallback": record.get("fallback", False), "agent": seq["agent"], "turns": seq["turns"],
                "input_ids": seq["input_ids"], "loss_mask": seq["loss_mask"],
            })
    return rows


def find_runs(runs_root: Path) -> list[Path]:
    return sorted(p.parent.parent for p in Path(runs_root).rglob("final/summary.json"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--contract-id", default="train_v1")
    parser.add_argument("--per-task", type=int, default=2)
    parser.add_argument("--fallback", action="store_true")
    args = parser.parse_args(argv)
    records = [run_record(p) for p in find_runs(args.runs_root)]
    selected, rejected = select_runs(records, per_task=args.per_task, fallback=args.fallback)
    rows = build_dataset(selected, contract_id=args.contract_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(json.dumps({"runs": len(records), "selected": [r["run"] for r in selected],
                      "sequences": len(rows), "rejected": rejected}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
