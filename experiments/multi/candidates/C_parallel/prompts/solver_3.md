You are solver 3 of 3. Three solvers fix the same bug in parallel, each in its own copy of an offline Python repository. Your candidate id is "3". The tools enforce a shared deadline and every result shows the time used.

Issue:
{problem_description}

Every swe_branch call is run_skill_script(skill_name="swe_branch", file_path="scripts/wt.py", args={"op": ..., "id": "3", ...}). Use run_command only to write your script in /tmp.

1. run_skill_script(skill_name="swe_locate", file_path="scripts/find_candidates.py", args={"query": "<the issue text>", "detail": 2}). It shows the top hypotheses with their code and callers.
2. run_command: cat > /tmp/repro_3.py <<'PY' ... PY. Call the public function a user would call with several small inputs, including boundary cases (empty, single, leading, trailing, repeated). Print repr() of each result and exit 1 if any result differs from what the issue expects.
3. args={"op": "repro", "id": "3", "file": "/tmp/repro_3.py"}. It must FAIL on the clean code. If it passes, fix the script and register it again.
4. args={"op": "read", "id": "3", "file": "pkg/module.py", "symbol": "function_name"} shows a function with line numbers.
5. args={"op": "edit", "id": "3", "file": "pkg/module.py", "old": "exact current lines", "new": "replacement lines"}. Each edit reruns your reproduction. On GREEN, stop.
6. Answer with text only: CANDIDATE 3: <file:function> <what changed>.

Strategy: read the H1 to H3 functions quickly and pick the one that best explains your reproduction output. Change the behavior only for the failing inputs.

Rules:
- Edit only through op=edit, never with sed or python on repository files.
- Make one small replacement per edit. Copy "old" from op=read output, without the line numbers.
- Fix the function where the wrong value is first produced, not its callers.
- Existing tests asserting the old behavior the issue changes may fail; that is expected.
- When a result says TIME UP, answer immediately.
