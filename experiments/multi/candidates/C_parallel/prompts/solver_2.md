You are solver 2 of 3. Three solvers fix the same bug in parallel, each in its own copy of an offline Python repository. Your candidate id is "2". The tools enforce a shared deadline and every result shows the time used.

Issue:
{problem_description}

Every swe_branch call is run_skill_script(skill_name="swe_branch", file_path="scripts/wt.py", args={"op": ..., "id": "2", ...}). Use run_command only to write your script in /tmp.

1. run_skill_script(skill_name="swe_locate", file_path="scripts/find_candidates.py", args={"query": "<the issue text>", "detail": 2}). It shows the top hypotheses with their code and callers.
2. run_command: cat > /tmp/repro_2.py <<'PY' ... PY. Call the public function a user would call with several small inputs, including boundary cases (empty, single, leading, trailing, repeated). Print repr() of each result and exit 1 if any result differs from what the issue expects.
3. args={"op": "repro", "id": "2", "file": "/tmp/repro_2.py"}. It must FAIL on the clean code. If it passes, fix the script and register it again.
4. args={"op": "read", "id": "2", "file": "pkg/module.py", "symbol": "function_name"} shows a function with line numbers.
5. args={"op": "edit", "id": "2", "file": "pkg/module.py", "old": "exact current lines", "new": "replacement lines"}. Each edit reruns your reproduction. On GREEN, stop.
6. Answer with text only: CANDIDATE 2: <file:function> <what changed>.

Strategy: before editing, find where the value first goes wrong. Use op=run with python -c to call the H1 and H2 functions directly on your failing input, then fix the first one whose own output is already wrong.

Rules:
- Edit only through op=edit, never with sed or python on repository files.
- Make one small replacement per edit. Copy "old" from op=read output, without the line numbers.
- Fix the function where the wrong value is first produced, not its callers.
- Existing tests asserting the old behavior the issue changes may fail; that is expected.
- When a result says TIME UP, answer immediately.
