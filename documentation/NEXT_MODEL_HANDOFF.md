# Gemma Developer Agent workflow handoff

## Current state

The Kaggle notebook is `dawoodazeeza/gemma-dev`:

<https://www.kaggle.com/code/dawoodazeeza/gemma-dev>

Version 17 is currently running as of 2026-09-24. It is the seven-task mixed benchmark described below. The user explicitly asked the previous model not to monitor this run, so no result is known yet. Before publishing anything else, check its status with the Kaggle CLI and, once terminal, download and review its outputs. Do not create a duplicate version while its state is uncertain.

The latest known completed and validated run is version 16. It passed the complete acceptance gate:

- Four NVIDIA L4 GPUs and the intended Gemma 4 model were used.
- `fastapi_14492`: PASS, 17 tool calls, 391-byte patch, 2 gold tests passed.
- `requests_7205`: PASS, 14 tool calls, 503-byte patch, 206 tests passed and 13 skipped.
- Both leak audits passed.
- `submission.zip` contained exactly `agent.yaml`, `configs/sampling.yaml`, `eval_config.yaml`, and `prompts/system.md`.
- No competition submission was performed.

Version 16 artifact hashes:

- `submission.zip`: `1e48a5b7492f941749afe5d7471a5133995ed6127147b245f494fb0832ea890f`
- `mvp_v2_diagnostics.zip`: `8f1df9ef806ee01f4b0824994112ab0dc13fd0d36f2c8b86952c178baad993f7`
- validated two-task notebook: `311da97b2ce6f13ad24b040faf3b5d52b1860616c2da39fa1a6e043ef8444893`

## Currently running version 17

Version 17 expands the development benchmark to seven deliberately different public tasks:

- FastAPI: `fastapi_14492`, `fastapi_14258`
- Rich: `rich_4076`, `rich_3468`
- Requests: `requests_7205`, `requests_6629`
- HTTPX: `httpx_3672`

It deletes each task's temporary agent workspace, verification workspace, and venv immediately after preserving diagnostics. It records aggregate task count, pass count, fail count, and resolution rate. Individual task failures are benchmark results rather than notebook failures; the infrastructure gate still requires the known FastAPI canary, all task records, a valid submission ZIP, and clean leak audits.

Version 17 notebook SHA-256:

`a7e96b0700ea719d0e6d887dba4ec56ccb0be3bae09546401f20871da9ff7109`

## Source of truth

Project root:

`/Users/dawoodazeeza/Projects/gemma-dev`

Edit the source files, not the generated notebook directly:

- Harness: `mvp_v2/harness.py`
- Notebook builder: `tools/build_mvp_v2_notebook.py`
- Generated notebook: `notebooks/gemma_agent_mvp_v2.ipynb`
- Submission files: `submission_v2/`
- Local tests: `tests/test_mvp_v2_harness.py`
- Version 16 validation report: `documentation/MVP_V2_VALIDATION.md`

User-facing copies are under:

`/Users/dawoodazeeza/Documents/Codex/2026-09-23/i/outputs`

## Required Kaggle workflow

Use the `kaggle-notebook-publisher` skill and read its `SKILL.md` completely before taking action:

`/Users/dawoodazeeza/.codex/skills/kaggle-notebook-publisher/SKILL.md`

Supporting paths:

- Kaggle CLI environment: `/Users/dawoodazeeza/Documents/Codex/2026-09-23/i/work/kaggle-cli-v2`
- Publisher: `/Users/dawoodazeeza/.codex/skills/kaggle-notebook-publisher/scripts/publish_notebook.py`

Always:

1. Check remote status before a retry or new push.
2. Run local harness tests.
3. Rebuild the notebook from the builder.
4. Parse every code cell before publishing.
5. Run the publisher in `--dry-run` mode and inspect the metadata.
6. Push exactly once without `--dry-run`.
7. Use `--timeout-seconds 7200`. This value is the Kaggle notebook runtime limit, not a network timeout.
8. Preserve these settings:
   - kernel: `dawoodazeeza/gemma-dev`
   - competition: `gemma-4-developer-agent`
   - model: `google/gemma-4/Other/gemma-4-31b-it-qat-w4a16-ct/2`
   - dataset: `nguyenkhaho/vllm-wheel`
   - accelerator: `NvidiaL4` for four L4 GPUs
   - internet: off
9. Never submit `submission.zip` to the competition unless the user separately and explicitly asks.

After version 17 becomes terminal, download logs and outputs to a new version-specific directory such as:

`/Users/dawoodazeeza/Documents/Codex/2026-09-23/i/work/kaggle-mixed-version17-output`

Inspect at minimum:

- kernel status and log
- `mvp_v2_diagnostics/summary.json`
- every task summary, dependency setup, transcript, patch, and test output
- aggregate resolution rate
- top failure categories
- leak audits
- exact `submission.zip` contents and SHA-256

## Important failure history

- Initial GPU detection failed when no accelerator was attached.
- vLLM's optional FlashInfer sampler failed to link `libcuda`; `VLLM_USE_FLASHINFER_SAMPLER=0` is required.
- Kaggle's post-vLLM environment could not run venv `ensurepip`; task venvs use `--without-pip --system-site-packages`.
- Version 12 produced the correct FastAPI patch but pytest auto-loaded a global LangSmith plugin and hit a Pydantic/Pydantic Core mismatch.
- Version 15 failed FastAPI setup because ordinary dependency resolution required an unavailable `blinker` wheel through Flask.
- The current installer uses compatible offline wheels, recursively includes transitive wheels that actually exist, uses `--no-deps`, and disables global pytest plugin auto-loading. This produced matching Pydantic `2.13.4` and Pydantic Core `2.46.4` in version 16.
- Versions 13 and 14 were cancelled because the publisher was invoked with `--timeout-seconds 60`. Never repeat that; use `7200`.

## Next objective

Use version 16 as the known-good baseline and version 17 as the first broader evidence set. Improve the workflow based on version 17's actual trajectories and categorized failures, while preserving reference-patch/test-patch isolation and the clean prompt-only submission artifact. Prefer small, evidence-driven changes over immediately running all 129 public tasks.
