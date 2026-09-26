"""Aggregate runtime behaviour across every traced Kaggle run (v19–v22 + multi-v1).

    python3 experiments/multi/shared/analysis/aggregate.py [--json out.json]

Controller-side analysis only. Reads events.jsonl + artifact blobs; never used by an agent.
Hidden-test replay of intermediate workspace states lives in replay_states.py (writes
replay_cache.json, which this script reads if present).
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import statistics
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
ROOTS = [
    ("v19", "work/kaggle-trace-v19/trace_runs"),
    ("v20", "work/kaggle-trace-v20/trace_runs"),
    ("v21", "work/kaggle-trace-v21/trace_runs"),
    ("v22", "work/kaggle-trace-v22/trace_runs"),
    ("v22b", "work/kaggle-multi-v1/trace_runs"),
    ("multi1", "work/kaggle-multi-v1/multi_runs"),
]
SCRATCH = re.compile(r"(^|/)(repro|probe|scratch|tmp|debug|test_fix|check|reproduce)[\w-]*\.py$|^[^/]+\.py$")
HEDGE = re.compile(r"\b(wait|actually|hmm|but wait|let me reconsider|on second thought|however|alternatively)\b", re.I)
CLAIM = re.compile(r"\b(fixed|resolved|successfully|the fix (is|works)|now passes|all tests pass|issue is solved|done)\b", re.I)


def load_tasks() -> dict:
    return {t["instance_id"]: t for t in map(json.loads, (PROJECT / "tasks.jsonl").read_text().splitlines())}


def patch_files(text: str) -> list[str]:
    return re.findall(r"^\+\+\+ b/(\S+)", text or "", re.M)


def discover() -> list[tuple[str, str, Path]]:
    runs = []
    for label, root in ROOTS:
        for events in sorted((PROJECT / root).glob("**/events.jsonl")):
            run = events.parent
            harness = run.parent.parent.name if label == "multi1" else "single"
            runs.append((label, harness, run))
    return runs


def read_blob(run: Path, event: dict, prefix: str) -> str:
    for ref in event.get("artifact_refs", []):
        if ref.get("purpose", "").startswith(prefix):
            path = run / ref["relative_path"]
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
    return ""


def classify_malformed(tool: str, args_text: str, result: dict) -> str | None:
    """Return a malformed-call class, or None when the call's shape was fine."""
    err = result.get("error_type")
    summary = result.get("summary", "")
    try:
        args = json.loads(args_text) if args_text else {}
    except json.JSONDecodeError:
        return "args_not_json"
    if err in {"ToolArgumentsJSONError"}:
        return "args_not_json"
    if tool == "run_skill_script":
        if not isinstance(args, dict):
            return "args_not_object"
        if "skill_name" not in args or "file_path" not in args:
            if isinstance(args.get("args"), dict) and ({"skill_name", "file_path"} & set(args["args"])):
                return "skill:keys_swallowed_into_args"
            return "skill:missing_skill_name_or_file_path"
        if "positional_args" in args or (isinstance(args.get("args"), dict) and "positional_args" in args["args"]):
            return "skill:invented_positional_args"
        name = str(args.get("file_path", "")) + str(args.get("skill_name", ""))
        if re.search(r"[`」「*<|>\"]|skill_name:", name):
            return "skill:corrupted_name_tokens"
        if isinstance(args.get("args"), str):
            return "skill:args_as_string"
        if re.search(r"would not compile|unterminated string", summary):
            return "edit:escaped_code_breaks_compile"
        if re.search(r"ERROR: give --|must be a|unknown op|unrecognized arguments|required", summary):
            return "skill:wrong_arg_names"
        if re.search(r"not found in|Closest regions", summary):
            return "edit:old_text_not_found"
    if tool == "edit_file":
        if re.search(r"\\n|\\\"", args_text) and err:
            return "edit_file:literal_escapes"
        if err:
            return "edit_file:failed"
    if err == "ValidationError":
        return f"{tool}:validation_error"
    return None


def classify_command(command: str) -> str:
    c = command.strip()
    if re.search(r"\bsed\s+-i", c):
        return "sed -i"
    if "<<" in c:
        return "heredoc"
    if re.search(r"python3?\s+-c", c):
        return "python -c"
    if re.search(r"\bpytest\b", c):
        return "pytest"
    if re.match(r"(grep|rg|find|ls|cat|head|tail|wc|sed -n|nl)\b", c):
        return "read/search"
    if re.search(r"python3?\s+\S+\.py", c):
        return "run script"
    if re.match(r"git\b", c):
        return "git"
    return "other"


def analyse_run(label: str, harness: str, run: Path, tasks: dict, replay: dict) -> dict:
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    task_id = events[0].get("task_id") or run.name.split("-")[0]
    task = tasks.get(task_id, {})
    gold = set(patch_files(task.get("patch", "")))
    start = events[0]["monotonic_ns"]
    t = lambda e: (e["monotonic_ns"] - start) / 1e9
    agent_start = next((t(e) for e in events if e["event_type"] == "agent.start"), 0.0)
    outcome = {}
    if (run / "final" / "outcome.json").is_file():
        outcome = json.loads((run / "final" / "outcome.json").read_text())
    final_patch = (run / "final" / "agent.patch").read_text() if (run / "final" / "agent.patch").is_file() else ""

    calls, pending = [], {}
    models = []
    for e in events:
        kind, a = e["event_type"], e.get("attributes", {})
        if kind == "model_response":
            usage = a.get("usage") or {}
            raw = read_blob(run, e, "raw model response")
            text = thought = ""
            try:
                message = json.loads(raw)["choices"][0]["message"]
                text = message.get("content") or ""
                thought = message.get("reasoning") or message.get("reasoning_content") or ""
            except Exception:
                pass
            models.append({"t": t(e) - agent_start, "agent": a.get("agent", "single"),
                           "dur": (e.get("duration_ms") or 0) / 1000,
                           "out": usage.get("completion_tokens"), "prompt": usage.get("prompt_tokens"),
                           "reason": ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                           "text": text, "thought": thought, "tool_calls": a.get("tool_call_count", 0)})
        elif kind == "tool_call":
            pending[a.get("correlation_id")] = {"tool": a.get("tool"), "args": read_blob(run, e, "tool arguments"),
                                                "t": t(e) - agent_start, "agent": a.get("agent", "single")}
        elif kind == "tool_result":
            call = pending.pop(a.get("correlation_id"), {"tool": a.get("tool"), "args": "", "t": t(e) - agent_start})
            diff = ""
            if a.get("workspace_diff_path") and (run / a["workspace_diff_path"]).is_file():
                diff = (run / a["workspace_diff_path"]).read_text(errors="replace")
            full = read_blob(run, e, "model-visible tool result") or a.get("model_visible_summary", "")
            call.update({"error": e.get("status") == "error", "error_type": a.get("error_type"),
                         "summary": full, "changed": bool(a.get("workspace_changed")), "diff": diff})
            calls.append(call)

    # --- malformed / repeats ----------------------------------------------------------
    malformed = collections.Counter()
    for c in calls:
        m = classify_malformed(c["tool"], c["args"], {"error_type": c["error_type"], "summary": c["summary"]})
        if m:
            malformed[m] += 1
            c["malformed"] = m
    keys = [(c["tool"], hashlib.sha256(c["args"].encode()).hexdigest()) for c in calls]
    duplicate_calls = sum(n - 1 for n in collections.Counter(keys).values() if n > 1)
    max_consecutive = best = 1
    for i in range(1, len(keys)):
        best = best + 1 if keys[i] == keys[i - 1] else 1
        max_consecutive = max(max_consecutive, best)

    # --- reads ------------------------------------------------------------------------
    reads = [c for c in calls if c["tool"] == "read_file"]
    read_files = collections.Counter()
    whole = 0
    for c in reads:
        try:
            args = json.loads(c["args"])
        except Exception:
            args = {}
        read_files[args.get("filepath") or args.get("path") or "?"] += 1
        if not args.get("start_line") and not args.get("end_line"):
            whole += 1
    commands = collections.Counter()
    command_fail = collections.Counter()
    for c in calls:
        if c["tool"] == "run_command":
            try:
                cmd = json.loads(c["args"]).get("command", "")
            except Exception:
                cmd = ""
            k = classify_command(cmd)
            commands[k] += 1
            if c["error"] and k in {"sed -i", "heredoc", "python -c"} and re.search(
                    r"syntax error|unexpected|unterminated|No such file|SyntaxError|unmatched", c["summary"]):
                command_fail[k] += 1

    # --- edits, gold touch, hidden green --------------------------------------------------
    edits = [c for c in calls if c["changed"]]
    first_gold = next((c["t"] for c in edits if gold & set(patch_files(c["diff"]))), None)
    states = []
    for c in edits:
        sha = hashlib.sha256(c["diff"].encode()).hexdigest()
        states.append({"t": c["t"], "sha": sha, "tool": c["tool"], "hidden": replay.get(f"{task_id}:{sha}")})
    first_green = next((s["t"] for s in states if s["hidden"] == "PASS"), None)
    lost = bool(first_green is not None and states and states[-1]["hidden"] != "PASS")
    final_sha = hashlib.sha256(final_patch.encode()).hexdigest()
    edit_tools = collections.Counter(c["tool"] if c["tool"] != "run_command" else "run_command"
                                     for c in edits)

    # --- self-verification signals the agent saw ------------------------------------------
    agent_green = next((c["t"] for c in calls if c["t"] > (edits[0]["t"] if edits else 1e9) and (
        re.search(r"REPRO PASS|GREEN|\bSUCCESS\b|passed", c["summary"]) and not c["error"])), None)

    # --- reasoning ----------------------------------------------------------------------
    hedges = sum(len(HEDGE.findall(m["thought"])) for m in models)
    final_text = next((m["text"] for m in reversed(models) if m["text"].strip()), "")
    after_green_edits = sum(1 for s in states if first_green is not None and s["t"] > first_green)

    durations = [m for m in models if m["dur"] and m["out"]]
    return {
        "label": label, "harness": harness, "task": task_id, "run": str(run.relative_to(PROJECT)),
        "outcome": outcome.get("classification"), "submitted": any(c["tool"] == "submit_patch" for c in calls),
        "model_calls": len(models), "tool_calls": len(calls),
        "model_seconds": round(sum(m["dur"] for m in models), 1),
        "reason_tokens": sum(m["reason"] or 0 for m in models), "out_tokens": sum(m["out"] or 0 for m in models),
        "max_prompt": max((m["prompt"] or 0 for m in models), default=0),
        "calls_per_agent": dict(collections.Counter(m["agent"] for m in models)),
        "tools": dict(collections.Counter(c["tool"] for c in calls)),
        "errors": sum(c["error"] for c in calls), "malformed": dict(malformed),
        "duplicate_calls": duplicate_calls, "max_consecutive_same_call": max_consecutive,
        "reads": len(reads), "whole_file_reads": whole, "re_reads": sum(n - 1 for n in read_files.values() if n > 1),
        "commands": dict(commands), "command_syntax_failures": dict(command_fail),
        "edits": len(edits), "edit_tools": dict(edit_tools), "first_gold_touch_s": first_gold,
        "hidden_states": [(round(s["t"]), s["hidden"]) for s in states],
        "first_hidden_green_s": first_green, "green_lost": lost, "edits_after_green": after_green_edits,
        "agent_saw_green_s": agent_green,
        "final_patch_files": patch_files(final_patch), "gold_files": sorted(gold),
        "scratch_in_patch": [f for f in patch_files(final_patch) if f not in gold and SCRATCH.search(f)
                             and not f.startswith("tests/")],
        "compactions": sum(e["event_type"] == "context_compaction" for e in events),
        "nudges": sum(e["event_type"] == "harness_intervention" for e in events),
        "termination": next((e["attributes"].get("termination_reason") for e in events
                             if e["event_type"] == "agent_error"), None),
        "hedge_words": hedges, "final_claims_success": bool(CLAIM.search(final_text)),
        "final_text": final_text[:300],
        "_models": [{k: m[k] for k in ("t", "agent", "dur", "out", "prompt", "reason")} for m in durations],
    }


def fit_latency(rows: list[dict]) -> dict:
    """Least squares: dur = a + b*out + c*prompt (pure python, 3x3 normal equations)."""
    pts = [(m["out"], m["prompt"] or 0, m["dur"]) for r in rows for m in r["_models"] if m["out"]]
    n = len(pts)
    X = [[1.0, o, p] for o, p, _ in pts]
    y = [d for *_, d in pts]
    XtX = [[sum(X[k][i] * X[k][j] for k in range(n)) for j in range(3)] for i in range(3)]
    Xty = [sum(X[k][i] * y[k] for k in range(n)) for i in range(3)]
    # Gaussian elimination
    A = [row[:] + [v] for row, v in zip(XtX, Xty)]
    for i in range(3):
        piv = A[i][i]
        A[i] = [v / piv for v in A[i]]
        for j in range(3):
            if j != i:
                f = A[j][i]
                A[j] = [vj - f * vi for vj, vi in zip(A[j], A[i])]
    a, b, c = (A[i][3] for i in range(3))
    pred = [a + b * o + c * p for o, p, _ in pts]
    ss_res = sum((yi - pi) ** 2 for yi, pi in zip(y, pred))
    mean = statistics.fmean(y)
    ss_tot = sum((yi - mean) ** 2 for yi in y)
    reasoning_share = sum(m["reason"] or 0 for r in rows for m in r["_models"]) / max(
        1, sum(m["out"] or 0 for r in rows for m in r["_models"]))
    long_calls = [m for r in rows for m in r["_models"] if m["out"] >= 500]
    return {"n": n, "a_seconds": round(a, 2), "b_ms_per_out_token": round(b * 1000, 2),
            "c_ms_per_prompt_token": round(c * 1000, 4), "r2": round(1 - ss_res / ss_tot, 3),
            "reasoning_share_of_output_tokens": round(reasoning_share, 3),
            "share_of_model_time_in_calls_ge_500_out": round(
                sum(m["dur"] for m in long_calls) / max(1e-9, sum(m["dur"] for r in rows for m in r["_models"])), 3),
            "median_call_s": round(statistics.median(m["dur"] for r in rows for m in r["_models"]), 2)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", type=Path, default=HERE / "aggregate.json")
    args = parser.parse_args()
    tasks = load_tasks()
    cache = HERE / "replay_cache.json"
    replay = json.loads(cache.read_text()) if cache.is_file() else {}
    rows = [analyse_run(label, harness, run, tasks, replay) for label, harness, run in discover()]
    fit = fit_latency(rows)
    out = {"latency_fit": fit, "runs": [{k: v for k, v in r.items() if k != "_models"} for r in rows]}
    args.json.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(fit, indent=1))
    for r in rows:
        print(f"{r['label']:6} {r['harness'][:12]:12} {r['task']:14} {str(r['outcome']):11} sub={int(r['submitted'])} "
              f"calls={r['model_calls']:3} msec={r['model_seconds']:6} reasonTok={r['reason_tokens']:5} "
              f"err={r['errors']:2} malf={sum(r['malformed'].values()):2} dup={r['duplicate_calls']:2} "
              f"reads={r['reads']:2}/whole={r['whole_file_reads']:2}/re={r['re_reads']:2} edits={r['edits']:2} "
              f"gold@{r['first_gold_touch_s'] and round(r['first_gold_touch_s'])} "
              f"green@{r['first_hidden_green_s'] and round(r['first_hidden_green_s'])} lost={int(r['green_lost'])} "
              f"scratch={r['scratch_in_patch']} claim={int(r['final_claims_success'])}")


if __name__ == "__main__":
    main()
