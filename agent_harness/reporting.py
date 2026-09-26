from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .schemas import SCHEMA_VERSION
from .tracing import TRACE_ID_RE, SPAN_ID_RE, atomic_json, atomic_write, file_manifest, sha256_bytes, utc_now


def load_events(run_root: Path | str) -> list[dict[str, Any]]:
    path = Path(run_root) / "events.jsonl"
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid events.jsonl line {line_number}: {exc}") from exc
    return rows


def _artifact_path(run_root: Path, reference: dict[str, Any]) -> Path:
    relative = Path(str(reference.get("relative_path", "")))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe artifact path: {relative}")
    path = (run_root / relative).resolve()
    if run_root.resolve() not in path.parents:
        raise ValueError(f"Artifact escapes run root: {relative}")
    return path


def validate_trace(run_root: Path | str, *, require_complete: bool = True) -> dict[str, Any]:
    root = Path(run_root)
    events = load_events(root)
    errors: list[str] = []
    warnings: list[str] = []
    if not events:
        errors.append("events.jsonl is empty")
    expected_sequence = 1
    trace_ids: set[str] = set()
    run_ids: set[str] = set()
    seen_spans: set[str] = set()
    parent_spans: set[str] = set()
    started: dict[str, str] = {}
    ended: set[str] = set()
    requests: defaultdict[str, int] = defaultdict(int)
    responses: defaultdict[str, int] = defaultdict(int)
    calls: defaultdict[str, int] = defaultdict(int)
    results: defaultdict[str, int] = defaultdict(int)
    artifacts_checked = 0
    last_mono = -1
    for row in events:
        sequence = row.get("sequence")
        if sequence != expected_sequence:
            errors.append(f"Expected sequence {expected_sequence}, got {sequence}")
            expected_sequence = sequence if isinstance(sequence, int) else expected_sequence
        expected_sequence += 1
        trace_id = str(row.get("trace_id", ""))
        span_id = str(row.get("span_id", ""))
        trace_ids.add(trace_id)
        run_ids.add(str(row.get("run_id", "")))
        if not TRACE_ID_RE.fullmatch(trace_id):
            errors.append(f"Invalid trace_id at sequence {sequence}")
        if not SPAN_ID_RE.fullmatch(span_id):
            errors.append(f"Invalid span_id at sequence {sequence}")
        parent = row.get("parent_span_id")
        if parent is not None and not SPAN_ID_RE.fullmatch(str(parent)):
            errors.append(f"Invalid parent_span_id at sequence {sequence}")
        elif parent is not None:
            parent_spans.add(str(parent))
        monotonic = row.get("monotonic_ns")
        if isinstance(monotonic, int) and monotonic < last_mono:
            errors.append(f"monotonic_ns moved backwards at sequence {sequence}")
        if isinstance(monotonic, int):
            last_mono = monotonic
        event_type = str(row.get("event_type", ""))
        if event_type.endswith(".start"):
            if span_id in started:
                errors.append(f"Span {span_id} started more than once")
            started[span_id] = event_type[:-6]
            seen_spans.add(span_id)
        elif event_type.endswith(".end"):
            if span_id not in started:
                errors.append(f"Span {span_id} ended without a start")
            elif started[span_id] != event_type[:-4]:
                errors.append(f"Span {span_id} ended as a different type")
            ended.add(span_id)
        else:
            seen_spans.add(span_id)
        correlation = str(row.get("attributes", {}).get("correlation_id", ""))
        if event_type == "model_request":
            requests[correlation] += 1
        elif event_type == "model_response":
            responses[correlation] += 1
        elif event_type == "tool_call":
            calls[correlation] += 1
        elif event_type == "tool_result":
            results[correlation] += 1
        for reference in row.get("artifact_refs", []):
            try:
                path = _artifact_path(root, reference)
                data = path.read_bytes()
                if sha256_bytes(data) != reference.get("sha256"):
                    errors.append(f"Artifact hash mismatch: {reference.get('relative_path')}")
                if len(data) != reference.get("byte_length"):
                    errors.append(f"Artifact size mismatch: {reference.get('relative_path')}")
                artifacts_checked += 1
            except Exception as exc:
                errors.append(f"Invalid artifact at sequence {sequence}: {exc}")
    if len(trace_ids) > 1:
        errors.append("Trace contains multiple trace_id values")
    if len(run_ids) > 1:
        errors.append("Trace contains multiple run_id values")
    unknown_parents = parent_spans - seen_spans
    if unknown_parents:
        errors.append(f"Unknown parent spans: {sorted(unknown_parents)}")
    for name, left, right in (
        ("model", requests, responses), ("tool", calls, results),
    ):
        for correlation in sorted(set(left) | set(right)):
            if not correlation:
                errors.append(f"{name} boundary is missing correlation_id")
            elif left[correlation] != right[correlation]:
                errors.append(
                    f"Unpaired {name} correlation {correlation}: {left[correlation]} / {right[correlation]}"
                )
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    complete = state.get("status") == "COMPLETE"
    if require_complete and not complete:
        errors.append(f"Run state is {state.get('status')}, not COMPLETE")
    event_types = {row.get("event_type") for row in events}
    if complete and not {"run.end", "outcome"}.issubset(event_types):
        errors.append("Completed trace is missing run.end or outcome")
    missing_ends = set(started) - ended
    if complete and missing_ends:
        errors.append(f"Completed trace has open spans: {sorted(missing_ends)}")
    elif missing_ends:
        warnings.append(f"Partial trace has open spans: {sorted(missing_ends)}")
    seal_checked = False
    manifest_path = root / "run_manifest.json"
    if manifest_path.is_file():
        seal_checked = True
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            expected_paths = set()
            for item in manifest.get("files", []):
                expected_paths.add(item["path"])
                path = root / item["path"]
                if not path.is_file():
                    errors.append(f"Sealed file is missing: {item['path']}")
                    continue
                data = path.read_bytes()
                if len(data) != item["size_bytes"] or sha256_bytes(data) != item["sha256"]:
                    errors.append(f"Sealed file changed: {item['path']}")
            actual_paths = {
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
                and path.name != "run_manifest.json"
                and "teacher" not in path.relative_to(root).parts[:1]
            }
            unsealed = sorted(actual_paths - expected_paths)
            if unsealed:
                errors.append(f"Unsealed files exist outside teacher/: {unsealed}")
        except Exception as exc:
            errors.append(f"Invalid run_manifest.json: {exc}")
    return {
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "complete": complete,
        "event_count": len(events),
        "span_count": len(seen_spans),
        "artifact_references_checked": artifacts_checked,
        "seal_checked": seal_checked,
        "errors": errors,
        "warnings": warnings,
    }


def materialize_trace(run_root: Path | str) -> dict[str, Any]:
    root = Path(run_root)
    events = load_events(root)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        grouped[row["span_id"]].append(row)
    spans = []
    for span_id, rows in sorted(grouped.items(), key=lambda item: item[1][0]["sequence"]):
        start = next((row for row in rows if row["event_type"].endswith(".start")), rows[0])
        end = next((row for row in reversed(rows) if row["event_type"].endswith(".end")), None)
        spans.append({
            "trace_id": start["trace_id"],
            "span_id": span_id,
            "parent_span_id": start.get("parent_span_id"),
            "name": start["event_type"].removesuffix(".start"),
            "start_time_utc": start["timestamp_utc"],
            "end_time_utc": end["timestamp_utc"] if end else None,
            "status": end["status"] if end else "open",
            "duration_ms": end.get("duration_ms") if end else None,
            "events": rows,
        })
    trace = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": events[0]["trace_id"] if events else None,
        "run_id": events[0]["run_id"] if events else None,
        "task_id": events[0]["task_id"] if events else None,
        "spans": spans,
    }
    atomic_json(root / "trace.json", trace)
    return trace


def export_atif(run_root: Path | str) -> dict[str, Any]:
    root = Path(run_root)
    events = load_events(root)
    steps = []
    for row in events:
        if row["event_type"] not in {"model_response", "tool_call", "tool_result", "context_compaction"}:
            continue
        steps.append({
            "step_id": row["sequence"],
            "timestamp": row["timestamp_utc"],
            "source": row["agent_id"],
            "type": row["event_type"],
            "span_id": row["span_id"],
            "parent_span_id": row.get("parent_span_id"),
            "content": row.get("attributes", {}),
            "artifact_refs": row.get("artifact_refs", []),
        })
    document = {
        "schema_version": "1.7-compatible-export",
        "session_id": events[0]["run_id"] if events else None,
        "task_id": events[0]["task_id"] if events else None,
        "steps": steps,
        "fidelity": {
            "claim": "development export, not the official adk-eval-core SessionTrace serializer",
            "source_schema": SCHEMA_VERSION,
            "differences": [
                "Canonical storage is append-only events.jsonl.",
                "Large payloads are content-addressed artifacts.",
                "Span identity is retained as extension fields.",
            ],
        },
    }
    atomic_json(root / "trace.atif.json", document)
    return document


def render_reports(run_root: Path | str) -> None:
    root = Path(run_root)
    events = load_events(root)
    sections = [f"# Agent trajectory: {events[0]['task_id'] if events else 'unknown'}\n"]
    for row in events:
        if row["event_type"] in {
            "model_request", "model_response", "tool_call", "tool_result",
            "context_compaction", "patch_capture", "verification_result", "outcome",
        }:
            sections.append(
                f"## {row['sequence']}. {row['event_type']} · `{row['span_id']}`\n\n"
                f"```json\n{json.dumps(row.get('attributes', {}), indent=2, default=str)}\n```\n"
            )
    atomic_write(root / "transcript.md", "\n".join(sections).encode())
    payload = html.escape(json.dumps(events, indent=2, ensure_ascii=False))
    page = f"""<!doctype html>
<meta charset="utf-8"><title>Agent trace</title>
<style>body{{font:14px system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem}}
pre{{white-space:pre-wrap;background:#111;color:#eee;padding:1rem;border-radius:8px}}</style>
<h1>Agent trace</h1><p>Offline canonical event timeline. Artifact paths are relative to this run.</p>
<pre>{payload}</pre>"""
    atomic_write(root / "timeline.html", page.encode())


def _referenced_payloads(root: Path, events: Iterable[dict[str, Any]]) -> Iterable[tuple[int, str]]:
    for row in events:
        if row["event_type"] not in {"model_request", "tool_result"}:
            continue
        yield row["sequence"], json.dumps(row.get("attributes", {}), ensure_ascii=False, default=str)
        for reference in row.get("artifact_refs", []):
            # Only audit the bytes delivered to the model. Workspace snapshots,
            # full command logs and token ids are controller diagnostics, and may
            # legitimately contain an independently reconstructed test change.
            purpose = reference.get("purpose", "")
            if row["event_type"] == "tool_result":
                if not purpose.startswith(("model-visible tool result:", "malformed tool result")):
                    continue
            elif purpose not in {"complete outbound model messages", "rendered model prompt"}:
                continue
            try:
                payload = _artifact_path(root, reference).read_text(
                    encoding="utf-8", errors="replace"
                )
                yield row["sequence"], payload
                # JSON escaping must not hide a real multiline private value.
                if reference.get("media_type") == "application/json":
                    pending = [json.loads(payload)]
                    while pending:
                        value = pending.pop()
                        if isinstance(value, dict):
                            pending.extend(value.values())
                        elif isinstance(value, list):
                            pending.extend(value)
                        elif isinstance(value, str):
                            yield row["sequence"], value
            except Exception:
                continue


def audit_private_leaks(
    run_root: Path | str, *, private_values: dict[str, str], private_hashes: dict[str, str]
) -> dict[str, Any]:
    root = Path(run_root)
    events = load_events(root)
    hits = []
    checked = 0
    for sequence, payload in _referenced_payloads(root, events):
        checked += 1
        if re.search(r'(?i)"?(?:test_patch|reference_patch)"?\s*:', payload):
            hits.append({"sequence": sequence, "kind": "private_field_name"})
        for name, secret in private_values.items():
            if secret and secret in payload:
                hits.append({"sequence": sequence, "kind": "private_value", "field": name})
    audit = {
        "schema_version": SCHEMA_VERSION,
        "passed": not hits,
        "records_checked": checked,
        "hits": hits,
        "private_hashes": private_hashes,
        "limitations": (
            "Exact-content and field-name audit over model-visible requests and tool results only, "
            "including decoded JSON strings; controller-only diagnostics are excluded. "
            "this is not an operating-system sandbox or a provenance proof."
        ),
    }
    atomic_json(root / "leak_audit.json", audit)
    return audit


def seal_manifest(run_root: Path | str, metadata: dict[str, Any]) -> dict[str, Any]:
    root = Path(run_root)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "sealed_utc": utc_now(),
        **metadata,
        "files": file_manifest(root),
        "teacher_directory_excluded_from_seal": True,
    }
    atomic_json(root / "run_manifest.json", manifest)
    return manifest
