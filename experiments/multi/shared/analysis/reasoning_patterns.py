"""Count recurring cognitive patterns in Gemma's reasoning text across all traced runs.

    python3 experiments/multi/shared/analysis/reasoning_patterns.py
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from aggregate import discover, read_blob  # noqa: E402

PATTERNS = {
    "speculative what-if / extra edge case": r"\bwhat if\b|edge case|to be safe|more robust|also handle|what about",
    "second-guessing (wait/actually)": r"\bwait\b|\bactually\b|\bhmm\b|on second thought",
    "plans to re-read / re-check": r"let me (re-?read|look again|check again|double.check|verify again)|re-?read",
    "believes a tool is unavailable": r"(can ?not|can't|don't have|no) (grep|search|run|access|tool)",
    "declares success": r"\b(this (should )?fix(es)?|the fix (is|works)|issue is (fixed|resolved)|now (it )?works)\b",
    "mentions time/budget": r"\b(time|budget|remaining|deadline|seconds)\b",
    "plans to write scratch file": r"(create|write) (a )?(test|repro|script|file)",
}


def main() -> None:
    per_run = []
    totals = collections.Counter()
    thoughts_total = 0
    for label, harness, run in discover():
        events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
        counts = collections.Counter()
        n = 0
        for e in events:
            if e["event_type"] != "model_response":
                continue
            try:
                message = json.loads(read_blob(run, e, "raw model response"))["choices"][0]["message"]
            except Exception:
                continue
            thought = message.get("reasoning") or message.get("reasoning_content") or ""
            if not thought:
                continue
            n += 1
            for name, pattern in PATTERNS.items():
                if re.search(pattern, thought, re.I):
                    counts[name] += 1
        thoughts_total += n
        totals.update(counts)
        per_run.append((label, harness, run.name.split("-")[0], n, dict(counts)))
    print(f"model responses with reasoning: {thoughts_total}")
    for name in PATTERNS:
        print(f"{totals[name]:5d} ({totals[name] / max(1, thoughts_total):.0%})  {name}")


if __name__ == "__main__":
    main()
