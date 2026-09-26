# Gemma Agent Version 1 — Local Validation Record

Date prepared: 2026-09-23

## Artifact

- Source directory: `/Users/dawoodazeeza/Projects/gemma-dev/submission_v1`
- Archive: `/Users/dawoodazeeza/Projects/gemma-dev/gemma-agent-v1.zip`
- Archive size: 3,403 bytes
- SHA-256: `41a457ac2691f5070cfd38232f8575b55a9ad456b189e00c41202bdcec0afa9e`
- Submission description: `v1 prompt-only official scaffold; no adapters; 3m/18 calls; focused tests`

## Configuration

- Model for both agents: `gemma-4-31b-it-qat-w4a16-ct`
- Execution budget: 60 seconds per command, 18 counted tool calls, 3 minutes, 50 turns
- Generation: temperature 0.2, top-p 0.95, 8,192 output tokens
- Thinking: medium, 2,048-token budget, thoughts excluded
- Architecture: root repair agent plus one read-only analyzer agent
- Adapters: none

## Completed Checks

- [x] Exactly one recognized root config exists, and `agent.yaml` is at the ZIP root.
- [x] Both agents use the exact required model name.
- [x] No adapter keys, adapter files, or adapter directory are present.
- [x] All `!include` paths are relative, resolve inside the package, contain no `..`, and target existing files.
- [x] No symlinks, absolute includes, broken paths, or disallowed file extensions are present.
- [x] YAML syntax, expected scalar types, budgets, and sampling values are valid under local static parsing.
- [x] Root tools include command execution, file reading/editing/writing, status, patch submission, and the documented code-intelligence tools.
- [x] The analyzer has only read-only file and code-intelligence tools.
- [x] The prompts require exact-identifier navigation, at most one analyzer delegation, minimal source edits, narrow tests, one correction loop, a final diff check, and explicit patch submission.
- [x] The prompts forbid test edits, broad test suites, network installs, unrelated repairs, and out-of-workspace searches.
- [x] `tasks.jsonl` contains 129 valid JSON records, each with the eight expected fields.
- [x] The ZIP contains only six intended files and two directory entries; no model weights, datasets, caches, logs, wheels, snapshots, embeddings, or macOS metadata are included.
- [x] ZIP integrity testing passed.
- [x] A fresh extraction passed the same structural/configuration checks and matched the source directory byte-for-byte.
- [x] Approximately 13.6 GiB remained free after packaging, above the 10 GiB reserve.

## Verification Boundary

This record covers local static and archive validation only. No local Gemma inference, Docker harness run, Kaggle upload, or benchmark claim was attempted. The archive has not been submitted.
