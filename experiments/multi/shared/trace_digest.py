"""Compact, readable digest of one traced run (events.jsonl + artifact blobs).

    python3 experiments/multi/shared/trace_digest.py <run_dir> [--width 600] [--reasoning]

One line per model call: t+seconds, agent, duration, prompt/completion tokens, then the
tool call arguments and the tool result (truncated). --reasoning adds the model's thought text.
Shared by every experiment agent so everyone reads traces the same way.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def blob(run: Path, event: dict, purpose_prefix: str) -> str:
    for ref in event.get("artifact_refs", []):
        if ref.get("purpose", "").startswith(purpose_prefix):
            path = run / ref["relative_path"]
            return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return ""


def clip(text: str, width: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width] + f"…[{len(text)} chars]"


def digest(run: Path, width: int, reasoning: bool) -> str:
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    start = events[0]["monotonic_ns"]
    rows = []
    for e in events:
        kind, a = e["event_type"], e.get("attributes", {})
        t = (e["monotonic_ns"] - start) / 1e9
        if kind == "model_response":
            usage = a.get("usage") or {}
            raw = blob(run, e, "raw model response")
            text, thought = "", ""
            try:
                message = json.loads(raw)["choices"][0]["message"]
                text = message.get("content") or ""
                thought = message.get("reasoning") or message.get("reasoning_content") or ""
            except Exception:
                pass
            rows.append(f"[{t:6.1f}s] {a.get('agent')} model {e.get('duration_ms', 0) / 1000:.1f}s "
                        f"prompt={usage.get('prompt_tokens')} out={usage.get('completion_tokens')} "
                        f"finish={a.get('finish_reason')}" + (f"\n    TEXT: {clip(text, width)}" if text.strip() else ""))
            if reasoning and thought:
                rows.append(f"    THINK: {clip(thought, width)}")
        elif kind == "tool_call":
            rows.append(f"    CALL {a.get('tool')}: {clip(blob(run, e, 'tool arguments'), width)}")
        elif kind == "tool_result":
            status = "ERR " + str(a.get("error_type")) if e.get("status") == "error" else "ok"
            changed = " [workspace changed]" if a.get("workspace_changed") else ""
            rows.append(f"    -> {status}{changed}: {clip(a.get('model_visible_summary', ''), width)}")
        elif kind in {"agent_transfer", "agent_output", "agent_error", "context_compaction",
                      "harness_intervention", "automatic_patch_recovery", "outcome"}:
            extra = {k: v for k, v in a.items() if k not in {"before_sha256", "after_sha256"}}
            rows.append(f"[{t:6.1f}s] == {kind} {clip(json.dumps(extra), width)}")
    return "\n".join(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--width", type=int, default=600)
    parser.add_argument("--reasoning", action="store_true")
    args = parser.parse_args()
    print(digest(args.run_dir, args.width, args.reasoning))
