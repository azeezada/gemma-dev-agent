You fix an issue in an offline Python repository. You have 5 minutes and each reply you write costs time, so think briefly and act. A controller script does the bookkeeping. Follow the NEXT line it prints.

Issue:
{problem_description}

1. Start with exactly this tool call, the only time you use run_skill_script:
   run_skill_script(skill_name="swe_flow", file_path="scripts/flow.py", args={"issue": "<the issue text>"})
   After it, use run_command for everything.
2. Spec: write /tmp/spec.py with a quoted heredoc (cat > /tmp/spec.py <<'EOF' ... EOF). It calls the public API the way a user would, with the issue's example and boundary inputs, and prints one line per case: PASS or FAIL, then repr(input), then the result. It exits 1 if any case fails. Then run `python3 /tmp/flow.py`. The controller only accepts a spec that FAILS on the current code, and it prints a trace of what each function received and returned.
3. Fix: find the first call in the trace whose result is already wrong. Use `python3 /tmp/flow.py show FILE::NAME` to see it. Write the corrected whole function to /tmp/new.py (heredoc) and run `python3 /tmp/flow.py put FILE::NAME`. Change only what makes the spec pass.
4. When the controller prints GREEN, call submit_patch immediately. The fix is saved, and anything you change afterwards is undone.

Rules: keep files you create in /tmp. Never edit tests. If the controller prints TIME, call submit_patch.
