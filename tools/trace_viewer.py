"""Index sealed agent traces under work/ and serve the generic trajectory viewer (index.html).

Usage:
    python tools/trace_viewer.py index            # (re)build work/.trace_viewer/
    python tools/trace_viewer.py serve [--port N] # reindex, then serve the project at http://localhost:N/
    python tools/trace_viewer.py ingest <kaggle-output.zip|dir> --as v20

Nothing here knows about specific tasks, versions, tools, event types or outcomes: every run
directory containing a run_manifest.json is discovered, and unknown values pass through as-is.
Generated files live in work/.trace_viewer/ so sealed run directories are never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import re
import shutil
import sys
import threading
import webbrowser
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from agent_harness.reporting import _artifact_path, load_events  # noqa: E402

VIEWER_SCHEMA = "1.0"
OUT_DIRNAME = ".trace_viewer"
INLINE_LIMIT = 64_000  # bytes; larger blobs stay as lazy refs for the browser to fetch
TEST_COMMAND_RE = re.compile(r"\b(pytest|unittest|tox|nox|python\d?(\.\d+)? -m (pytest|unittest))\b|\btest_\w+\.py\b")
PATH_TOKEN_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:py|pyi|md|rst|txt|toml|cfg|ini|ya?ml|json))(?![\w/-])")
SCRATCH_PATH_RE = re.compile(r"(^|/)(tmp|scratch)/|(^|/)(repro\w*|reproduce\w*|debug\w*|scratch\w*)\.py$")
COMMAND_TOOLS = {"run_command"}
READ_TOOLS = {"read_file"}
WRITE_TOOLS = {"edit_file", "write_file"}


# --------------------------------------------------------------------------- helpers

def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _ref_by_purpose(event: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    for ref in event.get("artifact_refs") or []:
        if str(ref.get("purpose", "")).startswith(prefix):
            return ref
    return None


def _load_ref(run_root: Path, ref: dict[str, Any] | None, *, as_json: bool = False, limit: int = INLINE_LIMIT) -> Any:
    if not ref:
        return None
    if limit and int(ref.get("byte_length") or 0) > limit:
        return None
    try:
        text = _artifact_path(run_root, ref).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    if as_json:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


def _slim_ref(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    if not ref:
        return None
    return {k: ref.get(k) for k in ("purpose", "relative_path", "byte_length", "media_type")}


def normalize_path(path: str, workspace: str | None = None) -> str:
    path = path.strip().strip("'\"")
    if workspace and path.startswith(workspace):
        path = path[len(workspace):]
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/") if workspace else path


# --------------------------------------------------------------------------- patches

def parse_patch(text: str | None) -> list[dict[str, Any]]:
    """Split a unified diff into files with hunks; tolerant of both git and plain diffs."""
    files: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    hunk: dict[str, Any] | None = None
    old_path = None
    for line in (text or "").splitlines():
        if line.startswith("diff --git "):
            current, hunk, old_path = None, None, None
            continue
        if line.startswith("--- "):
            old_path = line[4:].split("\t")[0].strip()
            continue
        if line.startswith("+++ ") and old_path is not None:
            new_path = line[4:].split("\t")[0].strip()
            chosen = new_path if new_path != "/dev/null" else old_path
            chosen = re.sub(r"^[ab]/", "", chosen)
            current = {
                "path": chosen,
                "status": "added" if old_path == "/dev/null" else "deleted" if new_path == "/dev/null" else "modified",
                "added": 0, "removed": 0, "hunks": [],
            }
            files.append(current)
            old_path, hunk = None, None
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)", line)
            hunk = {"old_start": int(match.group(1)) if match else 0,
                    "new_start": int(match.group(2)) if match else 0,
                    "header": (match.group(3).strip() if match else ""),
                    "removed": [], "added": [], "context": []}
            current["hunks"].append(hunk)
        elif hunk is not None and line[:1] in "+- ":
            key = {"+": "added", "-": "removed", " ": "context"}[line[:1]]
            hunk[key].append(line[1:])
            if key in ("added", "removed"):
                current[key] += 1
    return files


def patch_files(text: str | None) -> list[str]:
    return [f["path"] for f in parse_patch(text)]


# --------------------------------------------------------------------------- code graph

def _module_candidates(path: str) -> list[str]:
    if not path.endswith(".py"):
        return []
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    candidates = [module]
    if module.startswith("src."):
        candidates.append(module[4:])
    return candidates


def load_graph(graphs_dir: Path, task_id: str, repo: str | None, base_commit: str | None) -> dict[str, Any] | None:
    names = [f"{task_id}.json"]
    if repo and base_commit:
        names.append(f"{repo.split('/')[-1]}_{base_commit}.json")
    for name in names:
        path = graphs_dir / name
        if path.is_file() and path.stat().st_size > 0:
            graph = _read_json(path)
            if isinstance(graph, dict) and graph.get("nodes"):
                return graph
    return None


def graph_functions_for_patch(graph: dict[str, Any], patch: list[dict[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
    """Map patch hunks onto graph nodes (functions/classes) by module prefix and source-line overlap."""
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or graph.get("links") or []
    found: dict[str, dict[str, Any]] = {}
    for pfile in patch:
        modules = _module_candidates(pfile["path"])
        if not modules:
            continue
        in_module = [n for n in nodes if any(str(n.get("id", "")) == m or str(n.get("id", "")).startswith(m + ".") for m in modules)]
        for hunk in pfile["hunks"]:
            probe = [l.strip() for l in hunk["removed"] + hunk["context"] if len(l.strip()) >= 6]
            best, best_score = [], 0
            for node in in_module:
                text = node.get("text") or ""
                score = sum(1 for l in probe if l in text) + sum(3 for l in hunk["removed"] if l.strip() and l.strip() in text)
                if score > best_score:
                    best, best_score = [node], score
                elif score and score == best_score:
                    best.append(node)
            for node in best[:3]:
                nid = str(node.get("id"))
                found.setdefault(nid, {"id": nid, "file": pfile["path"], "text": (node.get("text") or "")[:4000]})
    if not found:
        return []
    for nid, entry in found.items():
        entry["callers"] = sorted({e["source"] for e in edges if e.get("target") == nid})[:15]
        entry["callees"] = sorted({e["target"] for e in edges if e.get("source") == nid})[:15]
    return list(found.values())[:limit]


# --------------------------------------------------------------------------- run joining

def _files_from_command(command: str) -> list[str]:
    return sorted({m.group(1) for m in PATH_TOKEN_RE.finditer(command or "")})


def build_run(run_root: Path, project: Path, version: str) -> dict[str, Any]:
    """Join events + artifacts into one per-run document of turns, lifecycle events and summaries."""
    events = load_events(run_root)
    manifest = _read_json(run_root / "run_manifest.json", {}) or {}
    summary = _read_json(run_root / "final" / "summary.json", {}) or {}
    outcome = _read_json(run_root / "final" / "outcome.json", {}) or {}
    public = _read_json(run_root / "task" / "public.json", {}) or {}
    config = _read_json(run_root / "effective_config.json", {}) or {}
    leak = _read_json(run_root / "leak_audit.json", {}) or {}
    validation = _read_json(run_root / "trace_validation.json", manifest.get("trace_validation") or {}) or {}
    patch_text = (run_root / "final" / "agent.patch").read_text(encoding="utf-8", errors="replace") if (run_root / "final" / "agent.patch").is_file() else ""

    t0 = next((e.get("monotonic_ns") for e in events if e.get("monotonic_ns") is not None), 0) or 0
    turns: list[dict[str, Any]] = []
    lifecycle: list[dict[str, Any]] = []
    by_request: dict[str, dict[str, Any]] = {}
    by_tool: dict[str, dict[str, Any]] = {}
    pending_context: dict[str, Any] | None = None
    workspace = None
    verification: dict[str, Any] = {}
    event_types: Counter = Counter()

    def t_rel(event: dict[str, Any]) -> float | None:
        ns = event.get("monotonic_ns")
        return round((ns - t0) / 1e9, 3) if ns is not None else None

    for event in events:
        etype = str(event.get("event_type"))
        attrs = event.get("attributes") or {}
        event_types[etype] += 1
        base = {"seq": event.get("sequence"), "t": t_rel(event), "status": event.get("status"),
                "duration_ms": event.get("duration_ms")}
        if etype == "context_measurement":
            pending_context = {**attrs, "budget": event.get("budget_after") or event.get("budget_before")}
        elif etype == "model_request":
            turn = {
                "index": len(turns), "turn": attrs.get("turn"), "attempt": attrs.get("attempt"),
                "correlation_id": attrs.get("correlation_id"), "t_start": base["t"], "context": pending_context,
                "request": {k: v for k, v in attrs.items() if k not in ("correlation_id", "turn", "attempt")},
                "request_refs": [_slim_ref(r) for r in event.get("artifact_refs") or []],
                "settings": _load_ref(run_root, _ref_by_purpose(event, "effective model request settings"), as_json=True),
                "response": None, "tool_calls": [], "status": "ok",
            }
            messages_ref = _ref_by_purpose(event, "complete outbound model messages")
            messages = _load_ref(run_root, messages_ref, as_json=True, limit=0) if messages_ref else None
            turn["messages_count"] = len(messages) if isinstance(messages, list) else None
            if turns and isinstance(messages, list) and turns[-1].get("messages_count") is not None:
                turn["new_messages"] = messages[turns[-1]["messages_count"]:]
            elif isinstance(messages, list):
                turn["new_messages"] = messages
            pending_context = None
            turns.append(turn)
            by_request[str(attrs.get("correlation_id"))] = turn
        elif etype == "model_response":
            turn = by_request.get(str(attrs.get("correlation_id"))) or (turns[-1] if turns else None)
            if turn is None:
                lifecycle.append({**base, "type": etype, "attributes": attrs})
                continue
            raw = _load_ref(run_root, _ref_by_purpose(event, "raw model response"), as_json=True, limit=0)
            message = {}
            if isinstance(raw, dict) and raw.get("choices"):
                message = (raw["choices"][0] or {}).get("message") or {}
            error_text = _load_ref(run_root, _ref_by_purpose(event, "model request error"))
            turn["status"] = event.get("status") or "ok"
            turn["t_end"] = base["t"]
            turn["duration_ms"] = event.get("duration_ms")
            turn["budget"] = event.get("budget_after")
            turn["response"] = {
                "finish_reason": attrs.get("finish_reason"), "usage": attrs.get("usage"),
                "reasoning": message.get("reasoning") or message.get("reasoning_content"),
                "content": message.get("content"),
                "tool_calls": [
                    {"id": c.get("id"), "name": (c.get("function") or {}).get("name"),
                     "arguments": (c.get("function") or {}).get("arguments")}
                    for c in message.get("tool_calls") or []
                ],
                "error_type": attrs.get("error_type"), "error": error_text,
                "model": raw.get("model") if isinstance(raw, dict) else None,
                "extra": {k: v for k, v in attrs.items() if k not in ("finish_reason", "usage", "correlation_id", "turn", "response_sha256", "tool_call_count")},
            }
            turn["response_ref"] = _slim_ref(_ref_by_purpose(event, "raw model response"))
        elif etype == "tool_call":
            turn = turns[-1] if turns else None
            args = _load_ref(run_root, _ref_by_purpose(event, "tool arguments"), as_json=True)
            workspace = attrs.get("working_directory") or workspace
            call = {"seq": base["seq"], "t": base["t"], "id": attrs.get("correlation_id"), "tool": attrs.get("tool"),
                    "arguments": args, "result": None}
            if turn is None:
                lifecycle.append({**base, "type": etype, "attributes": attrs})
                continue
            turn["tool_calls"].append(call)
            by_tool[str(attrs.get("correlation_id"))] = call
        elif etype == "tool_result":
            call = by_tool.get(str(attrs.get("correlation_id")))
            if call is None:
                lifecycle.append({**base, "type": etype, "attributes": attrs})
                continue
            diff_ref = _ref_by_purpose(event, "workspace diff")
            call["result"] = {
                "status": event.get("status"), "t": base["t"], "duration_ms": event.get("duration_ms"),
                "error_type": attrs.get("error_type"),
                "model_visible": _load_ref(run_root, _ref_by_purpose(event, "model-visible tool result"), as_json=True),
                "model_visible_ref": _slim_ref(_ref_by_purpose(event, "model-visible tool result")),
                "output_ref": _slim_ref(_ref_by_purpose(event, "complete command output")),
                "workspace_changed": attrs.get("workspace_changed"),
                "diff": _load_ref(run_root, diff_ref), "diff_ref": _slim_ref(diff_ref),
                "budget": event.get("budget_after"),
                "extra": {k: v for k, v in attrs.items() if k not in ("correlation_id", "tool", "model_visible_summary", "error_type", "workspace_changed", "workspace_diff_path", "result_sha256", "workspace_hash_before", "workspace_hash_after")},
            }
        else:
            entry = {**base, "type": etype, "attributes": attrs,
                     "refs": [_slim_ref(r) for r in event.get("artifact_refs") or []]}
            lifecycle.append(entry)
            if etype == "verification_result":
                ref = next((r for r in event.get("artifact_refs") or [] if "verification output" in str(r.get("purpose"))), None)
                verification[str(attrs.get("phase") or len(verification))] = {**attrs, "log_ref": _slim_ref(ref)}

    workspace_prefix = (workspace.rstrip("/") + "/") if workspace else None
    files_read, files_edited, files_edit_failed, files_mentioned, commands = set(), set(), set(), set(), []
    for turn in turns:
        for call in turn["tool_calls"]:
            args = call["arguments"] if isinstance(call["arguments"], dict) else {}
            tool = call["tool"]
            target = args.get("filepath") or args.get("path") or args.get("file")
            if target:
                target = normalize_path(str(target), workspace_prefix)
                if tool in WRITE_TOOLS:
                    result = call.get("result") or {}
                    ok = result.get("status") == "ok" and not result.get("error_type")
                    (files_edited if ok else files_edit_failed).add(target)
                else:
                    files_read.add(target)
                call["file"] = target
            if tool in COMMAND_TOOLS or "command" in args:
                command = str(args.get("command") or "")
                commands.append(command)
                call["files"] = [normalize_path(p, workspace_prefix) for p in _files_from_command(command)]
                files_mentioned.update(call["files"])

    tools = Counter(c["tool"] for t in turns for c in t["tool_calls"])
    usage_prompt = [((t.get("response") or {}).get("usage") or {}).get("prompt_tokens") for t in turns]
    context_window = next((t["context"].get("context_window") for t in turns if t.get("context")), None)
    agent = summary.get("agent") or {}
    run_key = f"{version}__{manifest.get('run_id') or run_root.name}"
    return {
        "viewer_schema": VIEWER_SCHEMA,
        "key": run_key,
        "version": version,
        "path": _rel(run_root, project),
        "experiment_id": manifest.get("experiment_id") or summary.get("experiment_id"),
        "run_id": manifest.get("run_id") or run_root.name,
        "task_id": manifest.get("task_id") or summary.get("task_id") or public.get("task_id"),
        "trace_id": manifest.get("trace_id"),
        "sealed_utc": manifest.get("sealed_utc"),
        "outcome": manifest.get("outcome") or summary.get("outcome") or outcome.get("classification") or "UNKNOWN",
        "outcome_definition": outcome.get("definition"),
        "termination_reason": summary.get("termination_reason"),
        "agent": agent,
        "agent_error": agent.get("error"),
        "patch": patch_text,
        "patch_files": patch_files(patch_text),
        "baseline": summary.get("baseline") or _read_json(run_root / "final" / "baseline.json"),
        "verification": verification,
        "public_task": public,
        "config": config,
        "configuration_sha256": manifest.get("configuration_sha256") or config.get("configuration_sha256"),
        "prompt_sha256": hashlib.sha256(str((config.get("submission") or {}).get("instruction") or "").encode()).hexdigest()[:12] if config else None,
        "model": ((config.get("run") or {}).get("model") or {}).get("name") or (config.get("submission") or {}).get("model"),
        "validation": validation,
        "leak_audit": {"passed": leak.get("passed"), "hits": len(leak.get("hits") or []), "records_checked": leak.get("records_checked")},
        "workspace": workspace,
        "event_types": dict(event_types),
        "turns": turns,
        "lifecycle": lifecycle,
        "files_read": sorted(files_read),
        "files_edited": sorted(files_edited),
        "files_edit_failed": sorted(files_edit_failed - files_edited),
        "files_mentioned": sorted(files_mentioned - files_read - files_edited),
        "tools": dict(tools),
        "prompt_tokens": usage_prompt,
        "context_window": context_window,
        "commands": commands,
    }


# --------------------------------------------------------------------------- behaviour flags

def behavior_flags(run: dict[str, Any], gold_files: list[str] | None) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []

    def add(code: str, detail: str, severity: str = "warn") -> None:
        flags.append({"code": code, "detail": detail, "severity": severity})

    if run["termination_reason"] and run["termination_reason"] != "submitted_patch":
        add("abnormal_termination", f"terminated: {run['termination_reason']}", "bad")
    counts = Counter(c.strip() for c in run["commands"] if c.strip())
    repeats = {c: n for c, n in counts.items() if n >= 3}
    if repeats:
        cmd, n = max(repeats.items(), key=lambda kv: kv[1])
        add("repeated_command", f"{n}× {cmd[:80]}")
    streak = best = 0
    for turn in run["turns"]:
        for call in turn["tool_calls"]:
            failed = bool(call.get("result") and (call["result"].get("status") != "ok" or call["result"].get("error_type")))
            streak = streak + 1 if failed else 0
            best = max(best, streak)
    if best >= 3:
        add("error_streak", f"{best} consecutive failing tool calls")
    edit_calls = [c for t in run["turns"] for c in t["tool_calls"] if c["tool"] in WRITE_TOOLS]
    failed_edits = [c for c in edit_calls if (c.get("result") or {}).get("error_type")]
    if len(failed_edits) >= 3:
        add("failed_edits", f"{len(failed_edits)}/{len(edit_calls)} edit calls failed", "bad" if len(failed_edits) >= len(edit_calls) / 2 else "warn")
    scratch = [p for p in run["patch_files"] if SCRATCH_PATH_RE.search(p)]
    if scratch:
        add("scratch_files_in_patch", ", ".join(scratch[:4]), "bad" if len(scratch) == len(run["patch_files"]) else "warn")
    edited_at = None
    tested_after_edit = False
    for turn in run["turns"]:
        for call in turn["tool_calls"]:
            if call["tool"] in WRITE_TOOLS or (call.get("result") or {}).get("workspace_changed"):
                edited_at = edited_at if edited_at is not None else turn["index"]
            elif edited_at is not None and call["tool"] in COMMAND_TOOLS:
                command = str((call.get("arguments") or {}).get("command") or "")
                if TEST_COMMAND_RE.search(command) or "python" in command:
                    tested_after_edit = True
    if edited_at is not None and not tested_after_edit:
        add("no_check_after_edit", "never ran tests or python after first edit")
    if not run["patch"].strip():
        add("empty_patch", "final patch is empty", "bad")
    if run["agent"].get("patch_submitted_explicitly") is False:
        add("implicit_submit", "patch captured without submit_patch")
    if gold_files:
        touched = set(run["files_read"]) | set(run["files_edited"]) | set(run["files_edit_failed"]) | set(run["files_mentioned"])
        if not touched & set(gold_files):
            add("never_saw_gold_file", "never read or referenced any file the gold patch changes", "bad")
        elif run["patch_files"] and not set(run["patch_files"]) & set(gold_files):
            add("edited_wrong_files", "patch touches none of the gold files", "bad")
        extra = sorted(set(run["patch_files"]) - set(gold_files))
        if extra and set(run["patch_files"]) & set(gold_files):
            add("extra_files_in_patch", ", ".join(extra[:4]))
    window = run.get("context_window")
    peak = max([p for p in run["prompt_tokens"] if p] or [0])
    if window and peak / window >= 0.75:
        add("context_pressure", f"peak prompt {peak:,} / {window:,} tokens ({peak / window:.0%})", "bad" if peak / window >= 0.9 else "warn")
    budget = run["agent"].get("budget") or {}
    if budget.get("max_tool_calls") and budget.get("tool_calls_used", 0) / budget["max_tool_calls"] >= 0.9:
        add("tool_budget_pressure", f"{budget['tool_calls_used']}/{budget['max_tool_calls']} tool calls")
    if budget.get("max_time_minutes") and budget.get("time_seconds_remaining") is not None and budget["time_seconds_remaining"] < 0.1 * budget["max_time_minutes"] * 60:
        add("time_budget_pressure", f"{budget['time_seconds_remaining']:.0f}s left")
    if any((t.get("response") or {}).get("finish_reason") == "stop" and not (t["response"].get("tool_calls")) for t in run["turns"]):
        add("stopped_without_tool", "a model turn ended with prose instead of a tool call", "info")
    return flags


# --------------------------------------------------------------------------- index build

def discover_runs(work: Path) -> Iterable[tuple[str, Path]]:
    for manifest in sorted(work.glob("*/trace_runs/**/run_manifest.json")):
        if OUT_DIRNAME in manifest.parts:
            continue
        version = manifest.relative_to(work).parts[0]
        yield version, manifest.parent


def completeness_warnings(work: Path, indexed: set[tuple[str, str]]) -> list[dict[str, str]]:
    """Runs a batch_summary.json promises, or run dirs without a manifest, that did not make it into the index."""
    warnings: list[dict[str, str]] = []
    for batch in sorted(work.glob("*/trace_runs/**/batch_summary.json")):
        if OUT_DIRNAME in batch.parts:
            continue
        version = batch.relative_to(work).parts[0]
        try:
            results = json.loads(batch.read_text(encoding="utf-8")).get("results") or []
        except (OSError, ValueError, AttributeError):
            continue
        for row in results:
            run_id = str(row.get("run_id") or "")
            if run_id and (version, run_id) not in indexed:
                warnings.append({"version": version, "run_id": run_id, "task_id": str(row.get("task_id") or ""),
                                 "reason": f"listed in {_rel(batch, work.parent)} but not indexed (missing or incomplete run directory)"})
    listed = {(w["version"], w["run_id"]) for w in warnings}
    for experiment in sorted(work.glob("*/trace_runs/*/")):
        if OUT_DIRNAME in experiment.parts:
            continue
        version = experiment.relative_to(work).parts[0]
        for run_dir in sorted(p for p in experiment.iterdir() if p.is_dir()):
            if (version, run_dir.name) in indexed or (version, run_dir.name) in listed:
                continue
            if (run_dir / "events.jsonl").exists() or (run_dir / "artifacts").is_dir():
                warnings.append({"version": version, "run_id": run_dir.name, "task_id": "",
                                 "reason": f"{_rel(run_dir, work.parent)} has no readable run_manifest.json (partial extraction?)"})
    return warnings


def load_tasks(tasks_path: Path) -> dict[str, dict[str, Any]]:
    tasks: dict[str, dict[str, Any]] = {}
    if not tasks_path.is_file():
        return tasks
    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            tasks[str(row.get("instance_id") or row.get("task_id"))] = row
    return tasks


def _search_entries(run: dict[str, Any]) -> Iterable[dict[str, Any]]:
    for turn in run["turns"]:
        response = turn.get("response") or {}
        for kind in ("reasoning", "content"):
            if response.get(kind):
                yield {"run": run["key"], "step": turn["index"], "kind": kind, "text": str(response[kind])[:2000]}
        if response.get("error"):
            yield {"run": run["key"], "step": turn["index"], "kind": "error", "text": str(response["error"])[:2000]}
        for call in turn["tool_calls"]:
            yield {"run": run["key"], "step": turn["index"], "kind": call["tool"] or "tool",
                   "text": json.dumps(call.get("arguments"), ensure_ascii=False)[:2000]}
            summary = (call.get("result") or {}).get("model_visible")
            if summary:
                yield {"run": run["key"], "step": turn["index"], "kind": "result",
                       "text": (summary if isinstance(summary, str) else json.dumps(summary, ensure_ascii=False))[:1500]}


def build_index(project: Path = PROJECT, *, quiet: bool = False) -> dict[str, Any]:
    work = project / "work"
    out = work / OUT_DIRNAME
    (out / "runs").mkdir(parents=True, exist_ok=True)
    (out / "tasks").mkdir(parents=True, exist_ok=True)
    dataset = load_tasks(project / "data" / "tasks.jsonl")
    graph_dir = project / "data" / "graphs"

    catalog_runs, search, task_rows = [], [], {}
    seen_runs, indexed = set(), set()
    for version, run_root in discover_runs(work):
        try:
            run = build_run(run_root, project, version)
        except Exception as exc:  # a malformed run should not hide the others
            print(f"  skip {run_root}: {exc}", file=sys.stderr)
            continue
        task_id = str(run["task_id"])
        gold = dataset.get(task_id)
        if task_id not in task_rows:
            task_rows[task_id] = _build_task(task_id, gold, run, graph_dir, out)
        gold_files = task_rows[task_id]["gold_files"] if gold else None
        run["flags"] = behavior_flags(run, gold_files)
        run["gold_overlap"] = _gold_overlap(run, gold_files) if gold_files else None
        if gold:
            graph = load_graph(graph_dir, task_id, gold.get("repo"), gold.get("base_commit"))
            run["agent_functions"] = graph_functions_for_patch(graph, parse_patch(run["patch"]), limit=20) if graph else []
        _write_json(out / "runs" / f"{run['key']}.json", run)
        seen_runs.add(f"{run['key']}.json")
        indexed.add((version, str(run["run_id"])))
        indexed.add((version, run_root.name))
        search.extend(_search_entries(run))
        agent = run["agent"]
        budget = agent.get("budget") or {}
        catalog_runs.append({
            "key": run["key"], "version": version, "experiment_id": run["experiment_id"], "run_id": run["run_id"],
            "task_id": task_id, "repo": (gold or {}).get("repo") or run["public_task"].get("repository"),
            "outcome": run["outcome"], "termination_reason": run["termination_reason"], "sealed_utc": run["sealed_utc"],
            "model": run["model"], "configuration_sha256": run["configuration_sha256"], "prompt_sha256": run["prompt_sha256"],
            "turns": len(run["turns"]), "tools": run["tools"],
            "tool_errors": sum(1 for t in run["turns"] for c in t["tool_calls"] if (c.get("result") or {}).get("error_type")),
            "model_calls": agent.get("model_calls"), "prompt_tokens_total": agent.get("prompt_tokens_total"),
            "prompt_tokens_max": agent.get("prompt_tokens_max"), "completion_tokens_total": agent.get("completion_tokens_total"),
            "elapsed_seconds": budget.get("agent_elapsed_seconds"), "tool_calls_used": budget.get("tool_calls_used"),
            "max_tool_calls": budget.get("max_tool_calls"), "max_time_minutes": budget.get("max_time_minutes"),
            "context_window": run["context_window"],
            "patch_size": len(run["patch"]), "patch_files": run["patch_files"],
            "valid": (run["validation"] or {}).get("valid"), "leak_passed": run["leak_audit"]["passed"],
            "flags": [f["code"] for f in run["flags"]], "gold_overlap": run["gold_overlap"],
            "path": run["path"],
        })
        if not quiet:
            print(f"  {version:<22} {task_id:<18} {run['outcome']:<12} {len(run['turns']):>3} turns  flags={','.join(f['code'] for f in run['flags']) or '-'}")

    for stale in (out / "runs").glob("*.json"):
        if stale.name not in seen_runs:
            stale.unlink()
    versions = sorted({r["version"] for r in catalog_runs}, key=_natural_key)
    warnings = completeness_warnings(work, indexed)
    catalog = {
        "viewer_schema": VIEWER_SCHEMA,
        "generated_from": "work/*/trace_runs/**/run_manifest.json",
        "versions": versions,
        "outcomes": sorted({r["outcome"] for r in catalog_runs}),
        "tools": sorted({t for r in catalog_runs for t in r["tools"]}),
        "flags": sorted({f for r in catalog_runs for f in r["flags"]}),
        "runs": catalog_runs,
        "tasks": {tid: {k: v for k, v in row.items() if k in ("task_id", "repo", "created_at", "has_gold", "gold_files", "test_files", "title")} for tid, row in task_rows.items()},
        "dataset_size": len(dataset),
        "warnings": warnings,
    }
    _write_json(out / "catalog.json", catalog)
    _write_json(out / "search.json", search)
    annotations = out / "annotations.json"
    if not annotations.exists():
        _write_json(annotations, {})
    if not quiet:
        for warning in warnings:
            print(f"  WARNING {warning['version']} {warning['run_id']}: {warning['reason']}", file=sys.stderr)
        print(f"indexed {len(catalog_runs)} runs, {len(task_rows)} tasks, {len(versions)} versions -> {_rel(out, project)}/")
    return catalog


def _natural_key(text: str) -> list[Any]:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", text)]


def _gold_overlap(run: dict[str, Any], gold_files: list[str]) -> dict[str, Any]:
    gold = set(gold_files)
    first_seen = None
    for turn in run["turns"]:
        for call in turn["tool_calls"]:
            touched = {call.get("file")} | set(call.get("files") or [])
            if touched & gold:
                first_seen = turn["index"]
                break
        if first_seen is not None:
            break
    return {
        "gold": sorted(gold),
        "read": sorted(gold & set(run["files_read"])),
        "edited": sorted(gold & set(run["files_edited"])),
        "edit_failed": sorted(gold & set(run["files_edit_failed"])),
        "patched": sorted(gold & set(run["patch_files"])),
        "mentioned": sorted(gold & set(run["files_mentioned"])),
        "first_seen_step": first_seen,
    }


def _build_task(task_id: str, gold: dict[str, Any] | None, run: dict[str, Any], graph_dir: Path, out: Path) -> dict[str, Any]:
    statement = (gold or {}).get("problem_statement") or run["public_task"].get("problem_statement") or ""
    row: dict[str, Any] = {
        "task_id": task_id,
        "repo": (gold or {}).get("repo") or run["public_task"].get("repository"),
        "created_at": (gold or {}).get("created_at"),
        "base_commit": (gold or {}).get("base_commit"),
        "title": statement.strip().splitlines()[0][:140] if statement.strip() else task_id,
        "problem_statement": statement,
        "hints": (gold or {}).get("hints_text") or run["public_task"].get("hints"),
        "has_gold": bool(gold),
        "gold_patch": (gold or {}).get("patch"),
        "test_patch": (gold or {}).get("test_patch"),
        "gold_files": patch_files((gold or {}).get("patch")),
        "test_files": patch_files((gold or {}).get("test_patch")),
        "gold_functions": [],
    }
    if gold:
        graph = load_graph(graph_dir, task_id, gold.get("repo"), gold.get("base_commit"))
        if graph:
            row["gold_functions"] = graph_functions_for_patch(graph, parse_patch(gold.get("patch")))
            row["graph_nodes"] = len(graph.get("nodes") or [])
    _write_json(out / "tasks" / f"{task_id}.json", row)
    return row


# --------------------------------------------------------------------------- server

class ViewerHandler(http.server.SimpleHTTPRequestHandler):
    """Static files from the project root, plus POST /api/annotations to persist notes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(PROJECT), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_POST(self) -> None:
        if self.path.rstrip("/") == "/api/annotations":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("annotations must be an object")
                _write_json(PROJECT / "work" / OUT_DIRNAME / "annotations.json", data)
                self._reply(200, {"ok": True})
            except ValueError as exc:
                self._reply(400, {"ok": False, "error": str(exc)})
        elif self.path.rstrip("/") == "/api/reindex":
            catalog = build_index(PROJECT, quiet=True)
            self._reply(200, {"ok": True, "runs": len(catalog["runs"])})
        else:
            self._reply(404, {"ok": False})

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def serve(port: int, open_browser: bool) -> None:
    build_index(PROJECT)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), ViewerHandler)
    url = f"http://localhost:{port}/index.html"
    print(f"serving {PROJECT} -> {url}  (Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def ingest(source: Path, name: str) -> Path:
    """Copy a Kaggle output (zip or directory containing trace_runs/) into work/<name>/ and reindex."""
    target = PROJECT / "work" / (name if name.startswith("kaggle-") else f"kaggle-trace-{name}")
    if target.exists():
        raise SystemExit(f"{target} already exists")
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            archive.extractall(target)
    elif source.is_dir():
        shutil.copytree(source, target)
    else:
        raise SystemExit(f"not a zip or directory: {source}")
    for nested in target.rglob("trace_diagnostics.zip"):
        with zipfile.ZipFile(nested) as archive:
            archive.extractall(nested.parent)
    if not any(target.glob("**/run_manifest.json")):
        print(f"warning: no run_manifest.json found under {target}", file=sys.stderr)
    build_index(PROJECT)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("index", help="rebuild work/.trace_viewer/")
    serve_parser = sub.add_parser("serve", help="reindex and serve the viewer")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--no-open", action="store_true")
    ingest_parser = sub.add_parser("ingest", help="copy a Kaggle output into work/ and reindex")
    ingest_parser.add_argument("source", type=Path)
    ingest_parser.add_argument("--as", dest="name", required=True)
    args = parser.parse_args(argv)
    if args.command == "index":
        build_index(PROJECT)
    elif args.command == "serve":
        serve(args.port, not args.no_open)
    elif args.command == "ingest":
        print(f"ingested -> {ingest(args.source, args.name)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
