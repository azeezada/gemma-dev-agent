# Trace schema

`events.jsonl` is canonical. `trace.json`, `trace.atif.json`, `transcript.md`, and `timeline.html` are derived views.

Each event contains `experiment_id`, `run_id`, `task_id`, 32-hex `trace_id`, 16-hex `span_id`, optional `parent_span_id`, strictly increasing `sequence`, UTC and monotonic timestamps, `event_type`, `status`, attributes, artifact references, and optional budget snapshots.

Important boundaries:

- `model_request` / `model_response` pair through `attributes.correlation_id`.
- `tool_call` / `tool_result` pair the same way.
- `context_measurement` and `context_compaction` explain context pressure.
- `patch_capture` identifies the frozen patch.
- `verification_result` has `phase` equal to `baseline` or `patched`.
- `outcome` and `run.end` close a complete run.

Large payloads are stored in `artifacts/blobs/`. Resolve an `artifact_ref.relative_path` beneath the run directory and verify its SHA-256 before using it. Distinguish complete output artifacts from the bounded `model_visible_summary`.

The run seal excludes `teacher/`, allowing evaluation output without modifying the solver record. Treat any hash mismatch outside `teacher/` as an infrastructure-integrity failure.
