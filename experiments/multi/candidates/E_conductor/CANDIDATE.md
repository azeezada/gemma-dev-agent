design: One LlmAgent conducted by a controller script: 1 flat skill call installs /tmp/flow.py; all later work via run_command (spec-must-fail gate, value trace, whole-function put, first-green lock, deadline salvage)
basis: deepdive-E aggregate of 34 traced runs; CodeAct, CODESTRUCT entity edits, Coherence Collapse checkpointing, dynamic slicing / print-debugging
owner: deepdive-E

# E_conductor: a single agent conducted by a controller script

Path 4.2: an aggregate design derived from 34 traced runs. Full analysis: `shared/analysis/E_aggregate.md`.

## Shape
One `LlmAgent` (tools: run_command, read_file, submit_patch; skill `swe_flow`), thinking low/512.
1. **One** skill call, `swe_flow/flow.py {"issue": ...}`, ranks the sites (vendored `find_candidates`), installs `/tmp/flow.py`
   and starts the clock.
2. The model writes `/tmp/spec.py` by heredoc (PASS/FAIL line per case) and runs `python3 /tmp/flow.py`. The controller accepts it
   only if it FAILS on the original code, and prints a **value trace**: every repository function the spec's calls reached,
   with arguments → return value (generators: yielded values), failing inputs first.
3. The model writes the corrected whole function to `/tmp/new.py` and runs `python3 /tmp/flow.py put FILE::NAME` (or `FILE:A-B`).
   The controller compiles it, runs the spec, and compares related tests with the original code. On failure it prints a new trace.
4. At GREEN the controller checkpoints, LOCKS, and says "call submit_patch". Later edits are reverted.
Planned: about 6–9 model calls (~60–150 s) when the first fix is right. Each controller call takes <2 s (≤24 s internal cap).

## Root causes → mechanisms (data in E_aggregate.md §6)
| root cause | mechanism |
|---|---|
| R1 output tokens = time; the model is time-blind | thinking_budget 512; a clock in every reply; the controller dictates the next step |
| R2 first gold edit at a median of 163 s; 65% of reads are re-reads | ranked sites + value trace of the failing inputs (dynamic slice) + `show` = body + callers |
| R3 skill calls 40% malformed (rising with keys), run_command 0% | exactly one 1-key skill call; everything else is run_command; code only in heredoc files |
| R4 edits after green (3/4 greens), B's collapse | first green checkpointed + locked; later changes reverted |
| R5 old-behaviour tests look like regressions | failures classified by exception type; assertion changes don't block, crashes do |
| R6 non-discriminating repros | the spec must fail on a clean `git archive` copy; a broken spec is reported as such |
| R7 no submit (14/31), scratch leakage (12/31) | scratch/test-file cleanup at every check; deadline restores the best checkpoint (green > fewest FAIL lines) |
| R8 edit mechanics (edit_file 82% malformed) | AST whole-function put, decorators kept, re-indented, compile-checked (refused, not applied) |

## How it differs from A–D, F
A/C/D split work across agents or branches, and B/F guard a free agent. E keeps **one context** (AgentFloor: decomposition hurt Gemma)
but moves every decision that can be computed into the controller. It is the only design that gives the model **runtime values**
(the trace) instead of more code to read, and the only one whose tool interface is one flat skill call plus plain shell commands.

## Why it can pass (generic)
If the spec reproduces the issue through the public API, the trace names the first function whose output is already
wrong. That is where the fix belongs; all the model has to do is rewrite that function. The run stops at the first green state,
so a correct fix cannot be refined away. Verified locally: the rich_4076 and requests_7205 locked diffs pass the hidden tests (replay).

## Risks
- The official `run_skill_script` must execute in the same container/filesystem as run_command (`/tmp/flow.py` is installed by it).
  The shared lib makes the same assumption (verify.py reads /tmp/repro). If it runs elsewhere, the model gets
  "python3: can't open /tmp/flow.py", and E degrades to a plain shell agent with no guard.
- First-green lock: a spec that is too weak locks a partial fix. Mitigation: the spec template asks for boundary inputs, and
  spec updates are allowed (they must still fail on the original code).
- The trace can be noisy for framework-heavy code (e.g. request routing). It is capped at ~1.5k chars and focused on the failing inputs.
- Clock origin is the first skill call (~5–10 s after the real start). Hard deadline 235 s after that.
- /tmp/spec.py, /tmp/new.py, /tmp/flow.py persist across harnesses in one notebook. E deletes spec/new on a fresh start.

## Files
`agent.yaml`, `prompts/system.md` (≈230 words), `configs/sampling.yaml`, `skills/swe_flow/{SKILL.md, scripts/flow.py,
scripts/tracer.py, scripts/find_candidates.py (vendored copy of lib swe_locate after path-A's 22:55 LIB-CHANGE)}`.
Tests: `tests/test_flow.py` (controller), `tests/test_e_runtime.py` (scripted run through the local runtime → PASS).
