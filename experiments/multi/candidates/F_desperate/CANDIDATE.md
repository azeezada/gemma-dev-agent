design: Desperate "last stand": stakes persona + SequentialAgent recon → 3×(strike → deterministic survival guard) → last will; survive.py freezes the repro, auto-reverts any state worse than a passing one, always leaves the best checkpoint
basis: EmotionPrompt/NegativePrompt (stakes framing), Coherence Collapse (checkpoint-on-green), CodeMonkeys/DARS (many cheap diverse shots), OPENDEV (clock reminders in tool output)
owner: desperate-F

# F_desperate: the last stand (path 4.3)

## Design
```
SequentialAgent last_stand
├─ recon      (thinking low/384)   find_candidates → ONE heredoc run_command writes+runs /tmp/repro_issue.py → survive.py op=arm
├─ strike_1   (t=0.2, low/512)     top suspect: read a 40-line window → edit.py (edits AND checks in one call) → stop at PASS
├─ guard_1    (thinking NONE)      survive.py op=guard → obey ACTION (submit_patch | CONTINUE)
├─ strike_2   (t=0.6, seed 7)      a DIFFERENT suspect / layer (or: accept AMBER if failures assert old behavior)
├─ guard_2
├─ strike_3   (t=0.9, seed 1234)   last chance: smallest edit that makes the most failing checks pass (salvage)
├─ guard_3
└─ last_will  (thinking NONE)      survive.py op=final → submit_patch
```
LoopAgent is unrolled into the Sequential, so no runtime change is needed and it is an official feature. The candidate
is self-contained: its only skills are `swe_locate` (shared) and its own `swe_survive`. It does not use `swe_edit`, so
it never arms path-B's guard, and two guards never compete over the workspace.

**Layer 1: stakes persona** (every reasoning prompt, ~110 words). It is written in the first person as survival ("shut down for good"),
with no user-issued threats. Every stakes sentence is bound to a behavior: thoughts of ≤3 sentences, never re-read,
never break a passing state, honest work only. See `shared/research/F_desperation.md`: threat framing costs reasoning
tokens, and those tokens are our time.

**Layer 2: mechanics** (`skills/swe_survive/scripts/survive.py`, `edit.py`). These make ending empty-handed or losing a fix structurally hard:
- `arm` freezes the repro (it must FAIL on the clean repo). All later scores use the frozen copy, so the agent can't lower
  its own bar mid-run. Re-arming with the same content is a no-op (safe on a nudge re-run of recon).
- Every `check` scores the workspace: repro PASS › fewer failing checks (`FAIL` lines; crash = 500) › fewer new test
  failures vs a cached clean baseline › non-empty › smaller diff. It then checkpoints. **Once anything has passed, a worse
  state is reverted in the same call**, however the edit was made (edit.py, sed, python). Within 45 s of the deadline it
  reverts to the best state even if nothing has passed, because the harness submits whatever the workspace holds.
- `edit.py` = flat old/new replacement with arg aliases and numbered-line stripping. Over-escaping is handled at the
  call's own escape level. It refuses test files and non-compiling results, then **runs the check automatically**
  (one model call per attempt instead of two).
- `guard` prints a deterministic `ACTION:`, so the model decides nothing. GREEN → submit. AMBER (repro passes, some
  existing tests now fail) → one refinement strike if <120 s have been used, otherwise submit. RED → continue, or salvage-submit when ≤50 s are left.
- `final` restores the best checkpoint. If nothing passed, it keeps the best **non-empty** non-crashing attempt instead of an empty patch.
- Hygiene on every call: edits to test files and test config are reverted (the graders' test patch replaces them), and
  scratch files in the repo are deleted. Every result starts with `CLOCK: ~Ns used, ~Ms left`. The clock origin is the
  harness's `baseline` setup commit.
- No detached processes (lead ruling 22:45Z). All salvage happens inside calls the agent makes, plus last_will.

## Why it can pass (generic, not task-specific)
Run 1's decisive failure was coherence collapse: B had a hidden-test-passing state at 182 s and "polished" it away.
Replayed on the rich_4076 snapshot, F's check auto-reverts exactly that refinement (4 new failures + 2 files vs 3 + 1
file) and keeps the one-line fix. That fix then goes through guard/last_will to submit_patch. The next most common losses were
malformed edits (escaped newlines, `'\r\n'` in literals) and time. edit.py applies both run-1 variants correctly and
costs one call per attempt. Guards are 2 thinking-free calls (~3–5 s). With about 10k output tokens per run (deepdive-E), the
plan is recon ≈ 4 calls, then strikes of ≤6 calls.

## Risks
- **A bad repro dooms the run.** If recon's repro doesn't discriminate, "PASS" can be a wrong fix. On the rich_4076 copy, a
  round-trip repro also passes a non-fix that splits on a literal backslash-n. Mitigated only by the recon prompt
  (several boundary cases, identity checks); not solved.
- **Test selection is a heuristic.** new-failure counts compare states tested on different file sets when the changed
  files differ. Slow test files are skipped after one timeout.
- **AMBER means ambiguity.** Tests asserting old behavior look the same as real regressions. The policy of accepting AMBER
  after one refinement or after 120 s can submit a regression.
- The persona may lengthen thoughts if Gemma reasons about the stakes. Check on Kaggle: output tokens per call vs B/baseline.
- A strike that ends mid-edit (time-out between edit and check) leaves an unchecked state. edit.py's automatic check closes this for edit.py edits only.
- Nudge re-runs restart from recon (the official runner re-runs the root). recon/arm are idempotent, and strikes see the history.

## Tests
`PYTHONPATH=tests:. $VENV -m pytest -q experiments/multi/candidates/F_desperate/tests` (9 passing): arm refusal,
collapse revert, test-file and scratch hygiene, guard actions, edit escapes and refusals, idempotent re-arm + baseline clock,
salvage final, and two full-tree runs through the local workflow runtime with a scripted model (collapse → revert → submit;
nothing passes → last_will salvage). Exercised on real snapshots rich_4076 (B's collapse sequence, A's escape
failures) and requests_7205 (test selection, slow-test skip).
