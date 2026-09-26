design: ParallelAgent of 3 solvers started at t=0 (own repro + checkout each; tools enforce a deadline, checkpoint green edits and auto-adopt the best into /workspace) → judge cross-checks every patch against every repro and submits
basis: CodeMonkeys, Kimi-Dev self-play, Agentless voting, Coherence Collapse, OPENDEV event reminders
owner: path-C agent

# C_parallel v2: parallel short solvers + execution-based selection

## Problems in run 1 (evidence: `work/kaggle-multi-v1/multi_runs/C_parallel.digest.txt`)
1. **The serial scout starved the parallel phase.** The scout used 202 s over 29 calls, reading text.py, _wrap.py, wrap, split
   and rstrip_end, even though H1 (the right site) was on its first tool result. Three solvers then had about 100 s.
2. **About half the solver calls were argument errors** against wt.py: `command` instead of `cmd`, `positional_args`
   (both inside and outside `args`), `start_line` instead of `start`, reading `/tmp/repro_issue.py` (rejected as outside
   the checkout), and `skill_name` nested inside `args` (rejected by ADK before any script runs).
3. **No solver edited, the judge never ran, and nothing was adopted**, so the patch was empty. The result depended on the
   last LLM step being reached.
4. (Found while redesigning, measured locally.) Ranking by "fewest new test failures" picks the wrong fix on this task.
   The root-cause fix breaks 3 old tests that assert the old behavior; a higher-layer workaround breaks 2.

## Changes
- **No scout.** `SequentialAgent[ParallelAgent[solver_1, solver_2, solver_3], judge]`. The solvers start at once, and each:
  - calls `find_candidates` with `detail: 2`, which gives hypotheses plus the code and caller usage in one call;
  - writes `/tmp/repro_N.py` with a `run_command` heredoc (code never goes into nested skill args);
  - registers it with `op=repro`, which runs it on the clean code and warns if it does not fail there (it's not discriminating);
  - reads with `op=read symbol=` and edits with `op=edit`.
- **Diversity:** strategy per solver (direct H1 fix / find where the value first goes wrong / pick by evidence) and
  temperatures 0.2 / 0.5 / 0.7 with different seeds. Thinking is `low`, budget 384. The judge runs with thinking `none` (2 tool calls).
- **wt.py enforces discipline mechanically** (shared lib, backward compatible):
  - Every op auto-creates the checkout.
  - It accepts all argument shapes from run 1: aliases, `positional_args`, a dict nested in `args`, a missing `op`, and
    reads of `/tmp` scripts.
  - Every result ends with `[Ns of 200s used; call k]`.
  - After the 200 s solver deadline, work ops answer `TIME UP`, so the ParallelAgent ends and the judge gets its turn.
  - After the judge has run, a nudge re-run of the root gets `DONE` instead of redoing work.
- **Coherence-collapse guard:** `op=edit` reruns the candidate's repro (or any registered one). When it passes, the edit
  runs the module's existing tests, checkpoints, and **adopts the candidate into /workspace if it is the best so far**
  (fcntl lock). The git-diff fallback therefore always holds the best candidate, even if the judge is never reached.
- **Judge = deterministic cross-validation.**
  - Every candidate patch, both the current state and the best checkpoint, is applied in a scratch checkout.
  - Each is run against every registered repro that fails on the clean code (broken scripts are excluded) and against
    the existing tests (new failures are relative to clean code).
  - Ranking: repros passed › crash-type new failures › identical-patch votes › fewest added lines › total changed lines ›
    assertion-type new failures (these may be tests pinning the old behavior).
  - The winner is adopted, then `submit_patch`.

## Why this can now pass (generic)
- Run-1 time went to serial exploration and argument errors. Now all three solvers work from t≈0, and the first green
  edit is kept in /workspace immediately. Run 1's B reached a passing edit at 182 s on a single thread.
- The final state no longer depends on reaching the last LLM step. It depends on the tool calls the solvers make anyway
  (auto-adopt), and the judge only improves on it.
- Selection does not punish fixes that change behavior the issue asks to change. Discriminating, cross-checked repros
  and minimality decide, and crash-type breakage still disqualifies.
- Nothing here is task-specific: strategies, repro rules and ranking are the same for any repository.

## Risks
- The model may still write a repro that asserts the wrong expectation. Cross-validation helps only if another solver's repro is right.
- If all solvers edit the same wrong function, selection can't fix that. Diversity is only strategy + temperature.
- "Fewest added lines" is a heuristic. It picks the root-cause fix here, but a correct fix can be larger than a workaround.
- Three concurrent contexts: each solver starts at about 3k tokens with `include_contents: none`; KV headroom on 4×L4 should be fine.
- Solvers have `run_command`. If one edits /workspace directly, the next adopt overwrites it; if nothing is ever
  adopted, such an edit reaches the fallback diff.
- The judge's pytest runs are capped at 45 s total. On slow suites it may rank with fewer signals.

## Local checks
- `validate.py C_parallel` OK.
- `tests/test_wt_branch.py` (6) and the C tests in `tests/test_workflow.py` (2, including "green candidate is in the
  workspace even when the judge skips judging") pass, as does the full `tests/`.
- wt.py was exercised on a rich_4076 copy. Three candidates (root fix, caller workaround, crash): auto-adopt kept the root
  fix, and the judge ranked it first in 4.7 s. Also exercised on requests_7205 (src layout): repro, read, edit, auto-adopt, judge.
