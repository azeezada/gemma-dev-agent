# Harness contracts

Each directory is a declarative submission (`agent.yaml`, `eval_config.yaml`, `prompts/`, `configs/`, optional
`skills/`, `sub_agents/`). Only files the official submission allowlist accepts may live inside; notes go here.

| contract_id | What | Status |
|---|---|---|
| `train_v1` | Training harness v1 (plan D5, section 5): one `LlmAgent`; built-in tools `run_command`, `read_file`, `get_status`, `submit_patch`; no skills, no sub-agents; edits via `run_command`. Sampling copied from v22. | **Frozen.** Any change to prompt, tools, sampling or thinking needs a new directory, because runs recorded under a contract are training data. Guarded by `tests/test_train_harness.py`. |
| `v22` | Submitted skills harness (swe_locate + swe_edit). Nested skill args break the gemma4 parser (40 % malformed). | Submission baseline / control |
| `v3` | Earlier localizer sub-agent harness; used by tests as a fixture. | Legacy |
| `candidates/A..F` | Multi-harness experiments (`experiments/multi/assemble.py`). | Experimental |
