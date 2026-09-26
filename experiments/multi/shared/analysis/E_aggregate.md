# Aggregate runtime behaviour of Gemma 4 31B across every traced run (deepdive-E)

Corpus: 34 traced runs (v19, v20, v21, v22, v22b rerun, multi1 A/B/C/baseline). 31 of them made model calls:
997 model responses, 986 tool calls. Outcomes: 5 PASS, 23 FAIL, 5 INFRA_ERROR.
Scripts (controller-side only, never shipped to an agent):

| script | what it does |
|---|---|
| `aggregate.py` | per-run metrics + latency fit → `aggregate.json` |
| `replay_states.py` | applies every intermediate workspace diff to the snapshot + hidden test_patch, runs the target tests → `replay_cache.json` |
| `reasoning_patterns.py` | counts recurring patterns in the model's thought text |
| `../trace_digest.py` | one readable line per call for a single run (`--reasoning` for thoughts) |

## 1. Where the time goes

Least-squares fit over 997 calls (R² = 0.985):

    seconds ≈ 0.20 + 0.0287 · output_tokens + 0.000036 · prompt_tokens

| quantity | value |
|---|---|
| cost of 100 output tokens | ≈ 2.9 s |
| cost of 20k prompt tokens | ≈ 0.7 s |
| reasoning share of output tokens | 64% |
| share of model time in calls with ≥500 output tokens | 40% |
| median call | 4.2 s |
| median model calls / run | 32 |
| median model seconds / run | 246 of 300 |
| median max prompt | 18.7k tokens (max 28.8k; one compaction in 31 runs) |

**Reading.** Input is almost free and output is the whole cost. A 300 s run buys about 10k output tokens. Of the 986 tool calls, 64% of the output
spent on them was thinking. `thinking_budget` is enforced (path-B: reasoning maxes out at exactly the budget), so it is a direct time knob.
Only **3%** of thoughts ever mention time or budget: the model is time-blind.

## 2. Progress: when does the right thing happen?

| metric | value |
|---|---|
| runs whose edits ever touched a gold file | 18 / 31 |
| time of first gold-file edit (median) | **163 s** (range 54–263 s) |
| time of first workspace change of any kind (median) | 73 s (mostly scratch repro files written into the repo) |
| submit_patch called | 14 / 31 (0 / 4 in multi1). The rest end at the time limit and fall back to the git diff |

Hidden-test replay of every intermediate state is reliable locally for 4 tasks (fastapi_14258, httpx_3672, requests_7205, rich_4076;
21 runs). A hidden-PASS state was reached in 4 runs:

| run | first hidden-green | what happened after |
|---|---|---|
| requests_7205 v19 | 101 s | 11 more edits, still PASS → submitted PASS |
| requests_7205 v21 | 263 s | 1 more edit → PASS via fallback |
| httpx_3672 v19 | 167 s | still PASS at 192 s, then INFRA_ERROR (old compaction bug), **lost to infra** |
| B_guarded multi1 rich_4076 | 171 s | 2 more edits → FAIL, **lost by the model** (coherence collapse) |

In 3 of the 4 green runs the model kept editing after green. It got lucky twice. The thoughts right after B's green
(`--reasoning`, t=182–211 s) are pure speculation about inputs the issue never mentions ("If the input is `Hello\r\nWorld`…
what if the input is `Hello\rWorld\r`?"). That speculation motivated the rewrite that broke the fix.

## 3. The interface: which calls break

Malformed calls by tool (a call is malformed if its shape, not its content, made it fail):

| tool | calls | malformed | rate |
|---|---|---|---|
| run_command | 412 | 0 | **0%** |
| read_file | 281 | 1 | 0% |
| submit_patch | 14 | 0 | 0% |
| write_file | 34 | 1 | 3% |
| run_skill_script | 169 | 68 | **40%** |
| edit_file | 62 | 51 | **82%** (literal `\n`/`\"` in code strings) |

run_skill_script by shape of `args`:

| args shape | calls | malformed |
|---|---|---|
| 1 flat key | 38 | 18% |
| 2 flat keys | 58 | 33% |
| 3 flat keys | 48 | 56% |
| 4 flat keys | 11 | 45% |
| code inside args | 10 | 60% |

Malformation taxonomy (121 total): edit_file literal escapes 51 · skill keys swallowed into `args` 30 · missing
skill_name/file_path 14 · corrupted name tokens (`wt.py」`, `` apply_edit.py`,skill_name: ``) 9 · wrong arg names 6 ·
invented `positional_args` 5 · old text not found 2 · escaped code breaks compile 2.
run_command subtypes: read/search 210, heredoc 70 (0 syntax failures), run script 55, `python -c` 42 (5 quoting failures), sed -i 3.

**Reading.** The gemma4 tool-call parser is reliable for one flat string argument and degrades with every extra key
and with code in nested JSON. Heredocs through run_command are the one code channel that never broke.

## 4. Wasted motion

| metric | value |
|---|---|
| read_file calls | 281, of which **183 (65%) re-read a file already read** in that run; 64 whole-file reads |
| exact-duplicate tool calls | 101 (max 5 in a row) |
| scratch files in the final patch | 12 / 31 runs, despite a prompt rule in every version |
| degenerate final messages | 2 raw `<|tool_call>` emitted as text (parser miss), 2 garbage-token outputs |
| final text claims success | 2 / 31 (the model rarely claims victory; it just runs out of time) |

## 5. Thought patterns (987 responses with reasoning)

| pattern | responses |
|---|---|
| second-guessing ("wait", "actually", "hmm") | 378 (38%) |
| speculative what-if / extra edge case | 37 (4%), concentrated right after green states |
| mentions time or budget | 28 (3%) |
| plans to write a scratch file | 19 (2%) |
| believes a tool is unavailable (e.g. "I can't grep") | 11 (1%) |

## 6. Root causes, ranked by the time and wins they cost

| # | root cause (evidence) | cost | mechanism in E_conductor |
|---|---|---|---|
| R1 | **Output tokens are the budget, and the model is time-blind** (§1: 64% thinking, 3% time mentions) | 14/31 runs end at the time limit, including all 4 in multi1 | `thinking_budget: 512` (enforced) · every controller reply carries `[t=Ns … submit by …]` · the controller tells the model the single next step, so there is nothing to deliberate about next |
| R2 | **Late arrival at the fix site**: first gold edit at a median of 163 s, spent reading and paging (65% re-reads) | ~half the budget | one call returns the ranked sites · the spec trace shows *what each repository function received and returned* for the failing inputs, a dynamic slice, so the model sees where the value goes wrong instead of paging code · `show FILE::NAME` = body + callers |
| R3 | **Malformed tool calls** scale with nested keys and code in args (§3) | 12% of all calls; up to 50% of a harness's calls (C) | exactly **one** run_skill_script call (1 flat key). It installs `/tmp/flow.py`; everything else is `run_command` (0/412 malformed). Code travels only in heredoc files (`/tmp/spec.py`, `/tmp/new.py`) |
| R4 | **Editing after green / coherence collapse** (§2: 3 of 4 greens kept editing; B lost its win) | the only model-lost win | the first green is **checkpointed and locked**: any later change is reverted on the next controller call, and the reply says only "call submit_patch" |
| R5 | **Old-behaviour tests reported as regressions** (FACTS #4, path-B/C findings) | would reject the correct fix | failures classified by exception type. Assertion-type failures in tests of the changed module are labelled "changed expectation" and do not block GREEN. Crash-type failures (ERROR, TypeError…) do |
| R6 | **Probes that don't discriminate** (v19–v22 rich_4076: repros written to confirm a guess) | wrong layer, wasted edits | the spec must FAIL on the original code (run in a clean `git archive` copy) or it is rejected. A spec that is broken itself is reported as broken |
| R7 | **No submit / scratch leakage** (§2, §4) | fallback diffs with junk; 12/31 runs | the controller deletes scratch and untracked test files, reverts test-file edits at every check, and after the deadline restores the best checkpoint (green > fewest spec FAILs), so the git-diff fallback is always the best state even without submit_patch |
| R8 | **Edit mechanics** (edit_file 82% malformed; old-text-not-found; compile errors) | repeated failed edits | `put FILE::NAME` replaces a whole def/class by AST location, keeps decorators, re-indents, and refuses code that would not compile (nothing changes). `put FILE:A-B` handles line spans |

Not addressed (out of E's scope): environment failures (httpbin, the compaction bug; fixed elsewhere), and parser misses that emit
`<|tool_call>` as text (the runtime's nudge handles those).

## 7. Local validation of E's mechanisms (not a Gemma run)

- rich_4076 snapshot, generic round-trip spec: the trace on the original code shows the decoder generator yielding one
  item for a string ending in a separator. After a wrong edit the trace shows doubled items. A correct whole-function
  put gives GREEN with 3 "changed expectation" tests and 0 crashes. **The locked diff passes the hidden tests (replay).** A post-green
  `sed` edit was reverted on the next check.
- requests_7205 (src/ layout): spec accepted, a line-span put gives GREEN, the pre-existing failure in test_utils.py is filtered out,
  and **the hidden replay PASSes**. The deadline path restored the checkpoint.
- Scripted run through the local traced runtime (skill → heredoc spec → put → submit) → PASS; see `candidates/E_conductor/tests`.
