# Harness Research Plan — Gemma 4 Developer Agent (no fine-tuning)

*2026-09-25 · follows Kaggle trace v22 (version 352600760)*

## 1. Why this document exists

Across v19–v22 (≈20 traced runs on the 7 core tasks) we have tried to steer one free-roaming Gemma agent with a longer prompt and better tools. The model ignores rules, skips tools it was given, and repairs its own mistakes badly:

| Symptom (measured) | Runs | What we tried | Result |
|---|---|---|---|
| Literal `\n` in `edit_file` args → edit fails | v19–v20: 88/88 such calls failed | Prompt rule, then `apply_edit` skill | Moved to nested skill args: v22 29/78 skill calls malformed |
| Anchors on first grep hit, ignores the ranked data-loss hint | rich_4076 in v19, v20, v21, v22 | "Ranked matches are hypotheses", operation map | v22: `ansi.py:135 splitlines` was the first operation-map line on turn 1, and the model skipped it |
| Probes that only confirm its own guess | rich_4076, rich_3468 | "Make a discriminating probe" rule | Built `Lines([Text('hello \n')])`, input that rich itself never produces |
| Tools it was given go unused | graph tools 0/14 runs, `run_tests` 1 call in v22 | Listed them in the prompt | Unused |
| Scratch files in the patch | 9/14 (v19–20), 2/5 (v22) | "Scratch goes in /tmp" | Still happens |
| Out of time | 4/5 valid v22 runs | Shorter thinking budget | Still happens |

The question is what the recent literature says about getting reliable behaviour from a small model that isn't trained for this, **without fine-tuning**, and inside the competition's rules.

## 2. What the competition allows (the design space)

From `HARNESS_README.md` and the competition rules:

- **Declarative only:** YAML agent trees (`LlmAgent`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`), up to 500 agents, nesting depth 50. No Python entrypoints. Skill scripts (`.py`) run in the sandbox.
- **Per-agent control:** `tools` subset, `skills`, `instruction`, `include_contents: none`, `output_key` → `{state}` templating, and `generate_content_config` (temperature, top_k, seed, stop sequences, `response_mime_type`, thinking budget/level). `response_schema` is **not** allowed.
- **Built-in tools (9):** `run_command`, `submit_patch`, `get_status`, `read_file`, `edit_file`, `write_file`, `get_code_neighbors`, `search_similar_code`, `get_code_subgraph`.
- **Runner:** it drives the root agent with `run_async` until `submit_patch`, a budget limit, or 3 consecutive nudges. If a turn ends without `submit_patch`, the whole root is re-run with a nudge message. If no patch was submitted, the fallback is `git diff HEAD`.
- **Budget:** 12 h for ~129 tasks including setup, so about 5 min per task. One vLLM server (4×L4, TP=4, 32k context) serves every agent, which means **concurrent requests from `ParallelAgent` branches are batched**. Wall-clock cost of N parallel samples is well under N×.
- **Single workspace:** parallel branches share one container filesystem. Parallel *editing* therefore needs isolation, either `git worktree` copies under `/tmp` via a skill script or candidates kept as text diffs.
- LoRA adapters are allowed but out of scope for this plan (no fine-tuning).

## 3. Literature review

Citation counts are from Semantic Scholar on 2026-09-25. Many of the most relevant papers are from 2026 and too new to be highly cited; their counts are shown so their weight can be judged.

### 3.1 Workflow over agency

| Paper | Cites | Core finding | Relevance |
|---|---|---|---|
| **Agentless** — Xia et al., FSE 2025 ([2407.01489](https://arxiv.org/abs/2407.01489)) | 505 | A fixed localize → repair → validate pipeline where the LLM never chooses the next action. It led open-source systems on SWE-bench Lite (32.7%, $0.68/task) and was adopted by OpenAI, Meta and DeepSeek for model evaluation. | Removes the decision our model makes badly. |
| **Kimi-Dev** — 2025 ([2509.23045](https://arxiv.org/abs/2509.23045)) | 32 | Agentless-style "BugFixer + TestWriter" roles reach 60.4% on SWE-bench Verified. Execution-based self-play with 3 patches × 3 tests beats majority voting over 40 patches. | Cross-checking patches against generated tests beats sampling more patches. (Its training parts are out of scope; the workflow result isn't.) |
| **AgentFloor** — Karmakar & Chatterjee, May 2026 ([2605.00334](https://arxiv.org/abs/2605.00334)) | 0 | Tests **gemma4:26b** directly. It matches GPT-5 on single-tool and short-chain tasks, but no model exceeds 60% reliability on long-horizon tiers. Gemma's failures are **step exhaustion (41%)** and **hallucinated tools (36%)**. A **generic plan/execute/submit decomposition regressed every model (−5 to −33 pp)**. | The only paper on our model family. It warns that naive phase splitting can hurt. |

### 3.2 Harness components (interface, context, planning)

| Paper | Cites | Core finding | Relevance |
|---|---|---|---|
| **SWE-agent** — Yang et al., NeurIPS 2024 ([2405.15793](https://arxiv.org/abs/2405.15793)) | 1853 | The agent–computer interface (compact search, a windowed viewer, an edit command that rejects syntax errors) drives performance for a fixed model. | Guardrails built into tools beat rules written in the prompt. |
| **An Empirical Study of Harness Design for Coding Agents** — Fan et al., Sep 2026 ([2609.20804](https://arxiv.org/abs/2609.20804)) | 0 | 176 matched settings. **Planning is an accuracy scaffold for weaker models.** **Predefined tools help weaker models**, while bash-only suits strong ones. Context management mostly helps by preventing overflow, and rule-based elision works best. | Supports giving Gemma structured tools and an explicit plan, not a bare shell. |
| **SWE-Edit** — Zhang et al., Apr 2026 ([2604.26102](https://arxiv.org/abs/2604.26102)) | 4 | Separating a **Viewer** (extracts code) from an **Editor** (applies a natural-language change instruction) fixes "context coupling": +2.1 pp and −17.9% cost. Choosing the edit format matters a lot for small models. | Supports a dedicated edit sub-agent with its own clean context, so the main agent never writes nested code strings. |
| **Coherence Collapse** — Kim et al., Mar 2026 ([2603.24631](https://arxiv.org/abs/2603.24631)) | 4 | **60–69% of failures reach and edit the correct function** and then corrupt a correct patch later. An edit-commit checkpoint recovered every case studied; a consensus variant gave +3.0 pp. | We have this in our own runs (fastapi_14258 had the right check, then duplicated it into `__init__`). Checkpointing is cheap. |
| **OPENDEV / Building Effective AI Coding Agents for the Terminal** — Bui, Mar 2026 ([2603.05344](https://arxiv.org/abs/2603.05344)) | – | Separate planning from execution; adaptive compaction of old observations; **event-driven system reminders to counter "instruction fade-out"**. | Explains why rules at the top of a prompt stop being followed mid-run. |
| **Survey: Agent System & Harness Design** — Jun 2026 ([2606.20683](https://arxiv.org/abs/2606.20683)) | – | Treats the harness as six parts: observation, context, control loop, action, state, verification. Argues the harness is an optimization target in its own right, independent of model scale. | A vocabulary for describing our options. |

### 3.3 Localization

| Paper | Cites | Core finding | Relevance |
|---|---|---|---|
| **LocAgent** — Chen et al., ACL 2025 ([2503.09089](https://arxiv.org/abs/2503.09089)) | 112 | A code graph (files, classes, functions plus imports, calls, inheritance) with multi-hop search reaches 92.7% file-level localization. | The competition ships these graphs and embeddings for every task, and we have never used them successfully. |
| **CoSIL** ([2503.22424](https://arxiv.org/abs/2503.22424)), OrcaLoca, GraphLocator ([2512.22469](https://arxiv.org/abs/2512.22469)) | – | Call-graph search finds suspicious code the issue never mentions. CoSIL improves function-level localization by 8.6–98.2%. | Exactly rich_4076: `Text.from_ansi → AnsiDecoder.decode → splitlines`. |

### 3.4 Test-time scaling and selection

| Paper | Cites | Core finding | Relevance |
|---|---|---|---|
| **CodeMonkeys** — Ehrlich et al., 2025 ([2501.14723](https://arxiv.org/abs/2501.14723)) | 53 | Parallel trajectories that each write code *and* tests, then selection by test voting plus a selector trajectory: 57.4% on Verified. | A template for parallel samples with a selector. |
| **DARS** — Aggarwal et al., 2025 ([2503.14269](https://arxiv.org/abs/2503.14269)) | 25 | Branch the trajectory only at key decision points, using execution feedback. Training-free. | Cheaper than full parallel runs; fits the 5-min budget. |
| **TestPrune / "Can Old Tests Do New Tricks?"** — Chen et al., 2025–26 ([2510.18270](https://arxiv.org/abs/2510.18270)) | 8 | Minimizing *existing* tests to a relevant subset gives +8–13% relative resolution across Agentless, SWE-agent and Trae. | Deterministic and cheap. The repo's existing tests are our strongest outside signal. |
| **General AgentBench** — Li et al., Feb 2026 ([2602.18998](https://arxiv.org/abs/2602.18998)) | – | Sequential scaling hits a "context ceiling"; parallel scaling hits a "verification gap". Neither helps without a good selector. | Parallel sampling pays only if selection is execution-based. |

### 3.5 Automated harness optimization

| Paper | Cites | Core finding | Relevance |
|---|---|---|---|
| **Agentic Harness Engineering (AHE)** — Lin et al., Apr 2026 ([2604.25850](https://arxiv.org/abs/2604.25850)) | 84 | A closed loop in which a coding agent edits harness components as files, reads distilled traces, and predicts each edit's effect before it is checked. Terminal-Bench 2 went from 69.7% to 77.0% in 10 iterations, and the frozen harness transfers to SWE-bench Verified. | Our local harness already produces the traces and per-component files this needs. |
| **Meta-Harness** — Lee, …, Khattab, Finn, Mar 2026 ([2603.28052](https://arxiv.org/abs/2603.28052)) | – | Claude Code with full access to past harness code, traces and scores searches harness space. Harness changes alone produce up to 6× gaps on the same model, and the discovered harnesses transfer to unseen models. | The same approach as AHE, with Claude as the optimizer. |
| **Meta Context Engineering** — Ye et al., Jan 2026 ([2601.21557](https://arxiv.org/abs/2601.21557)) | 37 | Evolves context-engineering *skills* (files and code) with agentic crossover. +5.6–53.8% relative over hand-designed context engineering. | Our skills and prompts are exactly the files it evolves. |

### 3.6 Consensus and conflicts

**Where the literature agrees:**
1. Put structure in tools and harness mechanics, not prose. SWE-agent, SWE-Edit, the harness study and Coherence Collapse all agree.
2. Weaker models benefit from predefined tools and explicit planning (harness study).
3. Repository tests and execution feedback are the strongest signal for selecting or verifying a patch (TestPrune, Kimi-Dev, CodeMonkeys).
4. Once a correct state is reached, protect it (Coherence Collapse).
5. Graph-guided localization beats lexical search for issues that don't name the buggy code (LocAgent, CoSIL).

**Where it conflicts, and why we have to test:**
- **Fixed workflow vs. agent loop.** Agentless and Kimi-Dev win with fixed workflows. AgentFloor found generic phase decomposition *hurt* Gemma. The harness study found planning *helps* weak models.
- **One long trajectory vs. many short ones.** CodeMonkeys and DARS gain from parallelism. General AgentBench found parallelism gives nothing without good verification. Our 5-minute budget makes this even less certain.
- **Hand-designed vs. searched harness.** AHE, Meta-Harness and MCE beat hand design, but they were demonstrated with frontier models and much larger evaluation budgets than our 129 tasks.

## 4. Where we are now vs. what we do next

| Harness part | Now (v22, `submission_v4`) | Next (applies to every candidate below) | Basis |
|---|---|---|---|
| Control loop | One `LlmAgent` chooses every action | Chosen per candidate (§5) | §3.6 conflict |
| Action interface: edit | `apply_edit` with nested `args.edits[]` holding code; 37% of skill calls malformed | No code inside nested tool args. Either a flat top-level `edit_file(old, new)` behind a normalizing skill, or an **Editor sub-agent** given a natural-language instruction. | SWE-Edit, v20/v22 measurements |
| Action interface: search | `find_candidates` (lexical) plus operation map; graph tools dropped | Graph-guided localization using the provided `graphs/*.json` and `embeddings/*.npz`: callers and callees of candidate sites up to the public API | LocAgent, CoSIL |
| Verification | Optional `run_tests`, called once | **Mandatory and deterministic:** TestPrune-style selection of relevant existing tests plus a reproduction test, run by the harness step and not left to the model | TestPrune, Kimi-Dev |
| State protection | None: later edits overwrite good ones | **Checkpoint on every green verification** (`git stash`/commit in a scratch ref). At the end, submit the best checkpoint, not the last state. | Coherence Collapse |
| Context | Full history plus replayed reasoning; peaks of 28k tokens | Phase agents with `include_contents: none` and state passed through `output_key`; rule-based elision of old tool output | Harness study, OPENDEV |
| Instruction | 847-word prompt with ~30 rules | Per-phase prompts under ~150 words with one job each; short reminders re-injected in tool results | OPENDEV "instruction fade-out", AgentFloor |
| Hygiene | Scratch files leak into the patch | The submit step strips untracked files outside the source and test trees | v19–v22 |
| Infra | httpbin wheels missing, so the notebook crashed | Embed only `httpbin`, `pytest_httpbin` and the dependencies Kaggle lacks | v22 log |

## 5. Candidate architectures (mutually exclusive; test before choosing)

Each candidate is a complete `agent.yaml` tree built from the shared "Next" parts in §4. They conflict: one fixed pipeline, one free agent, one parallel search, and a meta-method. The literature doesn't pick a winner for Gemma 4 31B under a 5-minute budget, so we measure.

### Candidate A — "Agentless-on-ADK" (fixed pipeline, no free agency)
*Basis: Agentless, Kimi-Dev, LocAgent, TestPrune, SWE-Edit.*

```
SequentialAgent root
├─ localize      LlmAgent  tools: [graph skill]         out: ranked edit sites   (picks from a list)
├─ reproduce     LlmAgent  tools: [write_repro skill]   out: repro test + pruned existing tests
├─ repair        ParallelAgent ×3 (temp 0.2/0.6/0.9)    out: search/replace diffs as TEXT (no tools)
├─ validate      LlmAgent  tools: [evaluate_candidates] runs each diff in /tmp worktree
└─ submit        LlmAgent  tools: [apply_best, submit_patch]
```
- **Model decides:** which listed site, what the diff is, which test captures the issue. It never decides *what to do next*.
- **Expected upside:** removes anchoring and tool-choice failures, and parallel samples are batched by vLLM.
- **Main risk:** AgentFloor's result that decomposition hurt Gemma. Vague issues (rich_4076) depend entirely on localization quality. The runner's nudge re-runs the whole tree if `submit` doesn't fire.

### Candidate B — "Minimal guarded agent" (one ReAct loop, strong interface)
*Basis: SWE-agent ACI, harness-design study, Coherence Collapse, AgentFloor, OPENDEV.*

```
LlmAgent root (short prompt; 5 flat tools)
  tools: read_file, run_command, edit (flat args, syntax-checked), verify (auto-selected tests + checkpoint), submit
  + AgentTool "editor" for multi-line edits (fresh context, NL instruction in)
```
- **Model decides everything**, but every tool has built-in safeguards. `verify` checkpoints each green state automatically. `submit` picks the best checkpoint and removes scratch files. Reminders are appended to tool output.
- **Expected upside:** keeps the flexibility AgentFloor found Gemma needs, and fixes the failure classes that are about mechanics rather than reasoning (edits, lost correct states, scratch files).
- **Main risk:** leaves the reasoning failure (rich_4076 anchoring) mostly unaddressed. This is closest to what has already failed four times.

### Candidate C — "Parallel short agents + execution-based selection"
*Basis: CodeMonkeys, DARS, Kimi-Dev self-play, TestPrune; the General AgentBench caution.*

```
SequentialAgent root
├─ testwriter    LlmAgent   out: repro test(s) (independent of any patch)
├─ ParallelAgent ×3 short B-style agents, each in its own /tmp worktree, 2-min cap, no submit
└─ judge         LlmAgent   tools: [cross_validate] runs every patch × every test + pruned regressions,
                            picks the best, applies it, submit_patch
```
- **Model decides:** everything inside each short agent, plus a final selection informed by the test matrix.
- **Expected upside:** diversity rescues anchoring, since one of three trajectories may pick `ansi.py`. Selection is execution-based, which addresses the verification gap.
- **Main risk:** per-task wall clock and KV cache (3 contexts of up to ~20k on 32k TP=4). Needs isolated worktrees through skill scripts. If the test writer is wrong, the vote is wrong.

### Candidate D — "Searched harness" (automated harness engineering)
*Basis: AHE, Meta-Harness, Meta Context Engineering.*

- Don't pick A, B or C by hand. Run an AHE-style loop in which Claude Code is the optimizer. It edits `agent.yaml`, prompts and skills as files, reads distilled traces from our local harness (`agent_harness`, `mvp_v2`), predicts each change's effect, and keeps changes that improve a **held-out** split.
- **Starting points:** A, B and C as seeds.
- **Expected upside:** the literature's strongest gains came from searching, not hand design, and we already have the traces and components as files.
- **Main risk:** compute. Each iteration needs Gemma rollouts, and our local harness is a CPU emulation, so rollouts need Kaggle GPU or a matching server. Also a high risk of overfitting 129 tasks. It conflicts with A, B and C for the same time budget.

### Ruled out
- **Expert–protégé collaboration** (SWE-Protégé, [2602.22124](https://arxiv.org/abs/2602.22124)): the single-model rule forbids a second model at evaluation time.
- **PRM-guided search** (SWE-TRACE, [2604.14820](https://arxiv.org/abs/2604.14820)): requires a trained reward model.
- **Any LoRA work:** excluded by this plan's no-fine-tuning scope, not by the rules.

## 6. How we choose (experiment protocol)

1. **Common fixes first** (§4 "Next" rows that don't depend on architecture): httpbin wheels, flat edit args, scratch-file stripping, checkpointing. Re-run v22 as **B0** so the candidates have a clean baseline.
2. **Evaluation set:** the 7 core tasks plus the 13 locally validated extras (20 tasks), split 12 dev / 8 held-out. Include at least 3 vague-statement tasks such as rich_4076.
3. **Metrics per candidate:**
   - pass rate
   - localization hit@3 (does the edited file contain the gold file?)
   - fraction of malformed tool calls
   - fraction of runs that reached a green state and then lost it
   - wall clock p50/p90 against the 5-minute budget
4. **Order:** A and B are the cheapest to build and represent the main conflict (pipeline vs. agent), so run them first on Kaggle in one notebook, same seed and same tasks. Then build C using the winner's components. Start D only if A, B and C plateau below target, seeded with the best of them.
5. **Decision rule:** a candidate wins if it beats B0 on held-out pass rate by ≥2 tasks without exceeding a p90 wall clock of 5 min. On a tie, prefer the simpler tree.

## 7. Open questions to verify before building

- **Nudges with a workflow root:** does a nudge re-run a `SequentialAgent` root from the first sub-agent in the official runner? This decides whether A and C need a guard (e.g. a `localize` step that returns cached state when it already exists).
- **Isolated worktrees:** does `ParallelAgent` concurrency plus `git worktree` under `/tmp` work inside Container A with the 30 s command timeout?
- **KV cache:** vLLM KV headroom for 3 concurrent ~20k-token contexts on 4×L4 with the w4a16 model.
- **`response_mime_type: application/json`:** does it constrain Gemma's output through vLLM's guided decoding? If so, the localize and judge steps can emit structured choices.
