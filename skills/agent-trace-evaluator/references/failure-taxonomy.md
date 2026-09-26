# Failure taxonomy

Choose one primary category and no more than two secondary categories. Cite direct evidence.

- `TASK_UNDERSTANDING`: misunderstood requested behavior or acceptance criteria.
- `LOCALIZATION`: failed to find the relevant implementation or tests.
- `HYPOTHESIS`: found relevant code but inferred the wrong failure mechanism.
- `TOOL_INTERFACE`: malformed calls, unsuitable tool choice, or inability to operate the interface.
- `EDIT_EXECUTION`: correct direction but failed or incoherent code changes.
- `VERIFICATION`: insufficient, irrelevant, or misinterpreted tests and final review.
- `BUDGET`: time, turn, or tool allocation prevented completion.
- `CONTEXT`: lost, duplicated, or compacted critical information.
- `HARNESS`: orchestration, parsing, tracing, or policy behavior caused the failure.
- `ENVIRONMENT`: dependencies, model server, process execution, or repository setup failed.
- `TASK_EVAL`: verifier or task definition cannot establish a trustworthy result.
- `NONE`: verified success with no material failure.

Attribute the earliest causal failure, not merely the final symptom. For example, budget exhaustion caused by repeated unfocused searches is primarily `LOCALIZATION`, with `BUDGET` secondary.
