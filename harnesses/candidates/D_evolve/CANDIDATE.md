design: Spec-first evolutionary patch search: scout writes a discriminating executable spec → 3 parallel whole-function proposals → script scores + keeps best → 2 feedback generations → finisher submits best
basis: CodeAct (code via heredoc), CODESTRUCT (AST-entity edits), generate-and-validate APR / PSearch / LEVI, discriminating fail-to-pass evidence (2607.28871), REAgent (executable requirements), Coherence Collapse (best-so-far)
owner: research-D

# D_evolve: spec-first evolutionary patch search

Research review: `shared/research/D_methods.md`.

## Method
```
SequentialAgent spec_evolve
├─ scout      (low thinking, ≤8 calls) find_candidates detail:2 → focus.py path::Qual → heredoc /tmp/d_spec.py → spec.py (must FAIL on original)
├─ gen1       ParallelAgent g1a/g1b/g1c (temps 0.2/0.6/0.9; angles: minimal / full rewrite / other layer)
│             each: context.py (1 call, everything it needs) → heredoc /tmp/d_cands/<id>.py → one-line reply
├─ judge1     (thinking NONE) trial.py: apply each candidate to ORIGINAL code, run spec + related tests, rank, leave best applied
├─ gen2       ParallelAgent g2a (repair best) / g2b (different approach); reply SOLVED when STATUS is SOLVED
├─ judge2     trial.py
├─ gen3       ParallelAgent g3a / g3b (same, TIME LOW aware)
└─ finisher   (thinking NONE) trial.py → final.py (original + best, scratch removed) → submit_patch
```
Nothing in the model's tool arguments contains code. Code travels in heredocs (spec, candidates). Skill args
are flat (`target`, `id`) or empty. A candidate is `# target: path.py::Qual.name` followed by a complete `def`. The
script finds the node by AST, re-indents the code, keeps the original decorators and docstring when they are omitted,
and hoists new imports. `# replace: path.py` with `<<<<<<< / ======= / >>>>>>>` covers module-level code, with
indentation-tolerant matching.

## Why it can pass (generic, not task-specific)
- **No coherence collapse.** Candidates always apply to HEAD, and the workspace always holds the best-ranked candidate,
  so the git-diff fallback is the best patch even if the run times out mid-generation.
- **No edit-format failures.** Whole-function rewrites never need an exact `old` string (run 1: A lost 3 edits to this).
  A broken candidate is reported and ignored; it never corrupts the tree.
- **Output-token economy.** Proposals are short (~150–300 tokens) and parallel (batched by vLLM). Scoring and
  finishing run without thinking. Long inputs (spec, code, callers, report) are cheap.
- **Selection signal that handles tests which pin old behavior.** Rank = spec passes → PASS−FAIL checks →
  regressions (new failures in tests *outside* the changed module's own `test_<module>.py`) → touches the focused function
  → own-test changes → diff size. On the rich_4076 snapshot this ranks the root-cause fix above the caller-level
  workaround (AMBER vs ORANGE), whereas "fewest new failures" would pick the wrong one (3 vs 2).
- **Discriminating spec enforced.** spec.py refuses specs that pass on the original code or crash before checking.

Local evidence: `tests/` (7 tests: script unit tests + the full agent tree through the local runtime with a scripted
model, which submits the best of 3 generations). Exercised on rich_4076 (best candidate → all 24 hidden tests pass) and on
fastapi_14492 (module-level replace block, GREEN in 2.8 s).

## Risks
- **Scout quality decides everything.** A wrong spec (wrong expected values) optimizes toward the wrong behavior. A
  spec that crashes falls back to "apply the focused candidate". The scout gets ~100 s; slow scouting squeezes the generations.
- **Wrong focus.** Proposers see only the focused function(s) and callers. g1c/g2b may change another function, but
  only one named in context.
- **Multi-file or new-file fixes** are supported only via several `# target:`/`# replace:` blocks; new files aren't.
- 11 LlmAgents means per-agent overhead. A nudge re-run of the root repeats the scout (the state in /tmp survives, so
  earlier candidates are kept).
- `/tmp/d_*` must be cleared between harnesses in the notebook (state auto-resets when the workspace path changes).
