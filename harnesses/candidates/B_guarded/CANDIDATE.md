design: One LlmAgent, short prompt, low thinking budget; swe_edit guard checkpoints every passing repro run (even via sed/run_command), verify gives a SUBMIT verdict, finalize/deadline restore the best passing state
basis: SWE-agent ACI, Coherence Collapse (2603.24631), OPENDEV event-driven reminders, harness-design study
owner: path-B agent

## Problems in run 1 (evidence: work/kaggle-multi-v1/multi_runs/B_guarded.digest.txt --reasoning)
1. **Coherence collapse.** At t=182 s a `sed` edit (`splitlines()` → `split("\n")`) made the repro pass. That state passes all 24
   hidden tests (checked locally). At 203 s the model invented an input the issue never mentions (bare CR) and rewrote the fix into
   keepends + joiner "" (wrong). Nothing was checkpointed: verify.py was first called at 247 s, and sed bypasses apply_edit.
2. **Correct fixes break old tests.** On the original tests the correct state has 3 new failures, all assertions that the hidden test
   patch rewrites; the collapsed state has 4. verify called them all "NEW FAILURE", and the message was ambiguous.
3. **Time.** Reasoning hit the 768-token `thinking_budget` cap on 4 calls (≈22–31 s each). The budget is enforced, so it is a time knob.
4. **Args.** `run_skill_script` without `file_path`; `python3 -c '...'` quoting failures. Context compaction at 30k.
5. Never called submit_patch; the git-diff fallback took the last (wrong) state.

## Changes
Candidate: a ~270-word prompt with exact call forms. The repro is run through verify.py from the start (arms the guard, must say BUG
REPRODUCED). Fix where the wrong value is first produced, with the smallest edit. **Stop at `VERDICT: SUBMIT` or at a [guard] PASS line**,
then finalize + submit. Don't extend a passing fix to unmentioned inputs; after 240 s, submit. Sampling: `thinking_level: low`,
`thinking_budget: 384` (was medium/768).

Shared `lib/skills/swe_edit` (backward compatible; fallbacks when guard.py is absent, e.g. swe_branch copies):
- `guard.py` (new). The first apply_edit/verify/finalize `main` in the real workspace arms it: it writes a state marker and a `.pth`
  hook. `python /tmp/repro*.py` in an armed workspace, run_command included, then records a checkpoint and prints
  `[guard] [t≈Ns of 300s] the reproduction PASSES … checkpoint #N … finalize.py and submit_patch`. The hook is a no-op for any other
  script, workspace, pytest or child run. Clock origin = HEAD "baseline" commit time.
- **Selection** (finalize, deadline): only repro-PASS states; crash-type new failures rank last; current state wins ties; then fewer
  assertion failures, smaller diff. Hook checkpoints get tests measured lazily in a clean copy.
- **Deadline salvage inside agent calls**: with <45 s left, any apply_edit/verify/repro run restores the best passing state and says so
  (apply_edit then refuses the edit). The detached watchdog is OFF by default (`SWE_GUARD_WATCHDOG=1`, ablation only).
- verify.py: `VERDICT:` SUBMIT (repro PASS, 0 crash, ≤3 assertion failures; still prints GREEN) / FIX / NOT FIXED / BUG REPRODUCED /
  REPRO TOO WEAK. Failures classified by exception type (pytest run with COLUMNS=100000 so parametrized ids keep their message;
  falls back to the `E ` traceback line). Aliases: --script/--path/--file or positional repro.
- apply_edit.py: aliases (path/filepath/filename, search/find/old_string, replace/new_string, 3 positionals). `--new` is tried **as typed
  first**, then with line breaks decoded only outside string literals, then fully decoded. The error reports the as-typed compile error
  (fixes A's run-1 `'\r\n'` → unterminated string).

## Why this can now pass (generic)
Replay of B's actual run-1 sequence on the real snapshot (`tests/test_swe_guard.py` + scratch replay): the sed edit → repro run via plain
`python` → hook checkpoint #2 + PASS notice; B's collapsed patch → verify; **finalize restores #2**, and that tree passes 24/24 hidden tests.
Even without the model obeying the stop rule, finalize or the next skill call after 255 s puts the passing state back. The correct fix now
gets `VERDICT: SUBMIT` rather than FIX. Lower thinking cuts the long-call cost by about half.

## Risks
- The model must still write a discriminating repro; a weak repro gives false greens (verify flags "passes WITHOUT any change").
- Salvage only runs when the agent makes a call after 255 s; a long final reasoning call can still time out on a bad state.
- The `.pth` hook needs a writable site-packages in Container A (unknown officially). Without it, checkpoints come only from verify.
- ≤3-assertion SUBMIT threshold is a heuristic; a wrong fix breaking ≤3 pinned tests also gets SUBMIT (finalize still ranks).
- thinking_budget 384 may hurt the fix-reasoning step.
