# gemma-dev — agent map

Goal: maximise the resolve rate of a Gemma 4 31B developer agent on hidden private-repo tasks (Kaggle competition
`gemma-4-developer-agent`). Levers: LoRA adapters on the fixed base model and the declarative harness.
Plan and decision log: `docs/RL_PROJECT_PLAN.md` (read §0 first). `AGENTS.md` is an identical copy of this file
(`tests/test_agent_docs.py` enforces it): edit `CLAUDE.md`, then `cp CLAUDE.md AGENTS.md`.

## Layout
- `src/gemma_agent/env/` — replica of the official harness (vLLM server, sandbox, budgets, verification).
- `src/gemma_agent/rollout/` — traced ADK runtime: schemas, tracing, reporting, evaluation, workflow (was `agent_harness/`).
- `harnesses/<contract_id>/` — declarative submissions (`v3`, `v22`); `harnesses/candidates/A..F` are experiments.
- `notebooks/src/` builders (single source of truth) → `notebooks/build/*.ipynb` (generated, never hand-edit).
- `kaggle/` — Kaggle helper scripts. `tools/` — trace viewer and analysis scripts. `skills/` — trace-evaluator skill.
- `experiments/multi/` — candidate assembly, shared skill library, trace analyses.
- `tests/` — run with `python -m pytest -q` (`pytest.ini` sets `pythonpath = src . tests`).
- `archive/` — superseded submissions/notebooks. Gitignored: `data/`, `work/`, `runs/`.

## Rules
- Competition data (`data/`: tasks.jsonl, snapshots, graphs, embeddings, wheels, HARNESS_README.md, sample_submission,
  `docs/rules.txt` and other provided docs) never goes to git or to a third-party API that retains data.
- No paid APIs; no Claude/GPT output in training data. Hidden tests/gold (`test_patch`, `patch`) never reach an agent.
- Training data must be rendered exactly as vLLM renders it; every run/adapter/eval is stamped with `contract_id`.
- Agent behaviour fixes go in the submission harness, not in local-only tools.
- Push, submit (`kaggle competitions submit`) and GPU runs only on explicit user OK.

## Known baseline
`python -m pytest -q`: 117 pass, 2 fail (`tests/test_swe_guard.py`: test_collapse_is_undone_by_finalize,
test_deadline_salvage_inside_apply_edit — pre-existing, environment-dependent).
