# Evaluation outputs

Complete the initialized `evaluation.json` without removing fields:

```json
{
  "schema_version": "1.0",
  "evaluation_id": "...",
  "run_id": "...",
  "teacher": {"model": "...", "skill_version": "1.0", "confidence": 0.0},
  "outcome": {"classification": "PASS|FAIL|INCONCLUSIVE|INFRA_ERROR", "matches_harness": true},
  "trajectory": {
    "phases": {
      "task_understanding": {"score": 0, "rationale": "...", "evidence_ids": ["E1"]}
    },
    "weighted_score": 0.0,
    "observable_weight": 0.0
  },
  "components": {"findings": []},
  "failure_attribution": {"primary": "...", "secondary": [], "rationale": "...", "evidence_ids": []},
  "recommendations": [{"change_area": "prompt|tool|harness|context|environment", "change": "...", "expected_metric": "...", "evidence_ids": []}],
  "limitations": []
}
```

A phase score may be an integer 0–4 or the string `not_observable`. `confidence` is from 0 to 1. The deterministic outcome must match `final/outcome.json`.

Write one JSON object per line in `evidence.jsonl`:

```json
{"evidence_id":"E1","claim":"...","event_sequences":[12],"span_ids":["0123456789abcdef"],"artifact_refs":[]}
```

`evaluation.md` is the concise human-readable rendering: outcome, strongest evidence, phase table, causal failure, and prioritized recommendations.
