# RL / Post-Training Project Plan — Gemma 4 Developer Agent

Status: **APPROVED plan (2026-09-26)**. Build started 2026-09-26: reorganisation done (§13); see §16 for progress.
Items still needing a user answer are marked **[OPEN]**.

---

## 0. Decision log

| # | Decision | Source |
|---|---|---|
| D1 | RL = a verifiable-reward data loop: SFT on teacher runs, then rejection-sampling fine-tuning (RFT / expert iteration) on Gemma's own passing runs. **Online RL (GRPO etc.) is skipped.** | user, round 3 |
| D2 | Build the whole pipeline end-to-end first (MVP notebook, tiny data), then improve the data. | user, round 3 |
| D3 | Harness is the *fast loop*, adapters the *slow loop*: each new adapter runs on the best harness, and harnesses are re-shaped to it before the next adapter run. | user, round 2 |
| D4 | One adapter shared by all agents. | user, round 2 |
| D5 | Training harness v1 = one agent, built-in tools only, **no skill scripts**. Revisit a localizer sub-agent after adapter v1 using measured criteria (§5.3). | user, round 3 |
| D6 | All 129 public tasks are held out (never trained on). `rich_4076` is the gut-check task for every pipeline change. | user, round 2 |
| D7 | No paid APIs. No Claude/GPT subscription output in training data (their terms forbid training other models on it). | user, round 2 + ToS |
| D8 | Teacher chosen by an experiment, not by assumption: Big Pickle (OpenCode Zen) vs self-hosted Qwen3-Coder-Next vs high-budget Gemma, benchmarked on the competition tasks. Big Pickle is used for *data generation* on external tasks only. | user, round 4 (see §8.2 [OPEN]) |
| D9 | External training tasks: recent real tasks from many repos ("core") only, to start. | user, round 4 |
| D10 | Graph/embedding tools deprioritised; one cheap experiment measures their value on base Gemma. | user, round 4 |
| D11 | Metrics dashboards are built agentically (static HTML from JSONL) for now. Trackio is a noted option to revisit; W&B not adopted. | user, round 3 |
| D12 | Reorganise the repo, `git init`, push to a **private** GitHub repo `azeezada/gemma-dev`, with competition data excluded. | user, rounds 2 + 4 |
| D13 | `AGENTS.md` is an identical copy of `CLAUDE.md` (a test enforces it). | user, round 2 |
| D14 | Spend one daily submission early on a tiny adapter to prove the official adapter path works. | user, round 2 |
| D15 | No timeline in this plan; work is ordered, not dated. `work/` is not archived to Kaggle; the user frees Mac disk space. | user, rounds 2 + 4 |

---

## 1. Goal and hard constraints

**Goal:** win, i.e. maximise the resolve rate on ~120 hidden tasks from *private repositories*.
The only levers are (a) LoRA adapters on the fixed base model and (b) the declarative harness.

### 1.1 Competition

| Constraint | Consequence |
|---|---|
| Base fixed: `gemma-4-31b-it-qat-w4a16-ct` (int4, group 32, compressed-tensors), one model for all agents | LoRA is the only weight lever. No full fine-tune, no merged weights. |
| Adapters: PEFT `.safetensors`, rank ≤ 128, ≤ 8 adapters, submission < 3 GiB | Size is not a constraint at rank ≤ 64. |
| vLLM: TP=4, `max_model_len=32768`, `gemma4` tool + reasoning parsers, ADK/LiteLLM client | Training data must be rendered **exactly** as vLLM renders it. |
| **No effective context compaction** (verified in adk-python source, §6.2) | A run whose context passes 32k dies (the harness then submits the `git diff`). Every training run must fit in 32k. |
| 12 h for ~120 tasks incl. setup; measured ≈ 0.2 s + 28.7 ms per output token | ≈ 5 min and ≈ 10k output tokens per task. Output tokens are the budget; prompt tokens are nearly free. |
| Hidden tasks come from private repos, same curation pipeline | Repo diversity in training matters more than fastapi/rich skill. |
| 1 submission/day; public LB ≈ 60 tasks | LB standard error ≈ ±5 pp at 20 %. Local eval is the decision metric; LB is a sanity check. |
| External data/models allowed if public and cheap; winners open-source their code | Training-data provenance must be documentable and licence-clean. |
| Competition data must not be shared with non-participants | Competition files stay out of GitHub and out of third-party APIs that retain data. |

### 1.2 Compute and storage

| Resource | Budget | Use |
|---|---|---|
| Kaggle 4×L4 (96 GB), 12 h sessions | ~30 GPU-h/week | rollouts, training, faithful eval, self-hosted teachers |
| Kaggle CPU notebooks (internet on) | free, no GPU quota | building task packs, API-teacher runs, data conversion |
| Mac M1 16 GB, ~30 GB free after user cleanup | local | code, tests, analysis, dashboards, a few sample task packs; never the full external data |

Estimates to be replaced by measurements in the MVP:
- Generation: batched ≈ 400–700 tok/s → **~50–100 agent runs per GPU-hour**.
- Training (LoRA, 31B, full-sequence backward): naive pipeline across 4 GPUs ≈ 150–300 tok/s → **~25–50 runs per
  GPU-hour** at 20k tokens. FSDP over int4-packed weights could give ~3–4× (an engineering bet, not assumed).
- ⇒ Training tokens are the binding constraint, so data must be small and high quality.

### 1.3 Not possible or not worth it

- Full-parameter training; changing quantisation, tokenizer, vLLM flags or tool implementations.
- Full online RL at 31B (needs 10³–10⁴ H100-hours). Skipped (D1).
- Logit distillation from a larger Gemma (31B is the largest; other teachers have different tokenizers).
- Multi-agent RL with separate policies per role.

---

## 2. Diagnosis: where the gap is

Evidence from 31 traced runs (`experiments/multi/shared/analysis/E_aggregate.md`):
- **Capability at our budget is the main gap.** Of 21 runs whose intermediate states could be replayed against the
  hidden tests, only **4 ever reached a passing state**. Only 18/31 runs ever edited the file the gold fix touches.
- **Protocol and efficiency failures come on top of that.** `edit_file` 82 % malformed and `run_skill_script` 40 %
  malformed, vs `run_command` 0/412. 65 % of `read_file` calls re-read a file. First edit to the right file at a median
  of 163 s. 64 % of output is thinking. The model keeps editing after reaching green (lost 1 of 4 wins). Scratch files
  in 12/31 patches. `submit_patch` called in only 14/31 runs.
- Gemma 4 31B scores ~54 % on SWE-bench Verified (Vals.ai) with 32k output tokens per call and high reasoning. Under our
  ~10k-token budget its effective capability is far lower. **The target is capability at 10k tokens.**

Consequences for the method:
- **RFT sharpens but rarely expands.** Training on Gemma's own passing runs mostly converts pass@k into pass@1; RL with
  verifiable rewards rarely makes a model solve tasks it could not solve at all (Yue et al. 2025).
  ⇒ Measure Gemma's pass@k at our budget early: it is the ceiling for RFT.
- **A stronger teacher is the main source of new capability**, so the teacher bake-off (§8) is a core experiment.

---

## 3. Method

| Stage | What | Expected role |
|---|---|---|
| 1. SFT on teacher runs | Runs recorded in the training harness, from the teacher that wins the bake-off, filtered to passing and budget-feasible | New capability and correct protocol. Largest expected single jump, with a risk that off-style data hurts a strong base; guard with evals. |
| 2. RFT / expert iteration | Gemma samples k runs per training task under the real budget; keep passing ones (shortest first); step-mask failed runs (SRFT) | Sharpens what Gemma can do in *our* harness; a few points per round, flattening after 2–3 rounds |
| 3. Online RL | — | **Skipped** (D1) |

Additional loss options, used only if evals justify them: SRFT step masking (critic labels bad steps in failed runs;
their loss is masked), DPO/KTO on pass-vs-fail pairs of the same task.

"Budget distillation" is included in stage 2's framing: Gemma with a relaxed budget is also a bake-off teacher (§8).

---

## 4. Harness ↔ adapter loops (D3, D4)

- **Fast loop (harness):** prompts, sampling and thinking budgets, helper scripts, sub-agents. Iterated on the current
  adapter (or base) with cheap evals.
- **Slow loop (adapter):** trained against a frozen harness version (`contract_id`). Each new adapter is evaluated on the
  current best harness; the next harness iteration is tuned *around* that adapter before the next training round.
- Every run, adapter and eval result is stamped with `contract_id`. Cross-evaluation grid ≤ 2 harnesses × ≤ 3 adapters.
- **One adapter** on every agent (vLLM could serve up to 8; role-specific adapters only as a late ablation).
- **Data portability rule:** interfaces the model calls should change rarely, because every interface change makes
  earlier runs invalid as training data. The built-in tools never change, which is why the training harness is built on them.

---

## 5. Training harness v1 (D5)

### 5.1 Shape
- One `LlmAgent`. Tools: `run_command`, `read_file`, `submit_patch`, `get_status`.
- Edits go through `run_command` (heredoc, Python one-liner or `sed`): the only code channel with 0 malformed calls.
  `edit_file`/`write_file` are excluded (82 % / scratch-file problems). No skill scripts.
- Fixed system prompt skeleton and sampling/thinking config, versioned as `contract_id = v1`.

### 5.2 Why not the v22 skills harness for training
1. Nested skill arguments break the gemma4 tool-call parser (40 % malformed); training signal would be spent on a
   formatting problem that can be avoided.
2. Skill scripts are our code and change weekly, which would invalidate earlier training runs.
3. Multi-agent pipelines split a task into several conversations with different prompts; each reshape invalidates the data.

Helper scripts may return later *through `run_command`* (flat, short commands), if they win in the fast loop.

### 5.3 Localizer revisit criteria (after adapter v1)
Measure per run: peak context, context composition by source (read_file, run_command output, thinking, model text),
time to first edit of the right file, and re-read rate. Current data: median peak prompt 18.7k (max 28.8k), 1
compaction in 31 runs, so reading costs *time and focus* more than capacity. If reading still dominates after adapter
v1, add a localizer `agent_tool` in the fast loop and train adapter v2 on that harness (same shared adapter; each
sub-agent conversation is its own training example).

---

## 6. Environment

### 6.1 One environment for rollouts, training data and eval
Our replica (`mvp_v2/harness.py` + `agent_harness/`) emulates ADK, the subprocess sandbox, budgets, nudges, leak audits
and verification. It becomes `src/gemma_agent/env/`.

### 6.2 Fidelity fixes (required before generating training data)
1. **Remove the replica's compact-and-retry.** Verified in adk-python source: token-threshold compaction only fires when
   the last reported prompt count ≥ `token_threshold` (32,768 = `max_model_len`, so never on a successful call);
   sliding-window compaction runs after an invocation every 15 invocations, and a task has at most 4. On overflow the
   replica must behave like the official harness (agent error → `git diff` fallback).
2. **Token-level recording:** record the exact prompt/response token IDs vLLM saw/produced; a unit test asserts our
   training renderer reproduces them (chat template, tool declarations, reasoning replay).
3. **Per-task validity gate:** baseline fails and gold passes the *whole* target test files (the competition runs whole
   files, not SWE-bench test IDs). Stored as `env_ok` per task.

### 6.3 Task packs
Same schema as competition `tasks.jsonl` plus `snapshot.tgz` + wheels. Built on Kaggle CPU notebooks (internet on),
stored as private Kaggle Datasets, run offline on GPU notebooks exactly like the competition.

Conversion from SWE-bench-style datasets:

| Step | Work |
|---|---|
| Fields | 1:1 (`instance_id, repo, base_commit, patch, test_patch, problem_statement, hints_text, created_at`) |
| Snapshot | clone → checkout `base_commit` → `git fast-export` into a fresh repo (no future history) → `.tgz` |
| Environment | Python 3.13 + offline wheels, no Docker: download wheels per task, then verify an offline install. Old tasks mostly fail here, so start from recent tasks |
| Scoring | re-validate: base fails / gold passes the whole files touched by `test_patch` |
| Graphs/embeddings | not generated (§10) |

---

## 7. Tasks and data volumes

| Pool | Content | Use |
|---|---|---|
| `eval/public-129` | all public tasks | release-candidate eval; bake-off benchmark (§8) |
| `eval/fast-N` | fixed stratified subset (~40) | weekly iteration |
| `eval/gut-check` | `rich_4076` | smoke run for every pipeline change |
| `train/core` (D9) | recent real tasks from many repos (SWE-rebench-style), filtered to Python 3.13 + wheel-installable, env-validated | all training |

Targets (literature: SWE-Gym gains from 491 runs; SWE-smith scales log-linearly to 5k):
- **Per adapter version:** ~300–500 successful runs from 150–300 distinct tasks (≤ 2 per task).
- **Attempts:** ~1,000–2,000 depending on teacher pass rate. **Task pool:** ~500 validated external tasks (≥ 30 repos).
- **MVP:** 10–30 runs.
- Validation yield of external tasks is unknown; measure on the first batch and size the pull accordingly.

---

## 8. Teacher bake-off (D8)

### 8.1 Design
- **Candidates:** Big Pickle (OpenCode Zen, free, OpenAI-compatible); Qwen3-Coder-Next 80B-A3B self-hosted on Kaggle
  4×L4 (Apache-2.0; fitting and running on L4s is part of the test); Gemma 4 31B with a relaxed budget (e.g. 30 min,
  higher thinking).
- **Benchmark:** the available competition tasks (user decision), same training harness v1 for all.
- **Budget rule:** score both unconstrained and within a Gemma-imitable budget (≤ ~10k output tokens, ≤ 32k context),
  since runs Gemma cannot reproduce in 5 min are poor training data.
- **Metrics:** pass rate (both budgets), share of runs fitting the budget, conversion cleanliness into Gemma format
  (tool calls, thinking), throughput (GPU-h or rate limits), licence/terms for training use, data retention.
- **Output:** the teacher (or mix) for stage 1, plus Gemma pass@k at our budget as the RFT ceiling.

### 8.2 [OPEN] Big Pickle on competition tasks
OpenCode Zen states Big Pickle's collected data "may be used to improve the model". Sending competition tasks
(problem statements, snapshots) to it may conflict with the rule against making competition data available to
non-participants. Qwen and Gemma are self-hosted, so they are unaffected. Needs the user's confirmation before that
run; alternatives: benchmark Big Pickle on external tasks only, or on a separate external benchmark set that mirrors
the competition's format.

### 8.3 How teacher runs become Gemma training data
We do not need the teacher's tokens. We record messages, tool calls and tool results, then re-render them in Gemma's
chat template with Gemma's tokenizer. Teacher thinking (if exposed) is condensed or dropped to fit Gemma's thinking
budget; the exact policy is decided from the bake-off data.

---

## 9. Training stack

- **Base (exact):** int4 QAT weights dequantised to bf16 (`CompressedTensorsConfig(dequantize=True)`), ~62 GB over 4×L4
  via `device_map`. Not bnb-NF4 re-quantisation (wrong base). Speed-up bet: int4-packed Linear with on-the-fly
  dequantisation (~17 GB) + FSDP2.
- **LoRA:** rank 8–16 (vLLM issue #50059: rank ≥ 32 on compressed-tensors int4 gave weak, non-deterministic output),
  language-model attention + MLP projections only, LR ≈ 10× full-FT LR (LoRA Without Regret). Module names must match
  what vLLM expects for Gemma 4.
- **Sequences:** one sequence per run (turns are prefix-consistent), loss on assistant tokens only, chunked CE (262k
  vocab), gradient checkpointing, ≤ 32k. Prefix truncation is an exact, cheap way to train early turns.
- **Parity gate:** every adapter's logprobs on a fixed probe set must match between HF training and vLLM serving.
- **GPU time-sharing:** vLLM (rollouts/eval) and trainer alternate on the same 4 GPUs within a notebook.
- **DRY:** TRL/PEFT for losses and utilities, vLLM for serving and adapter loading, patterns from ART / TRL AsyncGRPO /
  mini-swe-agent. No framework adopted wholesale (they assume Docker or disaggregated GPU pools).

## 10. Rewards and filters

| Signal | Use |
|---|---|
| Target test files pass (competition metric) | primary filter/reward |
| Fraction of target tests passing | tie-break, curriculum |
| Protocol: calls parse, no scratch files, `submit_patch` called, within budget, fits 32k | hard filters |
| Efficiency: output tokens, wall-clock | prefer the shortest successful run |
| Critic step labels (open model only) | SRFT masking, never the primary signal |

Each component is logged separately so reward hacking is visible.

## 11. Graph and embedding tools (D10)

Facts: 66/129 public tasks have empty embeddings and 61/129 empty graphs (tools not offered there); "similar code"
search resolves an exact symbol name to a node and compares stored vectors (no text embedder); Gemma never used the
tools in 14+ runs; our graphs could be approximated from ASTs but embeddings cannot be reproduced. Any run showing
graph-tool use needs graph data in that task's environment, whoever generates it.
**Experiment E4:** base Gemma prompted to use graph tools vs not, on public tasks that have the data. Build a graph
generator for training tasks only if E4 shows a gain (also a paper-track candidate).

---

## 12. Observability (D11)

- **Run registry:** each Kaggle run writes `run_manifest.json` (run_id, kind, git SHA, `contract_id`, config hash, input
  dataset/model versions, adapter SHA-256, GPU-h) + `metrics.jsonl` + artifacts. A pull script downloads into
  `runs/<run_id>/` and appends to `runs/INDEX.csv`.
- **Dashboards:** static HTML generated from the JSONL files (extends the existing trace viewer). Trackio = option to
  revisit.
- **Trace storage policy:** full traces stay in Kaggle outputs; the Mac pulls summaries plus selected traces.

| Layer | Metrics |
|---|---|
| Env | setup success, `env_ok` rate per pool, sandbox errors, test runtime, infra-error rate |
| Agent | resolve rate (95 % CI), pass@k, submit rate, time-outs, context overflows, malformed calls by tool, re-read rate, time to first right-file edit, output/thinking tokens, peak context + composition, scratch-file rate, patch size, nudges |
| Reward | per-component mean/std, reward vs resolve correlation |
| Training | loss by turn index, grad norm, LR, tokens/s, KL to base on a probe set |
| Serving parity | HF-vs-vLLM logprob agreement per adapter; deterministic rerun agreement |
| Cost | GPU-h per run and per week vs budget |

Weekly auto-generated one-page report: budget burn, adapter × harness grid, top failure categories.

---

## 13. Repository reorganisation (D12, D13)

```
gemma-dev/
  CLAUDE.md, AGENTS.md      # identical agent map + rules (test enforces equality)
  README.md
  docs/                     # plans, decision log, our analyses (competition docs excluded from git)
  src/gemma_agent/
    env/ tasks/ rollout/ data/ train/ eval/ observe/
  harnesses/<contract_id>/  # declarative submission variants (v1 training harness, v22, candidates A–F)
  notebooks/src/            # notebook builders (single source of truth)
  notebooks/build/          # generated .ipynb (never hand-edited)
  kaggle/                   # kernel-metadata templates + push/pull/status scripts
  tests/
  runs/        (gitignored)
  data/        (gitignored) # competition data
  archive/                  # submission_v1–v3, old notebooks and builders
```

GitHub: private `azeezada/gemma-dev`. Excluded from git: `tasks.jsonl`, `snapshots/`, `graphs/`, `embeddings/`,
`wheels/`, `runtime_wheels/`, `HARNESS_README.md`, `documentation/rules.txt` and other competition-provided docs,
`sample_submission/` adapters, `work/`, `runs/`, caches, `.DS_Store`.

---

## 14. Kaggle workflow: train ↔ inference

**The competition scorer is never needed for training.** Training and eval run in our notebooks against our replica;
the competition submission is only a daily sanity check.

```
 Mac                         Kaggle CPU notebooks            Kaggle 4×L4 notebooks
 code + tests ─ code DS ──▶  task-pack build (internet) ──▶  datasets: task packs
                             API-teacher runs            ──▶  datasets: runs
                                                             train.ipynb (internet ON) → adapter/, metrics, manifest
                                                             eval.ipynb (internet OFF, = competition)
                                                               input: train output → eval report + submission.zip
 pull outputs ◀──────────────────────────────────────────── kaggle kernels output
 submit (only on explicit user OK): kaggle competitions submit -f submission.zip
```

- Claude pushes/polls/pulls notebooks with the Kaggle CLI (dry-run first; user approves GPU runs). User one-time UI steps:
  notebook secrets if ever needed, approving submissions.
- Code ships as a private Kaggle Dataset (`gemma-dev-code`), removing the 1 MB kernel-source cap problem.
- The eval notebook loads the adapter in vLLM exactly as the competition does and assembles `submission.zip`
  (harness YAML + prompts + `adapters/<name>/`).
- Early tiny-adapter submission (D14) validates the official adapter path.

---

## 15. Compute allocation (no timeline, D15)

Default weekly split of ~30 GPU-h: ~40 % training, ~30 % rollouts/teacher runs, ~25 % eval, ~5 % slack.
CPU notebooks carry task-pack building and API-teacher runs.
If MVP throughput is much worse than estimated, the plan shrinks to SFT + one RFT round.

---

## 16. Ordered work

1. **Foundations:** reorg + `git init` + private GitHub push; `CLAUDE.md`/`AGENTS.md`; env fidelity fixes (§6.2);
   training harness v1; eval pools; run registry + first dashboard.
2. **MVP notebook** (§17) end-to-end; throughput measured; parity gate; tiny-adapter submission (D14).
3. **Experiments:** E1 Gemma pass@k at our budget on competition tasks · E2 teacher bake-off (§8) · E3 graph tools (§11).
4. **Data engine:** core external task packs (§6.3, §7); teacher runs on external tasks.
5. **Adapter v1:** SFT on the winning teacher's runs → eval on the best harness → harness fast loop re-shaped around it.
6. **RFT rounds:** Gemma runs on train tasks → filter → retrain; localizer revisit (§5.3); stop when a round no longer
   beats the previous one on the fast eval (CI-aware).
7. **Freeze and select:** full-129 eval + 12 h timing dry run on the exact competition config; pick two final submissions
   (one conservative, one aggressive). Paper-track write-up if pursued.

---

### 16.1 Progress (2026-09-26)
Done: reorg (§13) incl. `git init` (**GitHub push pending user OK**); `CLAUDE.md`/`AGENTS.md`; §6.2.1 compact-and-retry removed
from `rollout/runtime.py` (legacy loops in `env/harness.py` and `env/v3.py` still compact; they are not used for rollouts);
`harnesses/train_v1` + contract tests; eval pools (`tasks/pools.py`); run registry + static dashboard (`observe/`).
Not done: §6.2.2 token-level recording parity test (needs the Gemma tokenizer), §6.2.3 per-task validity gate
(`env_ok`, needs Python 3.13 + wheels), weekly report, MVP notebook (§17), everything from step 2 on.

## 17. MVP notebook specification

One Kaggle notebook (`notebooks/build/train_mvp.ipynb`, 4×L4, internet ON), ~1.5–2 h; every stage writes metrics and
artifacts:

1. **Setup:** competition data, Gemma model, code dataset; pinned deps (vLLM 0.29.0 wheel, peft, compressed-tensors);
   manifest.
2. **Rollouts:** vLLM with competition flags; training harness v1 on a handful of tasks × k samples; token-level
   recording + rewards. Includes `rich_4076` as the gut check.
3. **Filter:** passing + protocol filters; fall back to a small bundled set if none pass, so every stage runs.
4. **Train:** stop vLLM; exact int4-dequantised base; LoRA r=16; SFT; loss and throughput logged.
5. **Parity:** HF+LoRA vs vLLM+LoRA logprobs on probes; fail loudly on mismatch.
6. **Eval:** vLLM with the adapter; base vs adapter on a few held-out tasks.
7. **Package:** `adapters/<name>/`, harness YAML with `adapter:`, `submission.zip` passing static checks,
   `run_manifest.json`, `metrics.jsonl`.

Success = completes and produces a loadable adapter + valid `submission.zip`. A score gain is not expected from the MVP.

---

## 18. Risks

| Risk | Mitigation |
|---|---|
| vLLM LoRA on compressed-tensors int4 unstable at higher rank | rank ≤ 16, parity gate, early tiny-adapter submission |
| Training base ≠ serving base | exact dequantised QAT weights; parity gate |
| Gemma 4 LoRA module names not what vLLM expects | verify on the first adapter |
| Off-style teacher data hurts a strong base | bake-off + eval every adapter against base on the same harness |
| External task validation yield too low | measure early; widen the source pool |
| Training throughput far below estimate | shrink data; FSDP/int4 speed-up bet |
| Replica drift from official harness | fidelity fixes (§6.2), token-recording test, LB sanity checks |
| Competition data leaking to third parties | GitHub exclusions; §8.2 decision before any third-party API sees competition tasks |

---

## 19. Open items

- **[OPEN] §8.2:** confirm whether Big Pickle may be benchmarked on competition tasks given its data-use terms, or use an
  external benchmark set for it.

---

## Sources

- Competition: `HARNESS_README.md`, `documentation/competition-overview.txt`, `documentation/dataset-description.txt`, `documentation/rules.txt`
- Trace analysis: `experiments/multi/shared/analysis/E_aggregate.md`
- adk-python compaction: `src/google/adk/runners.py` (post-invocation), `src/google/adk/flows/llm_flows/context/_compaction.py` (pre-request token threshold) · https://google.github.io/adk-docs/context/compaction/
- vLLM LoRA + compressed-tensors int4: https://github.com/vllm-project/vllm/issues/50059
- HF compressed-tensors loading modes: https://huggingface.co/docs/transformers/en/quantization/compressed_tensors
- Gemma 4 31B on SWE-bench Verified (Vals.ai): https://www.vals.ai/models/google_gemma-4-31b-it
- Gemma 4 31B config: https://huggingface.co/google/gemma-4-31b-it
- Unsloth Gemma 4 training: https://unsloth.ai/docs/models/gemma-4/train
- LoRA Without Regret: https://thinkingmachines.ai/blog/lora/
- RL and pass@k (Yue et al. 2025): https://arxiv.org/abs/2504.13837
- Step Rejection Fine-Tuning: https://blog.jetbrains.com/research/2026/06/step-rejection-fine-tuning/
- SWE-Gym: https://arxiv.org/pdf/2412.21139 · SWE-smith: https://github.com/SWE-bench/SWE-smith · SWE-Lego: https://arxiv.org/abs/2601.01426 · R2E-Gym: https://github.com/R2E-Gym/R2E-Gym
- Kimi-Dev: https://arxiv.org/abs/2509.23045 · SWE-RL: https://arxiv.org/pdf/2502.18449 · Orchard: https://arxiv.org/abs/2605.15040
- Qwen3-Coder-Next: https://huggingface.co/Qwen/Qwen3-Coder-Next
- OpenCode Zen: https://opencode.ai/docs/zen/
- Anthropic consumer terms: https://anthropic.com/legal/terms
- RL library landscape: https://huggingface.co/blog/async-rl-training-landscape · TRL AsyncGRPO + LoRA: https://huggingface.co/blog/asyncgrpo-lora-hfjobs · OpenPipe ART: https://github.com/OpenPipe/ART
- Trackio: https://github.com/gradio-app/trackio
- Competitor repo: https://github.com/happyc0der/gemma-swe-agent
