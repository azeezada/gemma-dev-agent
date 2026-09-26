# First Submission Plan

## Objective

Submit a small, valid, prompt-only baseline by end of day that:

1. Is accepted by the Kaggle evaluation harness.
2. Uses the required `gemma-4-31b-it-qat-w4a16-ct` model.
3. Has a realistic chance of producing at least one correct patch.
4. Becomes a clean, reproducible base for later prompt, tool, and LoRA experiments.

The first submission is primarily an end-to-end systems test. A nonzero public score is the target, but an accepted and completed run is still useful because it verifies packaging, configuration, tool use, and time budgets.

## Local Constraints and Consequences

This Mac currently has:

- Apple M1, ARM64
- 8 CPU cores
- 16 GB unified memory
- About 15 GB free disk space
- Docker installed, but its engine is not currently available

Therefore, for this submission:

- Do not download the 23.3 GB model, repository snapshots, embeddings, wheels, or Docker images.
- Do not attempt local Gemma inference or LoRA training.
- Do not attempt an emulated `linux/amd64` harness run today. It would be slow, disk-heavy, and still would not reproduce the four-L4 organizer environment.
- Use the Mac only for editing, static validation, archive inspection, and lightweight analysis of `tasks.jsonl`.
- Keep at least 10 GB of disk free. Abort any optional download or build that threatens that reserve.

## Version 1 Design

Start from the official `sample_submission`, but copy it to a new directory such as `submission_v1`. Do not edit the sample in place.

### Agent architecture

Keep the official two-agent shape:

- Root repair agent: diagnoses, edits, tests, and submits the patch.
- Analyzer sub-agent: inspects code and symbol relationships when the relevant file is not immediately obvious.

This preserves a known-valid configuration shape while keeping implementation risk low.

### Adapters

Remove both `adapter:` references and exclude the `adapters/` directory from Version 1.

The supplied `main_lora` and `tool_lora` weights are identical, tiny rank-4 adapters affecting only layer 0. They appear suitable as packaging examples, not as evidence-backed specialist training. A prompt-only baseline has fewer failure modes and gives a cleaner reference score.

### Model

Use this exact model name everywhere:

```yaml
model: gemma-4-31b-it-qat-w4a16-ct
```

Do not add another model or attempt to bundle model weights. Kaggle supplies the model at evaluation time.

### Initial execution budget

Use:

```yaml
evaluation:
  timeout_seconds: 60
  max_tool_calls: 18
  max_time_minutes: 3
  max_turns: 50
```

Rationale:

- Three minutes gives the agent enough time for inspection, one focused edit, and a targeted test.
- Eighteen tool calls permit one analyzer delegation and one correction loop without inviting prolonged wandering.
- At three minutes per task, 120 tasks would consume at most six hours of agent time if executed serially, leaving headroom under the competition run limit.
- Individual shell commands remain capped at 60 seconds, discouraging full test suites.

### Initial generation settings

Use:

```yaml
temperature: 0.2
top_p: 0.95
max_output_tokens: 8192
thinking_config:
  thinking_level: medium
  thinking_budget: 2048
  include_thoughts: false
```

These settings favor deterministic repair behavior and reduce latency and context consumption. Version 1 should not spend 16K output tokens or expose 4K thought tokens unless results later show that deeper reasoning is more valuable than coverage.

### Prompt behavior

Keep the official prompt's emphasis on minimal fixes, but make these rules explicit:

1. Extract likely module, class, function, and exception names from the issue first.
2. Use graph and similarity tools with exact or suspected code identifiers, not prose copied from the error message.
3. If no identifier is known, use a narrow filename or text search rather than a broad repository scan.
4. Delegate to the analyzer at most once, and only when the target file or symbol is unclear.
5. Read the smallest relevant code region and nearby tests before editing.
6. Make the smallest source change that addresses the stated behavior. Never edit tests.
7. Run only the narrowest relevant test, test file, or reproduction command. Never run an unqualified full test suite.
8. Permit at most one edit-and-retest correction loop before submitting the best nonempty patch.
9. Inspect the final diff for unrelated changes and then call `submit_patch` immediately.
10. If testing cannot finish, submit a coherent nonempty patch rather than timing out while continuing to investigate.

The task data contains no hints, so the agent must not depend on `hints_text` being populated. The dataset is dominated by FastAPI and Rich, but Version 1 should remain repository-agnostic rather than adding brittle framework-specific instructions.

## Build Procedure

### 1. Create an isolated submission directory

- Copy `sample_submission/` to `submission_v1/`.
- Retain `agent.yaml`, `eval_config.yaml`, prompts, sampling config, and the analyzer sub-agent config.
- Remove adapter declarations and omit adapter files from the new directory.
- Do not add caches, logs, local metadata, or the downloaded competition data.

### 2. Apply the minimal changes

- Update the root and analyzer prompts with the behavior above.
- Update the execution budget.
- Update the generation settings.
- Leave tool names and the overall YAML structure unchanged unless validation shows a concrete problem.

### 3. Run static validation

Validate all of the following locally:

- Exactly one recognized root config is present.
- Both agent configs use the exact required model name.
- No `adapter:` references remain.
- Every `!include` target exists, remains inside the submission directory, and uses a relative path.
- There are no broken paths, absolute includes, `..` traversal, or symlinks.
- Every file uses an allowed extension: `.yaml`, `.yml`, `.md`, `.txt`, `.py`, `.json`, or `.safetensors`.
- YAML indentation and scalar types are valid, accounting for the harness-specific `!include` tag.
- The root agent has `submit_patch`, editing, file-reading, command, and status tools.
- The analyzer has read-only inspection tools and no unnecessary editing responsibility.
- The archive contains no model weights, snapshots, embeddings, wheels, datasets, or macOS metadata.

Also run lightweight checks on `tasks.jsonl` to confirm it still contains 129 valid JSON records with the expected fields. This checks the local download, not the agent's correctness.

### 4. Inspect prompt and budget consistency

Before packaging, manually confirm:

- The prompt does not tell the agent to perform more work than 18 tool calls and three minutes allow.
- It does not require a full test suite.
- It explains when to use symbol tools versus text search.
- It requires a final diff check and an explicit `submit_patch` call.
- It has a sensible timeout fallback: submit the best coherent patch.

### 5. Package and revalidate the artifact

- Create `gemma-agent-v1.zip` with `agent.yaml` at the archive root, not inside an extra parent folder.
- Aim for an archive well under 1 MB. The competition permits much more, but this baseline needs only text configuration.
- List the ZIP contents and verify there are no unexpected files.
- Extract the ZIP into a fresh temporary directory.
- Rerun the same structural validation against the extracted copy.
- Record a SHA-256 checksum and the exact source directory used to create it.

### 6. Submit once

Upload the verified ZIP through Kaggle and give it a clear description such as:

`v1 prompt-only official scaffold; no adapters; 3m/18 calls; focused tests`

Do not use another daily submission slot until the run result or a concrete packaging failure is available.

## Verification Boundary

What can be verified on this Mac:

- File structure and permitted extensions
- Includes and configuration references
- Exact model identifiers
- Absence of large or unintended files
- ZIP root layout and extraction integrity
- Prompt consistency and tool availability
- Dataset JSON integrity

What cannot be responsibly verified locally today:

- Gemma's actual tool-calling behavior
- Patch quality on the hidden repositories
- CUDA/vLLM compatibility and throughput
- The organizer sandbox's installed packages
- A true pass on a public or private benchmark task

The Kaggle run is therefore the exact-environment integration test. Do not claim that Version 1 passes a benchmark before Kaggle reports it.

## Go/No-Go Checklist

Submit only if every item below is true:

- [ ] `agent.yaml` is at the ZIP root.
- [ ] Exactly one root config exists.
- [ ] The exact required Gemma model is used by both agents.
- [ ] No adapter references or adapter files are present.
- [ ] All includes resolve inside the package.
- [ ] All tools are spelled exactly as documented.
- [ ] The root agent can edit files, run a focused test, inspect status, and submit a patch.
- [ ] The final prompt forbids test edits and broad test runs.
- [ ] Budget is 3 minutes, 18 tool calls, 50 turns, and 60 seconds per command.
- [ ] Generation settings are 0.2 temperature, 0.95 top-p, 8,192 output tokens, and a 2,048-token medium thinking budget.
- [ ] The extracted ZIP passes the same checks as its source directory.
- [ ] ZIP size is small and no prohibited or unintended artifacts are included.
- [ ] At least 10 GB remains free on the Mac.

## Interpreting the First Result

### If the submission is rejected before evaluation

Fix only the reported structural or configuration issue and resubmit. Do not change prompt strategy at the same time.

### If evaluation completes with a score of zero

Use logs, if available, to classify the dominant failure:

- No patch produced: simplify the prompt, reduce investigation, or raise the time/tool budget modestly.
- Timeouts: shorten reasoning/output budgets and enforce earlier editing/testing.
- Invalid tool calls: make tool argument rules more explicit.
- Patches produced but tests fail: inspect whether diagnosis, edit scope, or testing strategy is the weak link.
- Repeated graph-tool misses: strengthen the exact-identifier rule or remove the graph detour from the root agent.

Change one major variable at a time so scores remain interpretable.

### If the score is nonzero

Freeze the entire Version 1 directory and ZIP. Create Version 2 from a copy and test one of these improvements at a time:

1. A refined root prompt based on observed failure traces.
2. A different reasoning/time budget.
3. A simplified single-agent configuration if delegation wastes calls.
4. Repository-specific skills or instructions supported by task analysis.
5. A genuinely trained LoRA only after suitable compute, training examples, and reliable evaluation are available.

## Definition of Done for Today

Version 1 is done when:

- The package passes every local static and archive check.
- Its checksum and settings are recorded.
- Kaggle accepts it and starts or completes evaluation.
- The run outcome is logged as the baseline for future experiments.

A public score above zero is the desired outcome, not something local validation can guarantee.
