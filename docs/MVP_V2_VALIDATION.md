# MVP v2 Kaggle validation

Validated on Kaggle notebook version 16 on 2026-09-24.

- Notebook: `dawoodazeeza/gemma-dev`
- Notebook SHA-256: `311da97b2ce6f13ad24b040faf3b5d52b1860616c2da39fa1a6e043ef8444893`
- Runtime: four NVIDIA L4 GPUs
- Model: `gemma-4-31b-it-qat-w4a16-ct`
- vLLM startup: 448.976 seconds
- Overall acceptance: PASS
- Competition submission performed: no

## Failure review

Version 12 reached the FastAPI agent and generated the correct 391-byte patch, but verification inherited a global LangSmith pytest plugin and loaded an incompatible Pydantic/Pydantic Core pair. Version 15 failed earlier: its dependency resolver followed Flask's dependency on `blinker>=1.9.0`, which was not present in the offline competition wheel directory, so the FastAPI agent never started.

The final workflow disables third-party pytest plugin auto-loading and installs a constraint-compatible set of offline wheels with `--no-deps`. It recursively includes transitive wheels that are actually available, including the exact Pydantic Core required by Pydantic, while leaving absent optional dependencies to the pre-baked Kaggle environment. Dependency setup is preserved in each task's diagnostics.

Versions 13 and 14 were cancelled because the publishing workflow was temporarily invoked with a 60-second Kaggle runtime limit. Version 16 was published with a 7,200-second limit.

## Version 16 results

| Task | Result | Tool calls | Patch | Verification |
| --- | --- | ---: | ---: | --- |
| `fastapi_14492` | PASS | 17 | 391 bytes | 2 passed |
| `requests_7205` | PASS | 14 | 503 bytes | 206 passed, 13 skipped |

Both leak audits passed. Reference patches and gold tests were not exposed to the model. The verifier began only after each agent stopped.

`submission.zip` contains exactly:

- `agent.yaml`
- `configs/sampling.yaml`
- `eval_config.yaml`
- `prompts/system.md`

Artifact hashes:

- `submission.zip`: `1e48a5b7492f941749afe5d7471a5133995ed6127147b245f494fb0832ea890f`
- `mvp_v2_diagnostics.zip`: `8f1df9ef806ee01f4b0824994112ab0dc13fd0d36f2c8b86952c178baad993f7`
