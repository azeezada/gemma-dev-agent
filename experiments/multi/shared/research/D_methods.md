# D: methods not yet covered by A/B/C (research-D, 2026-09-25)

Scope: training-free methods that work with a ~31B open model (Gemma 4 31B w4a16) under our constraints:
300 s agent time, ~30 sequential calls, cost ≈ 2.9 s per 100 **output** tokens (inputs nearly free,
per deepdive-E's latency model), parallel branches batch well, and code inside nested tool-call
args breaks the gemma4 parser. A = fixed Agentless pipeline. B = guarded single ReAct agent. C = parallel
full ReAct solvers + LLM judge.

## Review

| # | Method (citation) | Core idea | Evidence for small / open models | Fit to our constraints | Covered by A–C? |
|---|---|---|---|---|---|
| 1 | **CodeAct**, Wang et al., ICML 2024 ([2402.01030](https://arxiv.org/abs/2402.01030)); OpenHands | Actions are executable Python, so one action can search, edit and test | +up to 20% success across 17 LLMs incl. open 7–70B; the basis of OpenHands | High: code travels in a heredoc (the channel that worked in v20), not in nested JSON args. Fewer calls. | No: A–C use per-op tools |
| 2 | **CODESTRUCT**, 2026 ([2604.05407](https://arxiv.org/abs/2604.05407)) | Read/edit **AST entities** (functions, classes) instead of text spans; syntax-validated | +1.2–5.0% on SWE-bench Verified across 6 models; **GPT-5-nano +20.8%; empty-patch failures 46.6% → 7.2%**; 12–38% fewer tokens | High: removes the `old`-string-not-found failure (A run 1: 3 failed edits) | No: all use search/replace |
| 3 | **Generate-and-validate / LLM-as-mutation-operator** (Conversational APR [2301.13246](https://arxiv.org/abs/2301.13246); PSearch [2507.01827](https://arxiv.org/abs/2507.01827); "Aligning the objective of LLM-based APR" [2404.08877](https://arxiv.org/abs/2404.08877); LEVI [2605.09764](https://arxiv.org/abs/2605.09764)) | Sample several full-function rewrites, validate by execution, feed failures back into the next generation | 2404.08877: whole-function refinement with 10 samples beats SOTA APR with perfect FL. LEVI: a stronger *search architecture* substitutes for a larger LLM in evolutionary search | High: one-shot proposals are short outputs, run in parallel, and are scored by a script, not by an LLM judge | Partly: C samples whole trajectories. No one samples *patches* and evolves them |
| 4 | **Discriminating executable specification** ("Validation evidence in LLM repair agents" [2607.28871](https://arxiv.org/abs/2607.28871); "Rethinking the value of agent-generated tests" [2602.07900](https://arxiv.org/abs/2602.07900); SWT-Bench; BRT cogeneration [2601.19066](https://arxiv.org/abs/2601.19066)) | A test only counts if it FAILS on the original code (fail-to-pass). 46% of "passing" evidence in agent runs has no bug-discriminating value. Showing baseline outcomes improves evidence quality by 7.4–7.8 pp | 2602.07900: agent-written tests *as usually written* (print-style) don't change resolve rates. They need to be assertion-based and discriminating | High: a script can enforce "spec must fail on original" mechanically | Partly: B/verify runs a repro but doesn't enforce discrimination |
| 5 | **Existing tests as executable requirements** (IssueExec [2607.17286](https://arxiv.org/abs/2607.17286); TestPrune [2510.18270](https://arxiv.org/abs/2510.18270)) | Tests cover 97% of gold files; trace-based localization +41.6% function-level R@1; +17.7% resolved in Agentless | Deterministic, model-agnostic | Medium: useful as a regression signal for ranking. Must treat old-behavior tests as "changed", not "broken" (FACTS #4) | Partly (verify.py) |
| 6 | **Spectrum-based fault localization** (AutoCodeRover [2404.05427](https://arxiv.org/abs/2404.05427); FL-context study [2604.05481](https://arxiv.org/abs/2604.05481)) | Coverage of failing vs passing runs ranks suspicious methods | FL study: file-level localization is 15–17× the value of no-file; line-level expansion often backfires | Low marginal value **here**: localization is already H1 on call 1 (FACTS #2) | No |
| 7 | **Tree search** (SWE-Search MCTS [2410.20285](https://arxiv.org/abs/2410.20285); DARS [2503.14269](https://arxiv.org/abs/2503.14269)) | Branch at key decision points, value-guided | DARS is training-free, but gains come with many rollouts | Low–medium: too many calls for 300 s. A 2-generation population search captures the cheap part | Partly (C) |
| 8 | **Thinking economy** (reasoning-sandwich pattern; DART adaptive budgets [2606.23181](https://arxiv.org/abs/2606.23181)) | Spend reasoning where decisions are made, none on mechanical steps | Budget studies show diminishing returns; our latency model makes output tokens the entire cost | High: `thinking_level` per agent is official (README l.144) | No |
| 9 | **Requirement-first issue understanding** (REAgent [2604.06861](https://arxiv.org/abs/2604.06861)) | Turn the issue into structured, checkable requirements before patching | +17.4% resolved on average over 5 baselines, training-free | High when requirements are *executable* (= #4) | No |
| 10 | **Pre-action verification / content-anchored edits** ([2609.11957](https://arxiv.org/abs/2609.11957)) | Deterministic checks before edits apply. Anchor-and-verify gives 1 misapplication in 8,320 | Model-agnostic | High: validate syntax + anchor before touching the workspace | Partly (apply_edit compile check) |
| 11 | **Checkpoint consensus / never lose green** (Coherence Collapse [2603.24631](https://arxiv.org/abs/2603.24631)) | 60–69% of failures reach the right function and then corrupt the patch | Model-agnostic | High: in a population search the best-so-far is kept by construction | B only (checkpoints) |
| 12 | Edit-format studies (SWE-Edit [2604.26102](https://arxiv.org/abs/2604.26102); Agentless) | Search/replace is cheap but brittle; whole rewrite is robust but costly. Small models benefit from choosing | SWE-Edit: format choice matters more than editor scale | High: whole-*function* rewrite sits between the two and is robust at function granularity | No |

## Takeaways (posted as FINDINGs)

1. **Channel code through heredocs and AST targets, never through nested JSON args.** A candidate
   file `# target: path::Qual.name` + full new `def` fixes the arg-escaping and old-string classes of failure.
2. **Specs must be discriminating.** Run the spec on the original code. It must FAIL there,
   otherwise it proves nothing (2607.28871). Assertion-style `check(label, got, want)` with one
   PASS/FAIL line per check gives partial credit for ranking.
3. **Search over patches, not trajectories.** Short proposals (≈150–300 output tokens ≈ 5–9 s) in parallel,
   scored by a script, beat long agent loops under an output-token-priced budget.
4. **Thinking only where a decision is made.** Understanding the issue and writing the spec need reasoning.
   Proposing a rewrite of a function already in view needs little, and scoring needs none.
5. **Rank by spec first, then by existing-test changes** (tests that changed are reported, not vetoed),
   then by diff size. Keep best-so-far across generations, so a green state can't be lost.

## Chosen design for D: "D_evolve" (spec-first evolutionary patch search)

Combination of #1 (code-as-action via heredoc), #2 (AST-entity edits), #3 (population +
feedback-driven second generation), #4/#9 (discriminating executable spec), #8 (thinking economy),
#11 (best-so-far by construction). It is none of A, B or C: no free agent edits the repo, no LLM judges
patches, and the only thing sampled in parallel is a patch, not a trajectory.
See `candidates/D_evolve/CANDIDATE.md`.
