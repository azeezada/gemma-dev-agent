# Shared facts: multi-experiment run 1 (Kaggle scriptVersionId 352840116, 2026-09-25)

Written by the lead before the six experiment agents started. Treat this as ground truth. If you
find something here is wrong, post a `CORRECTION` on BLACKBOARD.md; don't edit this file.

Downloaded output: `work/kaggle-multi-v1/` (runs under `multi_runs/<harness>/*/rich_4076-*`).
Readable digests: `work/kaggle-multi-v1/multi_runs/<harness>.digest.txt` (made by `shared/trace_digest.py`;
re-run it with `--reasoning` to see thoughts).

## Outcome

| harness | outcome | patch | hidden target tests (test_decode / _example / _newlines) |
|---|---|---|---|
| A_pipeline | FAIL, time budget | ansi.py only (wrong semantics: extra `Text('\n')` items) | 3 failed |
| B_guarded | FAIL, time budget | ansi.py + text.py (`splitlines(keepends=True)` + joiner `""`) | test_decode failed |
| C_parallel | FAIL, time budget | **empty** (judge never ran; nothing adopted) | n/a |
| baseline_v22 | FAIL, time budget | text.py + scratch `probe.py` | test_decode failed |

No harness ever called `submit_patch`. Every patch came from the `git diff` fallback.

## Cross-cutting facts (all measured)

1. **Time is the binding constraint.** Each run has 300 s of agent time. Model call latency is about
   1.5 s + ~28 ms per output token (≈35 tok/s decode). Calls that emit 700–1100 reasoning tokens take
   20–31 s; tool-only calls with <100 tokens take 1.5–3 s. A run gets about 30–35 sequential calls.
   `thinking_budget: 768` is **not** enforced as a hard cap (seen: 1095 output tokens). Reasoning
   tokens are the largest time cost.
2. **Localization is not the bottleneck on this task.** `find_candidates` put the correct site
   (`AnsiDecoder.decode`, ansi.py:135) as H1 on the first call in A, B and C. The v22 baseline still
   anchored on text.py.
3. **Coherence collapse cost B the win.** At t=182 s B had edited ansi.py `splitlines()` → `split("\n")`,
   and its repro passed. **Locally, that exact state passes all 24 tests of the hidden test file.**
   B then "refined" it into a wrong patch (keepends + joiner change) and had not checkpointed, because
   it only ran the repro via `run_command` and never called `verify.py` at the green moment. The edit
   was done with `sed` via `run_command`, which bypasses every edit guard.
4. **Existing tests can encode the old behavior.** The fix legitimately breaks
   `tests/test_ansi.py::test_decode_issue_2688` (the hidden test_patch rewrites it). verify.py reports
   it as a `NEW FAILURE`. Any selection or verification that treats "new failures = 0" as mandatory
   will reject the correct patch. Tests that assert the old behavior the issue asks to change need
   different treatment from real regressions.
5. **Malformed tool calls are still frequent** (A 1, B 1, C 6, baseline 3, plus many semantic arg errors):
   - `run_skill_script` missing `file_path` or `skill_name`, or both nested inside `args`
   - `positional_args` invented instead of `args`
   - wrong arg names for wt.py: `command` instead of `cmd`, `start_line` instead of `start`
   - skill names corrupted: `wt.py」`, `apply_edit.py\`,skill_name:`
   - code with `'\r\n'` inside nested args arrives as an unterminated string: "file would not compile"
   - bash heredoc/quoting errors when python is passed via `python3 -c '...'`

   Scripts should accept aliases and positional forms instead of rejecting them.
6. **Whole-file reads waste calls.** A's repair phase read text.py 10 times in windows (~40 s). `read_file`
   without a range returns a truncated head; the model then pages through.
7. **Phase handoffs lose information.** A's repair (`include_contents: none`) re-read files localize had
   already read. C's solvers started from scratch and misused wt.py.
8. **Sequential scouting starves parallel branches.** C's scout used 202 s (29 calls), so the 3 solvers
   got about 100 s each, all spent on setup and arg errors. The concurrent branches did run concurrently
   (≈104 s each in wall time), so vLLM batching works.
9. **Context compaction** triggered once (B at 30.4k prompt tokens → 8.2k). Prompts reached 20–28k tokens by t≈200 s.
10. **Scratch files** still leak via the git-diff fallback (baseline `probe.py`).

## What the correct fix for rich_4076 looks like (EVALUATION ONLY. NEVER put this in a prompt or skill)

Hidden tests require `AnsiDecoder.decode` to yield N+1 pieces for N newlines (`split("\n")`
semantics, a trailing `Text("")` after a final newline) so that `Text.from_ansi(x).plain == x`
round-trips. The gold patch is `re.split(r"(?<=\n)", text)` + `rstrip("\n")`, which is equivalent.
The fix at the `from_ansi` level (append "\n") passes 2 of 3 tests but fails `test_decode`.
Using this to write prompts is task-specific hinting and is forbidden (user rule). Use it only
to judge whether a *generic* strategy would have led there, e.g. round-trip/identity property probes,
minimal edits at the lowest layer that loses data, and "stop at first green".

## Environment for local work

- Python with pyyaml + pytest: `/private/tmp/claude-501/-Users-dawoodazeeza-Projects-gemma-dev/fa776964-5234-4294-83f2-ad88b52326c9/scratchpad/venv/bin/python`
  (tests: `PYTHONPATH=tests:. $VENV -m pytest -q tests/test_workflow.py tests/test_swe_tools.py`)
- Validate candidates: `PYTHONPATH=tests:. $VENV experiments/multi/validate.py [NAME]`
- Build notebook (lead does the final build): `$VENV tools/build_multi_experiment_notebook.py`
- rich_4076 snapshot: `snapshots/rich_4076.tgz`. Extract your OWN copy into your own scratch subdir
  (e.g. `$SCRATCH/<your-agent-name>/rich`) to try skill scripts; hidden test patch/gold are in `tasks.jsonl`
  (for evaluating your *harness*, never to be embedded).
- Gemma cannot run locally. Wiring can be tested with a scripted client (see `tests/test_workflow.py` `RouterClient`).
- Older traces for comparison: `work/kaggle-trace-v19`, `-v20`, `-v21`, `-v22`, `work/trace-v21-review`.
- Official harness contract: `HARNESS_README.md`. Research plan and literature: `documentation/HARNESS_RESEARCH_PLAN.md`.
