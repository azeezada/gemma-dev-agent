# Single-agent development harness

The development harness runs one public software-engineering task, freezes the solver patch, verifies it in independent patched and baseline workspaces, and saves a sealed replayable trace. It does not submit competition files.

## Trust boundaries

1. The solver receives only `TaskSpec`, its isolated repository, the packaged prompt, and the nine supported tools.
2. The verifier receives the frozen patch and `VerificationSpec` after the solver stops.
3. A separate teacher may later receive the sealed run plus optional reference evidence. Teacher output is written only below `teacher/`, which is excluded from the solver seal.

Private test and reference content never enters solver messages, tool results, or the run bundle. Their SHA-256 hashes are recorded for provenance.

## Run one task

Create JSON documents conforming to the schemas in `agent_harness/json_schemas/`, then run:

```bash
python -m agent_harness run \
  --task /absolute/path/task.json \
  --verification /absolute/path/verification.json \
  --config /absolute/path/run.json
```

The `RunConfig` may connect to an existing OpenAI-compatible endpoint with `model.base_url`, or launch the attached checkpoint with `model.path`. Sampling and budget overrides are explicit and recorded; otherwise the values in the submission directory are authoritative.

Validate or regenerate derived views with:

```bash
python -m agent_harness validate --run /absolute/path/to/run
python -m agent_harness render --run /absolute/path/to/run
```

## Canonical record

`events.jsonl` is the source of truth. Each row has stable experiment, run, task, trace, span, parent-span, sequence, timestamp, status, budget, and artifact fields. Large values are content-addressed under `artifacts/blobs/`; model-visible summaries remain distinct from complete command and model payloads.

`trace.json`, the documented ATIF-compatible export, transcript, and timeline are derived. `run_manifest.json` seals every non-teacher artifact. Validation checks ordering, identifier formats, parent relationships, model/tool pairing, artifact hashes, final events, and the run seal.

## Teacher evaluation

Invoke the project skill with a completed run:

```text
Use agent-trace-evaluator to evaluate:
/absolute/path/to/run
```

The skill first derives deterministic metrics, then produces a teacher report covering outcome, trajectory phases, component behavior, causal failure attribution, and actionable recommendations. Qualitative claims must cite event sequences and span IDs; the teacher cannot override the verifier outcome or delegate to subagents.

## Kaggle notebook

`tools/build_trace_notebook.py` produces `notebooks/gemma_agent_trace.ipynb`. Its stable configuration cell selects one public task. The notebook embeds the same local runtime, uses four L4 GPUs with internet disabled, creates `submission.zip`, and preserves the sealed run as `trace_diagnostics.zip` without submitting it to the competition.
