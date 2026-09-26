---
name: agent-trace-evaluator
description: Evaluate a completed single-agent software-engineering run from its sealed trace bundle, producing outcome, trajectory, component, and failure-attribution reports with span-level evidence. Use after a solver run; do not use to solve the task or alter solver artifacts.
---

# Agent Trace Evaluator

Evaluate one sealed run directory after the solver has stopped. Never delegate to subagents, continue the solver, change its patch, or edit files outside the run's `teacher/` directory.

## Workflow

1. Run `scripts/validate_trace.py <run-directory>`. Continue with a partial run only when the validator identifies it as incomplete; label unavailable evidence `not_observable`.
2. Run `scripts/init_evaluation.py <run-directory> [--reference <reference-bundle>]`. Use the printed evaluation directory for all teacher output.
3. Read `deterministic_metrics.json`, `final/outcome.json`, `transcript.md`, and relevant events or referenced artifacts. Read [references/rubric.md](references/rubric.md) before scoring and [references/failure-taxonomy.md](references/failure-taxonomy.md) before attribution.
4. If a reference bundle is supplied, treat it as post-run evidence only. Compare behavioral milestones and acceptance criteria, not an exact action sequence. Never copy private reference contents into the sealed solver files.
5. Complete `evaluation.json`, `evaluation.md`, and `evidence.jsonl` according to [references/evaluation-schema.md](references/evaluation-schema.md). Every qualitative claim must cite event sequence numbers and span IDs. Use `not_observable` instead of guessing.
6. Run `scripts/validate_evaluation.py <evaluation-directory>`. Fix only teacher-output validation failures.

## Invariants

- Reproduce the deterministic outcome exactly; never promote or demote it.
- Keep infrastructure failures separate from solver failures.
- Scores diagnose behavior and do not replace task success.
- Recommendations must name a prompt, tool, harness, context, or environment change and the metric it should affect.
- Record the teacher model, skill version, confidence, and any reference-bundle hash.

For trace fields or artifact resolution, read [references/trace-schema.md](references/trace-schema.md).
