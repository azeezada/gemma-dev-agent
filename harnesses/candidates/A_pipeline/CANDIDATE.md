design: SequentialAgent localize(1 tool call) → reproduce(1 call) → repair(inspect → edit → verify, stop at REPRO PASS) → submit(no thinking); per-phase thinking budgets
basis: Agentless, LocAgent/CoSIL (caller context), SWE-Edit, Coherence Collapse (stop at green + checkpoints), AgentFloor/latency model (few, cheap calls)
owner: path-A

# A_pipeline v2: time-boxed Agentless on ADK

## Problems in run 1 (evidence: `work/kaggle-multi-v1/multi_runs/A_pipeline.digest.txt`, `--reasoning`)

| # | Problem | Evidence | Cost |
|---|---|---|---|
| 1 | localize took 12 calls; inspect_symbol rejected `symbol:"rich/ansi.py:AnsiDecoder.decode"` and then `positional_args` ("file must be an existing workspace-relative source file"), so the model paged with read_file | t=15–118 s | 118 s of 300 |
| 2 | repair had no search tool and "believed it could not grep". It paged text.py 10× looking for the *caller* of the site, to learn how its result is used | t=145–187 s, THINK: "I still can't find from_ansi" | ~40 s |
| 3 | First edit applied (t=216 s) but never verified; the repro was never rerun after any edit | no verify.py call in repair | no feedback, no checkpoint |
| 4 | JSON-escaped display (`'\\r\\n'`) made the model "fix" correct code; the escape-decoding fallback then produced "unterminated string literal" ×2; one call lost `skill_name` | t=251–305 s | 4 calls |
| 5 | The pipeline never reached submit, so the git-diff fallback submitted an unverified mid-edit state | agent_error TimeoutError in repair | FAIL |
| 6 | All phases used thinking medium/768. Output tokens are the whole latency cost (deepdive-E: 0.0287 s/token) | 20–30 s calls | — |

Localization itself was right (SITE = the H1 site on the first call). The failure was time and semantics, not search.

## Changes

- **localize** (`configs/localize.yaml`: low, 384): one `find_candidates` call with `detail: 2`. It shows the code at the
  top two sites **and how their callers use the result**, then the agent answers SITE/ENTRY/EXPECTED. No read_file, so no paging.
  At most one extra `inspect_symbol`.
- **reproduce** (low, 512; `run_command` only): one heredoc call writes and runs `/tmp/repro_issue.py`, a table of
  labelled boundary cases (issue example, empty, single, at start/end/repeated/middle, plus one already-working guard)
  printing `PASS/FAIL label input -> actual expected`. It must FAIL on current code (discriminating spec).
- **repair** (medium, 768; skills only): `inspect_symbol` on SITE (body + USED BY) → `apply_edit` → `verify.py` after
  **every** edit (which also checkpoints) → **stop at the first REPRO PASS**, whatever else verify prints. At most 3 edits.
  Instructions to edit at SITE, not callers, and to keep backslash escapes exactly as in the source.
- **submit** (`thinking_level: NONE`, temp 0): exactly `finalize.py` (restores best checkpoint, strips scratch) then
  `submit_patch`. finalize prefers repro-PASS checkpoints, so an overshooting repair can't lose a green state.
- **Shared `lib/skills/swe_locate`** (owned by path-A): target-spelling tolerance, symbol-only lookup, USED BY def-use
  slice, `detail` mode, module-level windows. See the BLACKBOARD LIB-CHANGE and `tests/test_swe_locate_lib.py`.
- **Runtime** (lock protocol): the local loader now accepts the official `thinking_level: NONE` / `include_thoughts: false`
  and maps them to `enable_thinking: false` (tests/test_v3.py).

Planned call budget: localize 2 + reproduce 2 + repair 4–8 + submit 2 = **10–14 calls, ≈100–170 s** by the latency
model, versus 35 calls / 300+ s in run 1.

## Why this can now pass (generic reasoning, no task hints)

1. The site was already found on call 1 in run 1. v2 keeps that and spends 1–2 calls there instead of 12.
2. The failure in semantics came from not seeing how the caller consumes the site's result. USED BY puts the caller's
   call line and the value it combines the pieces with next to the site body, in the same tool result.
   This is general def-use context (LocAgent/CoSIL-style caller awareness), not a task hint.
3. A boundary-case table through the public entry discriminates wrong variants (run 1's first edit would print FAIL
   lines), and verify-after-every-edit gives that feedback within one call.
4. Stop-at-first-REPRO-PASS plus checkpoints plus finalize makes a green state terminal (Coherence Collapse). Existing tests
   that pin the old behaviour are explicitly declared expected.
5. submit is always reached with ~130 s of slack, and it restores the best checkpoint rather than the last state.

## Risks

- **Repro spec quality**: if reproduce writes a wrong expected value, repair optimizes the wrong target. Mitigations:
  expected values come from the issue text, and there is an already-working guard case. There is no second opinion.
- **Vague issues**: localize trusts `find_candidates` H1/H2. If neither is right, the site is wrong and repair can't
  recover, since it has no search. ALTERNATIVES are passed on but not re-tried automatically.
- **Dependence on swe_edit output**: repair keys on the literal `REPRO PASS` printed by verify.py (owned by path-B).
  If B's verdict text says "keep fixing" on a passing repro, the prompt's stop rule must win. See the REQUEST on classifying parametrized
  assertion failures as crashes.
- **Nudge**: if submit answers with text instead of calling submit_patch, the official runner re-runs the whole
  SequentialAgent from localize. The submit prompt is two tool calls, with no thinking and temperature 0.
- **thinking NONE fidelity**: the official ADK→vLLM mapping for NONE is undocumented. If it isn't honoured, submit just
  thinks briefly (harmless).
- Only one repair trajectory, so no diversity. C/D cover that.
